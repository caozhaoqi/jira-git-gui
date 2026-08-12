"""本地 Git 模式 + 历史版本文件查看 的单元测试。

覆盖：
- _parse_git_log 解析（含 RENAME 取新路径、变更类型归一）
- get_local_commits 真实跑 git log（临时仓库）
- get_file_at_commit 走本地 `git show` 取历史版本
- 未克隆时 get_local_commits 抛错
- Cookie 模式回退：本地不存在时调用 _cookie_file_content
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.client import JiraGitClient
from core.models import ConnectConfig


def _init_repo(path: Path) -> str:
    """创建一个临时 git 仓库，提交 a.txt，返回其首次提交 SHA。"""
    g = "git"
    subprocess.run([g, "-C", str(path), "init", "-q"], check=True)
    subprocess.run([g, "-C", str(path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run([g, "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "a.txt").write_text("hello world\n")
    subprocess.run([g, "-C", str(path), "add", "."], check=True)
    subprocess.run([g, "-C", str(path), "commit", "-q", "-m", "init commit"], check=True)
    out = subprocess.run([g, "-C", str(path), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


class LocalCommitsTest(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()
        self.c.set_config(ConnectConfig(
            jira_url="https://jira.example.com", cookie="JSESSIONID=abc"))

    def test_parse_git_log_basic(self):
        raw = ("\x1e" + "1111111111111111111111111111111111111111\x1f"
               "alice\x1falice@x\x1f2024-01-01 10:00:00 +0800\x1f"
               "first msg\n"
               "M\ta/b.py\n"
               "A\tnew.txt\n"
               "\x1e" + "2222222222222222222222222222222222222222\x1f"
               "bob\x1fbob@x\x1f2024-01-02 11:00:00 +0800\x1f"
               "rename msg\n"
               "R100\told.py\tnew.py\n")
        commits = JiraGitClient._parse_git_log(raw)
        self.assertEqual(len(commits), 2)
        c0 = commits[0]
        self.assertEqual(c0.commit_id, "1111111111111111111111111111111111111111")
        self.assertEqual(c0.display_id, "11111111")
        self.assertEqual(c0.author, "alice")
        self.assertEqual(c0.message, "first msg")
        self.assertEqual(len(c0.files), 2)
        self.assertEqual(c0.files[0].change_type, "M")
        self.assertEqual(c0.files[0].path, "a/b.py")
        # RENAME：change_type 归一为 R，path 取新路径
        c1 = commits[1]
        self.assertEqual(c1.files[0].change_type, "R")
        self.assertEqual(c1.files[0].path, "new.py")

    def test_parse_git_log_empty(self):
        self.assertEqual(JiraGitClient._parse_git_log(""), [])
        self.assertEqual(JiraGitClient._parse_git_log("\x1e\n"), [])

    def test_no_local_clone_raises(self):
        from core.errors import UserError
        with mock.patch("core.client.REPOS_DIR", Path("/no/such/dir/xyz")):
            with self.assertRaises(UserError):
                self.c.get_local_commits("895")

    def test_get_local_commits_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            repo_path = Path(d) / "895"
            repo_path.mkdir()
            sha = _init_repo(repo_path)
            with mock.patch("core.client.REPOS_DIR", Path(d)):
                commits = self.c.get_local_commits("895")
                self.assertTrue(len(commits) >= 1)
                self.assertIn("init commit", commits[0].message)
                self.assertEqual(commits[0].files[0].path, "a.txt")
                # 历史版本内容
                content, err = self.c.get_file_at_commit("895", sha, "a.txt")
                self.assertIsNone(err)
                self.assertIn("hello world", content)

    def test_get_file_at_commit_cookie_fallback(self):
        # 本地无克隆 -> 回退到 Cookie 模式（mock _cookie_file_content）
        with mock.patch("core.client.REPOS_DIR", Path("/no/such/dir/xyz")):
            captured = {}

            def fake_content(repo_id, commit_id, path, client=None):
                captured["commit_id"] = commit_id
                return (True, f"content-of-{path}@{commit_id}", "")

            self.c._cookie_file_content = fake_content
            content, err = self.c.get_file_at_commit("895",
                                                     "deadbeef" * 5, "x.py")
            self.assertIsNone(err)
            self.assertEqual(content, "content-of-x.py@deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
            self.assertEqual(captured["commit_id"], "deadbeef" * 5)


if __name__ == "__main__":
    unittest.main()
