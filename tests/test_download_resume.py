"""下载功能单元测试：断点续传 / 进度回调 / 可取消（全部离线，网络均 mock）。

覆盖：
- 断点续传清单的读写
- 整棵树文件枚举（含嵌套目录）
- 批量下载落盘 + 清单写入
- 断点续传：已存在且大小一致的文件被跳过、不再请求网络
- download_repo 编排：进度回调的 (done,total) 正确
- 取消（should_cancel）能在下载中途停止
"""
import os
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


class TestManifest(unittest.TestCase):
    def test_manifest_roundtrip(self):
        c = _make_client()
        with tempfile.TemporaryDirectory() as d:
            c._save_manifest(d, {"a.txt": 10, "b/c.txt": 20})
            loaded = c._load_manifest(d)
            self.assertEqual(loaded, {"a.txt": 10, "b/c.txt": 20})

    def test_load_missing_manifest_returns_empty(self):
        c = _make_client()
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(c._load_manifest(d), {})


class TestWalk(unittest.TestCase):
    def setUp(self):
        self.c = _make_client()
        # 模拟目录结构：
        # ""  -> [dir core, file README.md, file setup.py]
        # core -> [file core/a.py, file core/b.py, dir core/sub]
        # core/sub -> [file core/sub/c.py]
        self.tree = {
            "": [
                TreeEntry("core", "core", "dir", None, True),
                TreeEntry("README.md", "README.md", "file", 5, False),
                TreeEntry("setup.py", "setup.py", "file", 7, False),
            ],
            "core": [
                TreeEntry("a.py", "core/a.py", "file", 3, False),
                TreeEntry("b.py", "core/b.py", "file", 3, False),
                TreeEntry("sub", "core/sub", "dir", None, True),
            ],
            "core/sub": [
                TreeEntry("c.py", "core/sub/c.py", "file", 4, False),
            ],
        }
        self.c._list_dir = lambda rid, branch, path="": self.tree.get(path, [])
        self.c._resolve_branch = lambda rid, b: b or "master"

    def test_walk_collects_all_files(self):
        files = self.c._walk_all_files("895", "")
        self.assertCountEqual(
            files,
            ["README.md", "setup.py", "core/a.py", "core/b.py", "core/sub/c.py"],
        )

    def test_walk_respects_cancel(self):
        calls = {"n": 0}

        def fake_list(rid, branch, path=""):
            calls["n"] += 1
            if calls["n"] > 1:  # 列完根目录后取消
                self.c._cancel_flag = True
            return self.tree.get(path, [])

        self.c._list_dir = fake_list
        self.c._cancel_flag = False
        self.c._walk_all_files("895", "", should_cancel=lambda: self.c._cancel_flag)
        self.assertLess(calls["n"], 5)  # 后续目录未被枚举


class TestDownloadFiles(unittest.TestCase):
    def setUp(self):
        self.c = _make_client()
        self.c._resolve_branch = lambda rid, b: b or "master"
        # head commit 探测
        fake_resp = mock.MagicMock()
        fake_resp.text = ""
        self.c._fetch_browse = lambda *a, **k: fake_resp
        self.c._parse_repo_info = lambda *a, **k: {"headCommit": "HEAD123"}

    def _fake_list(self, files_by_dir):
        self.c._list_dir = lambda rid, branch, path="": files_by_dir.get(path, [])

    def _fake_content(self, contents):
        # contents: dict path -> text
        self.c._cookie_file_content = (
            lambda rid, head, path: (True, contents.get(path, ""), "")
        )

    def test_download_writes_files_and_manifest(self):
        files_by_dir = {
            "": [TreeEntry("README.md", "README.md", "file", 5, False),
                 TreeEntry("setup.py", "setup.py", "file", 7, False)],
        }
        self._fake_list(files_by_dir)
        self._fake_content({"README.md": "hello", "setup.py": "world"})

        with tempfile.TemporaryDirectory() as d:
            manifest = {}
            ok, fails, dest, skipped, ok_paths = self.c._download_files(
                "895", "", d, ["README.md", "setup.py"], manifest=manifest)
            self.assertEqual(ok, 2)
            self.assertEqual(skipped, 0)
            self.assertEqual(fails, [])
            self.assertEqual(sorted(ok_paths), ["README.md", "setup.py"])
            self.assertTrue((Path(dest) / "README.md").read_text() == "hello")
            self.assertTrue((Path(dest) / "setup.py").read_text() == "world")
            # 清单已记录且已落地到磁盘（记录的是实际落盘字节数）
            self.assertEqual(manifest, {"README.md": 5, "setup.py": 5})
            self.assertEqual(self.c._load_manifest(d),
                             {"README.md": 5, "setup.py": 5})

    def test_resume_skips_existing_same_size(self):
        files_by_dir = {
            "": [TreeEntry("README.md", "README.md", "file", 5, False)],
        }
        self._fake_list(files_by_dir)
        contents = {"README.md": "hello"}
        self.c._cookie_file_content = (
            lambda rid, head, path: (True, contents.get(path, ""), "")
        )

        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            # 第一次：下载并写入清单（manifest 必须传入才会落盘）
            self.c._download_files("895", "", dest, ["README.md"], manifest={})
            # 第二次：载入清单后再次下载，应跳过、不再请求网络
            with mock.patch.object(self.c, "_cookie_file_content") as m:
                man = self.c._load_manifest(dest)
                ok, fails, dest2, skipped, ok_paths = self.c._download_files(
                    "895", "", dest, ["README.md"], manifest=man)
                self.assertEqual(skipped, 1)
                self.assertEqual(ok, 0)
                self.assertEqual(ok_paths, ["README.md"])
                m.assert_not_called()

    def test_cancel_stops_midway(self):
        files_by_dir = {
            "": [TreeEntry("a.txt", "a.txt", "file", 1, False),
                 TreeEntry("b.txt", "b.txt", "file", 1, False),
                 TreeEntry("c.txt", "c.txt", "file", 1, False)],
        }
        self._fake_list(files_by_dir)
        self._fake_content({"a.txt": "A", "b.txt": "B", "c.txt": "C"})

        state = {"n": 0}

        def should_cancel():
            # 下载 1 个后取消
            return state["n"] >= 1

        orig = self.c._cookie_file_content
        def counting(rid, head, path):
            state["n"] += 1
            return orig(rid, head, path)

        self.c._cookie_file_content = counting

        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped, ok_paths = self.c._download_files(
                "895", "", d, ["a.txt", "b.txt", "c.txt"],
                should_cancel=should_cancel, max_workers=1)
            self.assertLess(ok, 3)  # 中途停止
            # 取消后落盘的文件数不应超过已成功数（串行下恰好 1 个）
            existing = [f for f in ("a.txt", "b.txt", "c.txt")
                        if (Path(dest) / f).exists()]
            self.assertLessEqual(len(existing), ok + skipped)


class TestDownloadRepoOrchestration(unittest.TestCase):
    def setUp(self):
        self.c = _make_client()
        self.c._resolve_branch = lambda rid, b: b or "master"
        fake_resp = mock.MagicMock()
        fake_resp.text = ""
        self.c._fetch_browse = lambda *a, **k: fake_resp
        self.c._parse_repo_info = lambda *a, **k: {"headCommit": "HEAD123"}
        self.tree = {
            "": [TreeEntry("README.md", "README.md", "file", 5, False),
                 TreeEntry("core", "core", "dir", None, True)],
            "core": [TreeEntry("a.py", "core/a.py", "file", 3, False)],
        }
        self.c._list_dir = lambda rid, branch, path="": self.tree.get(path, [])
        self.c._cookie_file_content = (
            lambda rid, head, path: (True, f"content-of-{path}", "")
        )

    def test_download_repo_progress_and_counts(self):
        progress = []
        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped = self.c.download_repo(
                "895", "", d,
                on_progress=lambda dn, tot, p: progress.append((dn, tot, p)))
            self.assertEqual(ok, 2)
            self.assertEqual(fails, [])
            # 进度单调推进，且末尾 done==total
            self.assertTrue(progress)
            self.assertEqual(progress[-1][0], progress[-1][1])
            self.assertEqual(progress[-1][1], 2)

    def test_download_repo_cancel_partial(self):
        progress = []
        state = {"n": 0}
        orig = self.c._cookie_file_content

        def counting(rid, head, path):
            state["n"] += 1
            return orig(rid, head, path)

        self.c._cookie_file_content = counting
        with tempfile.TemporaryDirectory() as d:
            ok, fails, dest, skipped = self.c.download_repo(
                "895", "", d,
                on_progress=lambda dn, tot, p: progress.append((dn, tot, p)),
                should_cancel=lambda: state["n"] >= 1)
            self.assertLess(ok, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
