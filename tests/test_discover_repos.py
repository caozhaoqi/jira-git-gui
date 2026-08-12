"""仓库发现：翻页遍历 + 合并去重 + 发现数日志 的回归测试（纯逻辑，不联网）。"""
import unittest
from unittest import mock

from core.client import JiraGitClient, RepoInfo
from core.models import ConnectConfig


class _FakeResp:
    def __init__(self, status=200, payload=None, url="/x"):
        self.status_code = status
        self._payload = payload
        self.url = url

    def json(self):
        # 与 httpx.Response.json(self) 一致：是实例方法，需接受 self
        return self._payload


def _make_client():
    c = JiraGitClient()
    c.config = ConnectConfig()
    c.config.cookie = "dummy-session-cookie"
    c.config.jira_url = "https://jira.example.com"
    return c


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


class TestRestPagination(unittest.TestCase):
    def test_paginates_until_partial_page(self):
        c = _make_client()
        pages = {
            0: [{"id": i, "displayName": f"r{i}"} for i in range(50)],
            50: [{"id": i, "displayName": f"r{i}"} for i in range(50, 100)],
            100: [{"id": i, "displayName": f"r{i}"} for i in range(100, 130)],
        }

        def fake_get(url, headers=None):
            if "startAt=0" in url:
                return _FakeResp(payload=pages[0])
            if "startAt=50" in url:
                return _FakeResp(payload=pages[50])
            if "startAt=100" in url:
                return _FakeResp(payload=pages[100])
            return _FakeResp(payload=[])

        c.http_get = fake_get
        out = c._discover_repos_rest()
        self.assertEqual(len(out), 130)
        self.assertIn("129", out)

    def test_stops_when_server_ignores_pagination(self):
        # 服务端忽略 startAt，每次都回同一批 -> 不应死循环
        c = _make_client()
        same = [{"id": i, "displayName": f"r{i}"} for i in range(50)]

        calls = {"n": 0}

        def fake_get(url, headers=None):
            calls["n"] += 1
            return _FakeResp(payload=same)

        c.http_get = fake_get
        out = c._discover_repos_rest()
        self.assertEqual(len(out), 50)
        # 第一页充满 -> 第二页仍同 -> 立即停止，最多 2 次请求
        self.assertLessEqual(calls["n"], 2)

    def test_stops_on_empty_page(self):
        c = _make_client()
        calls = {"n": 0}

        def fake_get(url, headers=None):
            calls["n"] += 1
            return _FakeResp(payload=[])  # 首屏即空

        c.http_get = fake_get
        out = c._discover_repos_rest()
        self.assertEqual(out, {})
        # 各端点/各分页约定都会尝试，但绝不会死循环（远低于安全上限）
        self.assertLess(calls["n"], 40)


class TestDiscoverMerge(unittest.TestCase):
    def test_html_branch_enrichment_and_count_log(self):
        c = _make_client()
        html = {
            "1": RepoInfo(repo_id="1", display_name="repoA", default_branch="main"),
        }
        rest = {
            "1": RepoInfo(repo_id="1", display_name="repoA", clone_url="http://c1"),
            "2": RepoInfo(repo_id="2", display_name="repoB", clone_url="http://c2"),
        }
        c._discover_repos_html = lambda *a: html
        c._discover_repos_rest = lambda *a: rest
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
        c._discover_repos_html = lambda *a: {}
        c._discover_repos_rest = lambda *a: {}
        with self.assertLogs("jira-git-gui", level="WARNING") as cm:
            self.assertEqual(c.discover_repos(), [])
        self.assertTrue(any("仓库发现：0 个" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
