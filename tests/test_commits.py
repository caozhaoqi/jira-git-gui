"""单元测试：提交记录拉取（Jira Git 插件 REST 解析，不联网）。

覆盖（与新 client 接口对齐：get_commits(repo_id, branch, ...) 仓库/分支模式）：
- 仓库/分支普通查询：读 REST ``commits`` 端点的 ``data["values"]``
- 带文件清单（showFiles 语义）：files[].changeType/linesAdded/linesRemoved
- 非 200 / 登录页 / 空 values -> 优雅返回 []（不再抛 UserError）
- 无 cookie -> 返回 []
- PAT 模式无本地克隆 -> 返回 []
- _parse_commit 字段映射
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.client import JiraGitClient
from core.models import Commit, ConnectConfig


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
        self.c.set_config(ConnectConfig(
            jira_url="https://jira.example.com", cookie="JSESSIONID=abc"))

    def _values_payload(self, commits):
        return {"values": commits}

    # ---- 1) 仓库/分支 普通查询 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_basic(self, http_get):
        http_get.return_value = _Resp(json_data=self._values_payload([
            {"commitId": "34efa20372f0e2f0c9b705aacc57d7ad82e01426",
             "author": "msmith ", "date": "2015-05-18T10:52:54.000+0000",
             "message": "TST-234 update", "branch": "master",
             "repository": {"id": 5, "name": "repo1"}},
        ]))
        out = self.c.get_commits("895", "master")
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], Commit)
        self.assertEqual(out[0].display_id, "34efa203")
        self.assertEqual(out[0].author, "msmith")
        self.assertEqual(out[0].repository_name, "repo1")

    # ---- 2) 带文件清单（showFiles 语义）----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_showfiles(self, http_get):
        http_get.return_value = _Resp(json_data=self._values_payload([
            {"commitId": "c0a5c6a6c942e95d554326fa5265c4e0ba7e2f9a",
             "author": "John Smith ", "date": "2023-05-30T21:31:25+0700",
             "message": "TEST-2 commit B",
             "files": [{"path": "README.md", "changeType": "MODIFIED",
                        "linesAdded": 2, "linesRemoved": 1}]},
        ]))
        out = self.c.get_commits("895", "master")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].files), 1)
        f = out[0].files[0]
        self.assertEqual(f.path, "README.md")
        self.assertEqual(f.change_type, "MODIFIED")
        self.assertEqual(f.lines_added, 2)
        self.assertEqual(f.lines_removed, 1)

    # ---- 3) 非 200 -> 优雅返回 []（不抛错）----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_non_200_returns_empty(self, http_get):
        http_get.return_value = _Resp(status=403, json_data={})
        self.assertEqual(self.c.get_commits("895", "master"), [])

    # ---- 4) 登录页（非 JSON）-> 优雅返回 [] ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_login_page_returns_empty(self, http_get):
        # 登录页返回 200 但体为 HTML，json() 解析失败 -> 视为无数据，返回 []
        http_get.return_value = _Resp(status=200, json_data=None,
                                      url="http://jira/login.jsp?permissionViolation=true")
        self.assertEqual(self.c.get_commits("895", "master"), [])

    # ---- 5) 空 values -> 空列表 ----
    @mock.patch.object(JiraGitClient, "http_get")
    def test_empty_values_returns_empty(self, http_get):
        http_get.return_value = _Resp(json_data={"values": []})
        self.assertEqual(self.c.get_commits("895", "master"), [])

    # ---- 6) 无 cookie / 无可用模式 -> 返回 [] ----
    def test_no_cookie_returns_empty(self):
        c = JiraGitClient()  # 空配置
        self.assertEqual(c.get_commits("895", "master"), [])

    # ---- 7) PAT 模式无本地克隆 -> 路由到本地 git log，返回 [] ----
    @mock.patch("core.client.browse.REPOS_DIR", Path(tempfile.mkdtemp()))
    def test_pat_mode_no_local_clone_returns_empty(self):
        c = JiraGitClient()
        c.set_config(ConnectConfig(mode="pat", cookie=""))
        # 临时 REPOS_DIR 下不存在 895 的本地克隆 -> 走 get_local_commits 返回 []
        self.assertEqual(c.get_commits("895", "master"), [])

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
