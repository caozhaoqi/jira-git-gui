"""仓库发现：翻页遍历 + 合并去重 + 发现数日志 的回归测试（纯逻辑，不联网）。"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.client import JiraGitClient, RepoInfo
from core.client.repos import ReposMixin
from core.models import ConnectConfig


class _FakeResp:
    def __init__(self, status=200, payload=None, url="/x",
                 content_type="application/json;charset=UTF-8"):
        self.status_code = status
        self._payload = payload
        self.url = url
        self.headers = {"content-type": content_type}

    @property
    def text(self):
        # 与 httpx.Response.text 一致：返回响应体字符串
        return str(self._payload) if self._payload is not None else ""

    def json(self):
        # 与 httpx.Response.json(self) 一致：是实例方法，需接受 self
        return self._payload


def _make_client():
    c = JiraGitClient()
    c.config = ConnectConfig()
    c.config.cookie = "dummy-session-cookie"
    c.config.jira_url = "https://jira.example.com"
    return c


class _CacheMixin:
    """将仓库发现缓存（store/repos_cache.json）重定向到临时文件，避免污染真实
    store 并隔离跨测试/多次调用间的缓存状态。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp()
        self._cache_file = Path(self._tmp) / "repos_cache.json"
        self._cache_patch = mock.patch(
            "core.client.repos.ReposMixin._REPO_CACHE_FILE", self._cache_file)
        self._cache_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()


class TestNormalizeAndParse(unittest.TestCase):
    def test_normalize_list(self):
        self.assertEqual(JiraGitClient._normalize_rest_list([1, 2]), [1, 2])

    def test_normalize_wrapped(self):
        data = {"repositories": [{"id": 1}], "total": 2}
        self.assertEqual(JiraGitClient._normalize_rest_list(data), [{"id": 1}])

    def test_normalize_single_object(self):
        self.assertEqual(
            JiraGitClient._normalize_rest_list({"id": 5, "displayName": "x"}),
            [{"id": 5, "displayName": "x"}])

    def test_normalize_unknown(self):
        self.assertEqual(JiraGitClient._normalize_rest_list({"foo": "bar"}), [])

    def test_parse_rest_repo_item(self):
        ri = JiraGitClient._parse_rest_repo_item(
            {"id": "7", "displayName": "repo7", "cloneUrl": "u"})
        self.assertEqual(ri.repo_id, "7")
        self.assertEqual(ri.display_name, "repo7")

    def test_parse_rest_repo_item_no_id_skipped(self):
        self.assertIsNone(JiraGitClient._parse_rest_repo_item({"name": "no id"}))


class TestHtmlPagination(unittest.TestCase):
    """HTML 分页遍历：385 仓库分 4 页（每页 100），必须全部取到。"""

    @staticmethod
    def _html_page(page_idx, repos):
        """构造一页 AllRepositories HTML（含 out of 总数提示 + 仓库锚点）。"""
        total = 385
        anchors = []
        for rid, name in repos:
            anchors.append(
                f'<a href="/secure/GIJBrowseGit.jspa?repoId={rid}&branchName=master">'
                f'{name}</a>')
        body = "\n".join(anchors)
        return (
            f'<html><body>'
            f'Showing {page_idx * 100 + 1} - {page_idx * 100 + len(repos)} repositories out of {total}\n'
            f'{body}'
            f'</body></html>'
        )

    def test_paginates_all_pages(self):
        """4 页共 385 仓库，应全部解析到。"""
        c = _make_client()
        # 模拟 4 页：100 + 100 + 100 + 85 = 385
        pages_data = [
            [(i, f"acme/repo{i}") for i in range(1, 101)],       # page 0: 100 个
            [(i, f"acme/repo{i}") for i in range(101, 201)],     # page 1: 100 个
            [(i, f"acme/repo{i}") for i in range(201, 301)],     # page 2: 100 个
            [(i, f"acme/repo{i}") for i in range(301, 386)],     # page 3: 85 个
        ]

        def fake_get(url, headers=None):
            import re as _re
            m = _re.search(r'pageIndex=(\d+)', url or "")
            idx = int(m.group(1)) if m else 0
            if idx < len(pages_data):
                html = self._html_page(idx, pages_data[idx])
                return _FakeResp(status=200, payload=html, url=url,
                                 content_type="text/html;charset=UTF-8")
            return _FakeResp(status=200, payload="<html></html>", url=url,
                             content_type="text/html;charset=UTF-8")

        c.http_get = fake_get
        with mock.patch("core.client.repos.time") as t:
            t.strftime.return_value = "20200101_000000"
            out = c._discover_repos_html()
        self.assertEqual(len(out), 385)

    def test_stops_on_empty_page(self):
        """空页立即停止翻页。"""
        c = _make_client()
        calls = {"n": 0}

        def fake_get(url, headers=None):
            calls["n"] += 1
            # 第一页有内容，第二页为空
            if calls["n"] == 1:
                html = self._html_page(0, [(1, "acme/a"), (2, "acme/b")])
                return _FakeResp(status=200, payload=html, url=url,
                                 content_type="text/html;charset=UTF-8")
            return _FakeResp(status=200, payload="<html><body>no repos</body></html>",
                             url=url, content_type="text/html;charset=UTF-8")

        c.http_get = fake_get
        out = c._discover_repos_html()
        self.assertEqual(len(out), 2)
        self.assertLessEqual(calls["n"], 2)

    def test_stops_at_total_hint(self):
        """页面声明 out of N 且已累计 >= N 时提前终止。"""
        c = _make_client()
        calls = {"n": 0}
        total_repos = 150  # 声明总数 150

        def fake_get(url, headers=None):
            calls["n"] += 1
            idx = calls["n"] - 1
            if idx == 0:
                repos = [(i, f"acme/r{i}") for i in range(1, 101)]
            else:
                repos = [(i, f"acme/r{i}") for i in range(101, 151)]
            html = (
                f'<html><body>Showing ... out of {total_repos}\n'
                + "\n".join(
                    f'<a href="/secure/GIJBrowseGit.jspa?repoId={rid}">{name}</a>'
                    for rid, name in repos)
                + '</body></html>'
            )
            return _FakeResp(status=200, payload=html, url=url,
                             content_type="text/html;charset=UTF-8")

        c.http_get = fake_get
        out = c._discover_repos_html()
        self.assertEqual(len(out), 150)
        self.assertLessEqual(calls["n"], 2)  # 取完即停，不请求第 3 页


class TestRestPagination(unittest.TestCase):
    """REST 翻页遍历：实测端点为 /rest/gitplugin/1.0/repository/all，
    采用 offset/limit 分页（limit 上限 100），信封带 total 字段。"""

    def _fake_for(self, pages, total, per_endpoint_404=False):
        def fake_get(url, headers=None):
            if "repository/all" not in url:
                # 其余候选端点一律 404（真实实例中复数 /repositories 等不存在）
                return _FakeResp(status=404, payload={"error": "nf"}, url=url)
            if "offset=0" in url:
                return _FakeResp(payload={"total": total, "offset": 0,
                                         "count": len(pages[0]), "repositories": pages[0]})
            for off in pages:
                if f"offset={off}" in url:
                    return _FakeResp(payload={"total": total, "offset": off,
                                             "count": len(pages[off]),
                                             "repositories": pages[off]})
            return _FakeResp(payload={"total": total, "offset": 0,
                                     "count": 0, "repositories": []})
        return fake_get

    def test_paginates_until_partial_page(self):
        c = _make_client()
        pages = {
            0: [{"id": i, "displayName": f"r{i}"} for i in range(100)],
            100: [{"id": i, "displayName": f"r{i}"} for i in range(100, 200)],
            200: [{"id": i, "displayName": f"r{i}"} for i in range(200, 230)],
        }
        c.http_get = self._fake_for(pages, total=230)
        out = c._discover_repos_rest()
        self.assertEqual(len(out), 230)
        self.assertIn("229", out)

    def test_stops_when_server_ignores_pagination(self):
        # 服务端忽略 offset，每次都回同一批 -> 不应死循环
        c = _make_client()
        same = [{"id": i, "displayName": f"r{i}"} for i in range(100)]
        calls = {"n": 0}

        def fake_get(url, headers=None):
            calls["n"] += 1
            if "repository/all" not in url:
                return _FakeResp(status=404, payload={}, url=url)
            return _FakeResp(payload={"total": None, "count": 100, "repositories": same})

        c.http_get = fake_get
        out = c._discover_repos_rest()
        self.assertEqual(len(out), 100)
        # 第一页充满 -> 第二页仍同（无新增）-> 立即停止，最多 2 次请求
        self.assertLessEqual(calls["n"], 2)

    def test_stops_on_empty_page(self):
        c = _make_client()
        calls = {"n": 0}

        def fake_get(url, headers=None):
            calls["n"] += 1
            if "repository/all" not in url:
                return _FakeResp(status=404, payload={}, url=url)
            return _FakeResp(payload={"total": None, "count": 0, "repositories": []})

        c.http_get = fake_get
        out = c._discover_repos_rest()
        self.assertEqual(out, {})
        # 仅 repository/all 与其兜底约定各 1 次首屏，外加其余 3 候选端点，绝不死循环
        self.assertLess(calls["n"], 12)


class TestRestRepositoryAllEnvelope(unittest.TestCase):
    """针对真实端点 /rest/gitplugin/1.0/repository/all 的信封解析与 clone URL 提取。"""

    def test_envelope_total_and_clone_url_from_gk_repo_url(self):
        # 模拟真实 item：clone url 藏在 gkRepoUrl 的 ?url= 参数里
        item = {
            "id": 736,
            "displayName": "demo_selenium",
            "group": "demo",
            "integrationType": "GITLAB_SERVER",
            "gkRepoUrl": "gitkraken://repolink/abc?url=https%3A%2F%2Fcode.example.io%2Fdemo%2Fselenium.git",
            "glRepoUrl": "vscode://x?url=https%3A%2F%2Fcode.example.io%2Fdemo%2Fselenium.git",
        }
        ri = JiraGitClient._parse_rest_repo_item(item)
        self.assertEqual(ri.repo_id, "736")
        self.assertEqual(ri.display_name, "demo_selenium")
        self.assertEqual(ri.clone_url, "https://code.example.io/demo/selenium.git")

    def test_normalize_envelope_returns_total(self):
        class _R:
            def __init__(self, d): self._d = d
            def json(self): return self._d
        items, total = JiraGitClient._normalize_rest_envelope(
            _R({"success": True, "total": 385, "offset": 0, "count": 100,
                "repositories": [{"id": 1}, {"id": 2}]}))
        self.assertEqual(total, 385)
        self.assertEqual(len(items), 2)

    def test_realistic_385_across_four_pages(self):
        c = _make_client()
        # 385 = 100 + 100 + 100 + 85，分 4 页（与截图一致）
        pages = {
            0:   [{"id": i, "displayName": f"acme/r{i}"} for i in range(1, 101)],
            100: [{"id": i, "displayName": f"acme/r{i}"} for i in range(101, 201)],
            200: [{"id": i, "displayName": f"acme/r{i}"} for i in range(201, 301)],
            300: [{"id": i, "displayName": f"acme/r{i}"} for i in range(301, 386)],
        }

        def fake_get(url, headers=None):
            if "repository/all" not in url:
                return _FakeResp(status=404, payload={}, url=url)
            for off in pages:
                if f"offset={off}" in url:
                    return _FakeResp(payload={"total": 385, "offset": off,
                                             "count": len(pages[off]),
                                             "repositories": pages[off]})
            return _FakeResp(payload={"total": 385, "count": 0, "repositories": []})

        c.http_get = fake_get
        out = c._discover_repos_rest()
        self.assertEqual(len(out), 385)


class TestDiscoverMerge(_CacheMixin, unittest.TestCase):
    def test_html_branch_enrichment_and_count_log(self):
        c = _make_client()
        html = {
            "1": RepoInfo(repo_id="1", display_name="repoA", default_branch="main"),
        }
        rest = {
            "1": RepoInfo(repo_id="1", display_name="repoA", clone_url="http://c1"),
            "2": RepoInfo(repo_id="2", display_name="repoB", clone_url="http://c2"),
        }
        c._discover_repos_html = lambda *a, **k: html
        c._discover_repos_rest = lambda *a, **k: rest
        with self.assertLogs("jira-git-gui", level="INFO") as cm:
            result = c.discover_repos()
        # 合并去重：2 个仓库；repo1 的默认分支由 HTML 补全
        self.assertEqual(len(result), 2)
        by_id = {r.repo_id: r for r in result}
        self.assertEqual(by_id["1"].default_branch, "main")
        self.assertEqual(by_id["1"].clone_url, "http://c1")
        # 日志包含发现数
        self.assertTrue(any("合并去重后共 2 个" in m for m in cm.output))

    def test_no_cookie_returns_empty(self):
        c = _make_client()
        c.config.cookie = ""
        self.assertEqual(c.discover_repos(), [])

    def test_zero_discovery_warns(self):
        c = _make_client()
        c._discover_repos_html = lambda *a, **k: {}
        c._discover_repos_rest = lambda *a, **k: {}
        with self.assertLogs("jira-git-gui", level="WARNING") as cm:
            self.assertEqual(c.discover_repos(), [])
        self.assertTrue(any("仓库发现：0 个" in m for m in cm.output))


class TestRestUnavailableCache(_CacheMixin, unittest.TestCase):
    """真实场景回归：HTML 只给 3 个仓库、REST 全部 404 时，
    第一次发现应正常返回 3 个并把「REST 不可用」缓存；
    第二次发现应跳过 9 次 REST 白打，只请求 HTML。"""

    HTML = (
        '<a href="/secure/GIJBrowseGit.jspa?repoId=895&branchName=master">acme/core</a>'
        '<a href="/secure/GIJBrowseGit.jspa?repoId=1022">acme/web</a>'
        '<a href="/secure/GIJBrowseGit.jspa?repoId=1032">acme/api</a>'
    )

    def _make(self):
        c = _make_client()
        calls = {"html": 0, "rest": 0}

        def fake_get(url, headers=None):
            if "AllRepositories" in url:
                calls["html"] += 1
                return _FakeResp(status=200, payload=self.HTML, url=url,
                                 content_type="text/html;charset=UTF-8")
            # 所有 REST 端点一律 404（真实实例中 REST 接口未暴露的表现）
            calls["rest"] += 1
            return _FakeResp(status=404, payload={"error": "not found"}, url=url)

        c.http_get = fake_get
        c._calls = calls
        return c

    def test_first_discovery_returns_html_repos_and_caches(self):
        c = self._make()
        with mock.patch("core.client.repos.time") as t:
            t.strftime.return_value = "20200101_000000"
            res = c.discover_repos()
        # 结果按 display_name 排序：acme/api(1032) < acme/core(895) < acme/web(1022)
        self.assertEqual({r.repo_id for r in res}, {"895", "1022", "1032"})
        self.assertTrue(c._rest_unavailable, "应已缓存 REST 不可用")
        # 首次：HTML 1 次 + REST 8 次（4 候选端点 × 2 分页约定，各首屏即 404）
        self.assertEqual(c._calls["html"], 1)
        self.assertEqual(c._calls["rest"], 8)

    def test_second_discovery_skips_rest(self):
        c = self._make()
        with mock.patch("core.client.repos.time") as t:
            t.strftime.return_value = "20200101_000000"
            c.discover_repos()          # 第一次：REST 8 次，并写入文件缓存
            rest_after_first = c._calls["rest"]
            # 清掉文件缓存，仅保留内存中的「REST 不可用」标记，模拟同一会话内二次发现
            self._cache_file.unlink(missing_ok=True)
            c.discover_repos()          # 第二次：应跳过 REST（走内存缓存结论）
        self.assertEqual(c._calls["rest"], rest_after_first,
                         "第二次发现不应再发任何 REST 请求")
        self.assertEqual(c._calls["html"], 2,
                         "第二次发现仍应请求 HTML（唯一可靠来源）")

    def test_set_config_clears_cache(self):
        c = self._make()
        with mock.patch("core.client.repos.time") as t:
            t.strftime.return_value = "20200101_000000"
            c.discover_repos()
        self.assertTrue(c._rest_unavailable)
        c.set_config(ConnectConfig())  # 换配置后缓存作废
        self.assertFalse(c._rest_unavailable)


if __name__ == "__main__":
    unittest.main(verbosity=2)
