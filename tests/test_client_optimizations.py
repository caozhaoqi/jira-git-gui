"""优化点单元测试（全部离线，网络/子进程均 mock）。

覆盖：
- 二进制文件下载：Cookie 模式按 content-type / 启发式区分文本与二进制，二进制走字节落盘；
  get_file 对二进制返回友好提示而非乱码。
- 分支探测缓存：同一 repo_id 只在首次真正探测，后续命中缓存（不发 HTTP）。
- PAT 轻量连通测试：git ls-remote 成功/被拒/缺 username 三种分支。
- 并行下载：有界线程池确实并发抓取；cancel 能在并行中途停止；max_workers=1 退化为串行。
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.client import JiraGitClient
from core.models import ConnectConfig, TreeEntry


def _make_client():
    cfg = ConnectConfig()
    cfg.cookie = "DUMMY_SESSION=1"
    cfg.jira_url = "https://example.com"
    cfg.mode = "cookie"
    c = JiraGitClient()
    c.set_config(cfg)
    c.set_repo("895", "repo/demo", "")
    return c


class TestBinaryDownload(unittest.TestCase):
    def setUp(self):
        self.c = _make_client()
        self.c._resolve_branch = lambda rid, b: b or "master"
        fake_resp = mock.MagicMock()
        fake_resp.text = ""
        self.c._fetch_browse = lambda *a, **k: fake_resp
        self.c._parse_repo_info = lambda *a, **k: {"headCommit": "HEAD123"}

    def test_text_file_written_as_text(self):
        self.c._cookie_file_content = (
            lambda rid, head, path, client=None: (True, "hello world", ""))
        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped, _ = self.c._download_files(
                "895", "", d, ["a.txt"], manifest={})
            self.assertEqual(ok, 1)
            self.assertEqual((Path(dest) / "a.txt").read_text(), "hello world")

    def test_binary_file_written_as_bytes(self):
        png = bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x00, 0x01, 0x00])
        self.c._cookie_file_content = (
            lambda rid, head, path, client=None: (True, png, ""))
        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped, _ = self.c._download_files(
                "895", "", d, ["img.png"], manifest={})
            self.assertEqual(ok, 1)
            raw = (Path(dest) / "img.png").read_bytes()
            self.assertEqual(raw, png)  # 字节级一致，未被 utf-8 破坏

    def test_get_file_returns_note_for_binary(self):
        png = bytes([0x89, 0x50, 0x4E, 0x47, 0x00])
        self.c._cookie_file_content = (
            lambda rid, head, path, client=None: (True, png, ""))
        content, err = self.c.get_file("img.png")
        self.assertIsNone(content)
        self.assertIn("二进制", err)

    def test_get_file_returns_text(self):
        self.c._cookie_file_content = (
            lambda rid, head, path, client=None: (True, "print('hi')", ""))
        content, err = self.c.get_file("x.py")
        self.assertEqual(content, "print('hi')")
        self.assertIsNone(err)


class TestBranchCache(unittest.TestCase):
    def test_resolve_branch_caches_probe(self):
        c = _make_client()
        calls = {"n": 0}

        def fake_has_tree(rid, branch):
            calls["n"] += 1
            return branch == "master"

        c._browse_has_tree = fake_has_tree
        # 首次：探测 master（命中），缓存 "master"
        self.assertEqual(c._resolve_branch("895", ""), "master")
        # 再次：应直接命中缓存，不再探测
        self.assertEqual(c._resolve_branch("895", ""), "master")
        self.assertEqual(calls["n"], 1)

    def test_set_repo_clears_cache(self):
        c = _make_client()
        c._branch_cache["895"] = "master"
        c.set_repo("1022", "other", "")
        self.assertNotIn("895", c._branch_cache)


class TestPatQuickTest(unittest.TestCase):
    def setUp(self):
        self.c = _make_client()
        self.c.set_repo("895", "repo/demo", "master")
        self.c.config.pat = "dXNlcjEyMzpzZWNyZXQxMjM="  # base64("user123:secret123")

    def _ls_remote(self, returncode, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_quick_ok(self):
        with mock.patch.object(subprocess, "run",
                              return_value=self._ls_remote(0, "abc refs/heads/master\n")):
            ok, msg = self.c._pat_test_quick(self.c.config.pat, "user123")
        self.assertTrue(ok)
        self.assertIn("认证通过", msg)

    def test_quick_rejected(self):
        with mock.patch.object(subprocess, "run", return_value=self._ls_remote(
                128, stderr="remote: Invalid username or password\n")):
            ok, msg = self.c._pat_test_quick(self.c.config.pat, "user123")
        self.assertFalse(ok)
        self.assertIn("认证被服务器拒绝", msg)

    def test_quick_missing_username(self):
        # 使用一个不内嵌账号（解码后不含 ':'）的 PAT，且用户名留空 -> 直接报缺 username
        with mock.patch.object(subprocess, "run", return_value=self._ls_remote(0)):
            ok, msg = self.c._pat_test_quick("Zm9vYmFy", "")
        self.assertFalse(ok)
        self.assertIn("username", msg)

    def test_connect_uses_quick_test(self):
        with mock.patch.object(subprocess, "run",
                              return_value=self._ls_remote(0, "x refs/heads/master\n")):
            res = self.c.connect()
        self.assertIsNotNone(res.get("patTest"))
        self.assertTrue(res["patTest"]["ok"])


class TestParallelDownload(unittest.TestCase):
    def setUp(self):
        self.c = _make_client()
        self.c._resolve_branch = lambda rid, b: b or "master"
        fake_resp = mock.MagicMock()
        fake_resp.text = ""
        self.c._fetch_browse = lambda *a, **k: fake_resp
        self.c._parse_repo_info = lambda *a, **k: {"headCommit": "HEAD123"}
        self.tree = {
            "": [TreeEntry(f"f{i}.txt", f"f{i}.txt", "file", 3, False)
                 for i in range(12)],
        }
        self.c._list_dir = lambda rid, branch, path="": self.tree.get(path, [])

    def test_parallel_runs_concurrently(self):
        state = {"active": 0, "max": 0}

        def slow(rid, head, path, client=None):
            # 在“网络耗时”期间保持 active 计数，便于观测并发窗口
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            import time
            time.sleep(0.05)
            state["active"] -= 1
            return (True, f"data-{path}", "")

        self.c._cookie_file_content = slow
        with tempfile.TemporaryDirectory() as d:
            # 12 文件、并发 4，sleep 期间应有 >1 个任务同时在跑
            ok, fails, dest, skipped, _ = self.c._download_files(
                "895", "", d, [f"f{i}.txt" for i in range(12)],
                manifest={}, max_workers=4)
            self.assertEqual(ok, 12)
            self.assertGreater(state["max"], 1)  # 确实并发 > 1

    def test_parallel_cancel_stops(self):
        state = {"n": 0}

        def counting(rid, head, path, client=None):
            import time
            time.sleep(0.02)  # 模拟网络耗时，使取消有机会在中途介入
            state["n"] += 1
            return (True, f"data-{path}", "")

        self.c._cookie_file_content = counting

        def should_cancel():
            return state["n"] >= 2

        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped, _ = self.c._download_files(
                "895", "", d, [f"f{i}.txt" for i in range(12)],
                should_cancel=should_cancel, manifest={}, max_workers=4)
            self.assertLess(ok, 12)

    def test_serial_when_max_workers_one(self):
        order = []

        def counting(rid, head, path, client=None):
            order.append(path)
            return (True, f"data-{path}", "")

        self.c._cookie_file_content = counting
        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped, _ = self.c._download_files(
                "895", "", d, [f"f{i}.txt" for i in range(6)],
                manifest={}, max_workers=1)
            self.assertEqual(ok, 6)


class TestDownloadClientReuse(unittest.TestCase):
    """守护优化：整批下载只创建并复用【一个】httpx 客户端，而非每文件新建。"""
    def setUp(self):
        self.c = _make_client()
        self.c._resolve_branch = lambda rid, b: b or "master"
        fake_resp = mock.MagicMock()
        fake_resp.text = ""
        self.c._fetch_browse = lambda *a, **k: fake_resp
        self.c._parse_repo_info = lambda *a, **k: {"headCommit": "HEAD123"}
        self.c._list_dir = lambda rid, branch, path="": [
            TreeEntry(f"f{i}.txt", f"f{i}.txt", "file", 1, False) for i in range(5)
        ]
        self.seen = []
        self.c._cookie_file_content = (
            lambda rid, head, path, client=None:
                (self.seen.append(client) or (True, "x", ""))
        )

    def test_single_client_per_batch(self):
        made = {"n": 0}
        fake_client = object()

        def make():
            made["n"] += 1
            return fake_client

        self.c._make_client = make
        with tempfile.TemporaryDirectory() as d:
            self.c._download_files(
                "895", "", d, [f"f{i}.txt" for i in range(5)],
                manifest={}, max_workers=4)
        self.assertEqual(made["n"], 1)  # 整批只建一个 client
        self.assertTrue(all(c is fake_client for c in self.seen))  # 全部复用


class TestHeadCache(unittest.TestCase):
    """守护优化：(repo_id, branch) -> HEAD commit 缓存，避免重复解析。"""
    def test_resolve_head_caches_and_cleared_on_repo_switch(self):
        c = _make_client()
        c.set_repo("895", "repo", "master")
        calls = {"n": 0}

        def fake_browse(rid, branch, path=""):
            calls["n"] += 1
            r = mock.MagicMock()
            r.text = ""
            return r

        c._fetch_browse = fake_browse
        c._parse_repo_info = lambda *a, **k: {"headCommit": "abc123"}
        self.assertEqual(c._resolve_head("895", "master"), "abc123")
        self.assertEqual(c._resolve_head("895", "master"), "abc123")
        self.assertEqual(calls["n"], 1)  # 命中缓存后不再 browse
        # 切换仓库清空缓存
        c.set_repo("1022", "other", "")
        c._resolve_head("1022", "")
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
