"""单元测试：core 层纯逻辑（解析 / 工具函数），不发起任何网络请求。

运行：
    cd /Users/caozhaoqi/PycharmProjects/jira-git-gui
    PYTHONPATH=. ./venv/bin/python -m unittest tests.test_core_parsing -v
"""
import unittest

from core.client import JiraGitClient
from core.models import ConnectConfig, RepoInfo, TreeEntry


# 仿真的 AllRepositories 页面 HTML（覆盖：真实仓库链接、噪声锚点、空名、重复 id、嵌套无关链接）
ALL_REPOS_HTML = """
<html><body>
<h1>Repositories</h1>
<table>
  <tr><td><a href="/secure/GIJBrowseGit.jspa?repoId=1032&branchName=master">hcm-cloud-vue</a></td></tr>
  <tr><td><a href="/secure/GIJBrowseGit.jspa?repoId=1033&branchName=develop">hcm-core</a>
          (<a href="/secure/GIJCommitBrowser.jspa?repoId=1033">commits</a>)</td></tr>
  <tr><td><a href="/secure/GIJBrowseGit.jspa?repoId=1034">   </a></td></tr>
  <tr><td><a href="/secure/GIJRepositoryBrowser.jspa?repoId=1035&branchName=main">My Repo Long Name</a></td></tr>
  <tr><td><a href="/secure/GIJBrowseGit.jspa?repoId=1036&branchName=feature/x">dup</a>
          <a href="/secure/GIJBrowseGit.jspa?repoId=1036&branchName=feature/x">duplicate repo name longer</a></td></tr>
</table>
<a href="/browse/OTHERPROJECT">unrelated link</a>
</body></html>
"""

# 仿真 browse 页中的 ns.repoInfo（含嵌套 lastCommit 对象，验证括号配平解析）
REPO_INFO_HTML = 'var ns={}; ns.repoInfo = {"id": "1032", "displayName": "hcm-cloud-vue", "lastCommit": {"name": "deadbeef"}};'

# 仿真 browse 页中的 ns.data（文件树单层）
TREE_HTML = (
    'ns.data = {"files": ['
    '{"path": "src", "directory": true, "size": null},'
    '{"path": "README.md", "directory": false, "size": 123}'
    ']};'
)


class TestParseRepoList(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()

    def test_parses_expected_repos(self):
        repos = self.c._parse_repo_list(ALL_REPOS_HTML)
        by_id = {r.repo_id: r for r in repos}
        self.assertEqual(len(repos), 4)  # 1032/1033/1035/1036（1034 空名被跳过）
        self.assertEqual(by_id["1032"].display_name, "hcm-cloud-vue")
        self.assertEqual(by_id["1032"].default_branch, "master")
        self.assertEqual(by_id["1033"].display_name, "hcm-core")
        self.assertEqual(by_id["1033"].default_branch, "develop")
        self.assertEqual(by_id["1035"].display_name, "My Repo Long Name")
        self.assertEqual(by_id["1035"].default_branch, "main")

    def test_noise_and_empty_skipped(self):
        repos = self.c._parse_repo_list(ALL_REPOS_HTML)
        ids = {r.repo_id for r in repos}
        self.assertNotIn("1034", ids)  # 空名

    def test_duplicate_repo_id_keeps_longest_name(self):
        repos = self.c._parse_repo_list(ALL_REPOS_HTML)
        r1036 = next(r for r in repos if r.repo_id == "1036")
        self.assertEqual(r1036.display_name, "duplicate repo name longer")
        self.assertEqual(r1036.default_branch, "feature/x")

    def test_returns_repo_info_instances(self):
        repos = self.c._parse_repo_list(ALL_REPOS_HTML)
        self.assertTrue(all(isinstance(r, RepoInfo) for r in repos))

    def test_empty_html(self):
        self.assertEqual(self.c._parse_repo_list(""), [])


class TestStripTags(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()

    def test_strip(self):
        self.assertEqual(self.c._strip_tags("<b>hello</b> world"), "hello world")
        self.assertEqual(self.c._strip_tags("  <i>x</i>  "), "x")
        self.assertEqual(self.c._strip_tags(None), "")


class TestUtilities(unittest.TestCase):
    def test_b64_prefix_account(self):
        pat = "REDACTED_PAT_PLACEHOLDER"
        self.assertEqual(JiraGitClient.b64_prefix_account(pat), "871601245784")

    def test_b64_prefix_account_invalid(self):
        self.assertIsNone(JiraGitClient.b64_prefix_account("not-a-valid-pat"))
        self.assertIsNone(JiraGitClient.b64_prefix_account(""))

    def test_pat_secret(self):
        # base64("871601245784:mysecret9") 形态的伪 PAT
        import base64
        pat = base64.b64encode(b"871601245784:mysecret9").decode()
        self.assertEqual(JiraGitClient._pat_secret(pat), "mysecret9")

    def test_pat_secret_no_colon(self):
        # 无 ":" 分隔时返回 None（无法拆出密钥）
        self.assertIsNone(JiraGitClient._pat_secret("not-a-valid-pat"))

    def test_encode_pat_slash(self):
        self.assertEqual(JiraGitClient.encode_pat("a/b/c"), "a%2Fb%2Fc")

    def test_host_of(self):
        self.assertEqual(JiraGitClient.host_of("https://jira.hcmcloud.cn/x"), "jira.hcmcloud.cn")
        self.assertEqual(JiraGitClient.host_of("http://example.com/"), "example.com")


class TestCloneUserCandidates(unittest.TestCase):
    def _pat_with_account(self, account: str) -> str:
        # 用 base64 构造一个「账号:secret」前缀的伪 PAT，避免硬编码真实令牌
        import base64
        head = base64.b64encode(f"{account}:x".encode()).decode()
        return head + "resttoken"

    def test_acct_ordered_first(self):
        pat = self._pat_with_account("175128737722")
        cands = JiraGitClient._clone_user_candidates(pat, "hb_1150118968")
        self.assertEqual(cands, ["175128737722", "hb_1150118968"])

    def test_empty_username_dropped(self):
        pat = self._pat_with_account("175128737722")
        self.assertEqual(JiraGitClient._clone_user_candidates(pat, ""), ["175128737722"])
        self.assertEqual(JiraGitClient._clone_user_candidates(pat, "   "), ["175128737722"])

    def test_duplicate_deduped(self):
        pat = self._pat_with_account("175128737722")
        self.assertEqual(
            JiraGitClient._clone_user_candidates(pat, "175128737722"),
            ["175128737722"],
        )

    def test_no_account_and_no_username(self):
        # PAT 前缀无法解出账号，且未配置用户名 -> 候选为空
        self.assertEqual(JiraGitClient._clone_user_candidates("not-a-valid-pat", ""), [])


class TestParseRepoInfo(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()

    def test_parse(self):
        info = self.c._parse_repo_info(REPO_INFO_HTML)
        self.assertEqual(info.get("displayName"), "hcm-cloud-vue")
        self.assertEqual(info.get("repoId"), "1032")
        self.assertEqual(info.get("headCommit"), "deadbeef")

    def test_no_match(self):
        self.assertEqual(self.c._parse_repo_info("no repo info here"), {})


class TestParseTreeFiles(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()

    def test_parse(self):
        files = self.c._parse_tree_files(TREE_HTML)
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["path"], "src")
        self.assertTrue(files[0]["is_dir"])
        self.assertEqual(files[1]["path"], "README.md")
        self.assertFalse(files[1]["is_dir"])
        self.assertEqual(files[1]["size"], 123)

    def test_no_match(self):
        self.assertEqual(self.c._parse_tree_files("no data"), [])


class TestDiscoverNoCookie(unittest.TestCase):
    """无 Cookie 时 discover_repos 直接返回空（不联网）。"""

    def test_returns_empty(self):
        c = JiraGitClient()
        c.config.cookie = ""
        self.assertEqual(c.discover_repos(), [])


# 仿真 GIJViewGitFileContent 三种渲染结构
_PRE_HTML = """
<html><body>
<div class="aui-page-panel-content">
<pre class="bbb-gp-code">#!/usr/bin/env php
&lt;?php
function hi() {
    return "hello";
}
?&gt;</pre>
</div>
</body></html>
"""

_CODECELL_HTML = """
<html><body>
<table>
<code class="bbb-gp-diff_code-cell-content">line1</code>
<code class="bbb-gp-diff_code-cell-content">line2</code>
</body></html>
"""

_GENERIC_HTML = """
<html><body>
<nav>Home Projects Issues</nav>
<div id="main">
  <p>SELECT * FROM users;</p>
  <p>function calc() { return 1; }</p>
  <p>const x = 42;</p>
</div>
</body></html>
"""


class TestExtractCodeFromHtml(unittest.TestCase):
    """验证 _extract_code_from_html 兼容多种页面结构。"""

    def setUp(self):
        self.c = JiraGitClient()

    def test_extract_pre_block(self):
        out = self.c._extract_code_from_html(_PRE_HTML)
        self.assertIsNotNone(out)
        self.assertIn("function hi()", out)
        # HTML 实体应被还原
        self.assertIn("<?php", out)
        self.assertIn("?>", out)

    def test_extract_code_cells(self):
        out = self.c._extract_code_from_html(_CODECELL_HTML)
        self.assertEqual(out, "line1\nline2\n")

    def test_extract_generic_fallback(self):
        # 无 <pre>/<code> 时，兜底抓取“像代码”的行
        out = self.c._extract_code_from_html(_GENERIC_HTML)
        self.assertIsNotNone(out)
        self.assertIn("SELECT * FROM users;", out)
        self.assertIn("function calc()", out)

    def test_extract_empty_page(self):
        # 纯导航页、无代码特征 -> 返回 None（避免返回垃圾）
        self.assertIsNone(self.c._extract_code_from_html(
            "<html><body><nav>Home Projects</nav></body></html>"))


class _FakeResp:
    """最小 httpx.Response 替身，仅提供 _is_login_page / _browse_works 需要的字段。"""
    def __init__(self, url: str, text: str = ""):
        self.url = url
        self.text = text


class TestBranchResolution(unittest.TestCase):
    """空 branch 会被服务器重定向到登录页；list_level/get_file 必须自动探测可用分支。"""

    def setUp(self):
        self.c = JiraGitClient()
        self.c.set_config(ConnectConfig(
            jira_url="https://jira.hcmcloud.cn", cookie="JSESSIONID=dummy",
            mode="cookie"))

    def _fake_browse(self, table):
        """table: {branch: ('login'|'data'|'empty')} 决定 _fetch_browse 的返回。"""
        def fake(repo_id, branch, path=""):
            kind = table.get(branch, "empty")
            if kind == "login":
                return _FakeResp(
                    "https://jira.hcmcloud.cn/login.jsp?permissionViolation=true"
                    "&os_destination=%2Fsecure%2FGIJBrowseGit.jspa%3FrepoId%3D895"
                    f"%26branchName%3D{branch}", "")
            if kind == "data":
                return _FakeResp(
                    f"https://jira.hcmcloud.cn/secure/GIJBrowseGit.jspa?repoId=895"
                    f"&branchName={branch}",
                    'ns.data = {"files": [{"path": "src", "directory": true}]};')
            return _FakeResp(
                f"https://jira.hcmcloud.cn/secure/GIJBrowseGit.jspa?repoId=895"
                f"&branchName={branch}", "no tree here")
        self.c._fetch_browse = fake

    def test_is_login_page(self):
        self.assertTrue(self.c._is_login_page(
            _FakeResp("https://jira.hcmcloud.cn/login.jsp?permissionViolation=true")))
        self.assertFalse(self.c._is_login_page(
            _FakeResp("https://jira.hcmcloud.cn/secure/GIJBrowseGit.jspa?repoId=895&branchName=master")))

    def test_resolve_keeps_working_branch(self):
        # 给定 branch 可用 -> 直接用，不回退
        self._fake_browse({"master": "data", "main": "data", "develop": "data"})
        self.assertEqual(self.c._resolve_branch("895", "master"), "master")

    def test_resolve_empty_branch_falls_back(self):
        # 空 branch 被踢到登录页 -> 应自动选到第一个可用的 master
        self._fake_browse({"": "login", "master": "data", "main": "login",
                           "develop": "data", "release": "data"})
        self.assertEqual(self.c._resolve_branch("895", ""), "master")

    def test_resolve_given_branch_login_falls_back(self):
        # 给定 branch 返回登录页（无权限）-> 回退到候选中可用的
        self._fake_browse({"release": "login", "master": "data", "main": "login",
                           "develop": "data"})
        self.assertEqual(self.c._resolve_branch("895", "release"), "master")

    def test_resolve_all_fail_returns_given(self):
        # 全部失败 -> 返回给定 branch（由上层继续报错，而非静默空树）
        self._fake_browse({"": "login", "master": "login", "main": "login",
                           "develop": "login", "release": "login", "trunk": "login"})
        self.assertEqual(self.c._resolve_branch("895", ""), "")


class _FakeRespEx(_FakeResp):
    def __init__(self, url, text="", headers=None, status_code=200):
        super().__init__(url, text)
        self.headers = headers or {}
        self.status_code = status_code


class TestRecursiveDownload(unittest.TestCase):
    """Cookie 模式应支持嵌套目录列取与整库递归下载（破除“仅根目录”旧限制）。"""

    def setUp(self):
        self.c = JiraGitClient()
        self.c.set_config(ConnectConfig(
            jira_url="https://jira.hcmcloud.cn", cookie="JSESSIONID=dummy",
            mode="cookie"))

    def test_list_dir_nested(self):
        # 子目录浏览应返回带完整路径的条目（目录 + 文件）
        def fake_fetch(repo_id, branch, path=""):
            return _FakeResp(
                f"https://jira.hcmcloud.cn/secure/GIJBrowseGit.jspa?repoId={repo_id}"
                f"&branchName={branch}&path={path}",
                'ns.data = {"files": ['
                '{"path": "core/access", "directory": true, "size": null},'
                '{"path": "core/__init__.py", "directory": false, "size": 10}'
                ']};')
        self.c._fetch_browse = fake_fetch
        entries = self.c._list_dir("895", "master", "core")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].path, "core/access")
        self.assertEqual(entries[0].type, "dir")
        self.assertEqual(entries[1].path, "core/__init__.py")
        self.assertEqual(entries[1].type, "file")

    def test_cookie_file_content_nested_via_rest(self):
        # 嵌套文件不应被拦截，应经 REST 取回正文
        def fake_http(url, headers=None):
            return _FakeRespEx(
                url,
                text="__author__ = 'x'\n",
                headers={"content-type": "text/plain;charset=UTF-8"},
                status_code=200)
        self.c.http_get = fake_http
        self.c.branch = "master"
        ok, content, note = self.c._cookie_file_content("895", "deadbeef", "core/__init__.py")
        self.assertTrue(ok)
        self.assertIn("__author__", content)
        self.assertEqual(note, "")

    def test_download_repo_recursive(self):
        import tempfile, os
        # 构造一棵小树：根有 README.md + core/（含 core/x.py）
        tree = {
            "": [TreeEntry("README.md", "README.md", "file", 3, False),
                 TreeEntry("core", "core", "dir", None, True)],
            "core": [TreeEntry("x.py", "core/x.py", "file", 5, False)],
        }

        def fake_resolve(rid, branch):
            return "master"

        def fake_fetch(repo_id, branch, path=""):
            # 仅用于取 headCommit
            return _FakeResp(
                "https://jira.hcmcloud.cn/secure/GIJBrowseGit.jspa",
                'ns.repoInfo = {"id": "895", "lastCommit": {"name": "deadbeef"}};')

        def fake_list(repo_id, branch, path=""):
            return tree.get(path, [])

        def fake_content(rid, head, path):
            return True, f"# content of {path}\n", ""

        self.c._resolve_branch = fake_resolve
        self.c._fetch_browse = fake_fetch
        self.c._list_dir = fake_list
        self.c._cookie_file_content = fake_content

        tmp = tempfile.mkdtemp()
        try:
            ok_count, fail, dest = self.c.download_repo("895", "", tmp)
            self.assertEqual(ok_count, 2)
            self.assertEqual(fail, [])
            self.assertTrue(os.path.exists(os.path.join(tmp, "README.md")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "core", "x.py")))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
