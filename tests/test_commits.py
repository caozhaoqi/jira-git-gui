"""单元测试：提交记录拉取（Jira Git 插件 REST 解析，不联网）。

覆盖：
- issue 关联查询：普通 commits + showFiles=true（带文件清单）
- 登录页 / 非 200 -> 抛错
- 空 commits -> 空列表
- best-effort 按仓库不被支持 -> 友好提示
- _parse_commit 字段映射
"""
import unittest
from unittest import mock

from core.client import JiraGitClient
from core.models import Commit


class _Resp:
    """最小 httpx.Response 替身。"""
    def __init__(self, status=200, json_data=None, url="http://jira/rest/x",
                 headers=None):
        self.status_code = status
        self._json = json_data
        self.url = url
        self.headers = headers or {}
        self.text = ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class TestGetCommits(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()
        # 直接构造一个带 Cookie 的配置，避免依赖 .env
        from core.models import ConnectConfig
        self.c.set_config(ConnectConfig(
            jira_url="https://jira.example.com", cookie="JSESSIONID=abc"))

    def _issue_payload(self, commits):
        return {"commits": commits}

    def _showfiles_payload(self, commits):
        return {"success": True, "commits": commits}

    # ---- 1) issue 普通查询 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_issue_basic(self, http_get):
        http_get.return_value = _Resp(json_data=self._issue_payload([
            {"commitId": "34efa20372f0e2f0c9b705aacc57d7ad82e01426",
             "author": "msmith ", "date": "2015-05-18T10:52:54.000+0000",
             "message": "TST-234 update", "branch": "master",
             "repository": {"id": 5, "name": "repo1"}},
        ]))
        out = self.c.get_commits("TST-234")
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], Commit)
        self.assertEqual(out[0].display_id, "34efa203")
        self.assertEqual(out[0].author, "msmith")
        self.assertEqual(out[0].repository_name, "repo1")

    # ---- 2) showFiles=true 带文件清单 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_issue_showfiles(self, http_get):
        http_get.return_value = _Resp(json_data=self._showfiles_payload([
            {"commitId": "c0a5c6a6c942e95d554326fa5265c4e0ba7e2f9a",
             "author": "John Smith ", "date": "2023-05-30T21:31:25+0700",
             "message": "TEST-2 commit B",
             "files": [{"path": "README.md", "changeType": "MODIFIED",
                        "linesAdded": 2, "linesRemoved": 1}]},
        ]))
        out = self.c.get_commits("TEST-2")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].files), 1)
        f = out[0].files[0]
        self.assertEqual(f.path, "README.md")
        self.assertEqual(f.change_type, "MODIFIED")
        self.assertEqual(f.lines_added, 2)
        self.assertEqual(f.lines_removed, 1)

    # ---- 3) 登录页 -> 抛错 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_login_page_raises(self, http_get):
        http_get.return_value = _Resp(status=200, json_data={"commits": []},
                                      url="http://jira/login.jsp?permissionViolation=true")
        with self.assertRaises(RuntimeError):
            self.c.get_commits("TST-1")

    # ---- 4) 非 200 -> 抛错 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_non_200_raises(self, http_get):
        http_get.return_value = _Resp(status=403, json_data={})
        with self.assertRaises(RuntimeError):
            self.c.get_commits("TST-1")

    # ---- 5) 空 commits -> 空列表 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_empty_commits(self, http_get):
        http_get.return_value = _Resp(json_data={"commits": []})
        out = self.c.get_commits("TST-9")
        self.assertEqual(out, [])

    # ---- 6) best-effort 按仓库不被支持 -> 友好提示 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_repo_best_effort_unsupported(self, http_get):
        http_get.return_value = _Resp(status=404, json_data={})
        with self.assertRaises(RuntimeError) as ctx:
            self.c.get_commits(None, repo_id="895")
        self.assertIn("issue", str(ctx.exception))

    # ---- 7) 无 cookie / 无输入 -> 抛错 ----
    def test_no_cookie_raises(self):
        c = JiraGitClient()  # 空配置
        with self.assertRaises(RuntimeError):
            c.get_commits("TST-1")

    def test_no_input_raises(self):
        with self.assertRaises(RuntimeError):
            self.c.get_commits(None, repo_id="", branch=None)

    # ---- 8) _parse_commit 字段映射 ----
    def test_parse_commit(self):
        c = JiraGitClient._parse_commit({
            "commitId": "abcdef1234567890",
            "author": "  bob ",
            "date": "2024-01-01T00:00:00+0000",
            "message": "fix bug",
            "branches": ["feature/x"],
            "files": [{"path": "a.py", "changeType": "ADDED",
                       "linesAdded": 10, "linesRemoved": 0}],
        })
        self.assertEqual(c.display_id, "abcdef12")
        self.assertEqual(c.author, "bob")
        self.assertEqual(c.branch, "feature/x")
        self.assertEqual(c.files[0].change_type, "ADDED")
        self.assertEqual(c.files[0].lines_added, 10)


if __name__ == "__main__":
    unittest.main()
