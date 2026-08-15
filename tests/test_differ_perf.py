# -*- coding: utf-8 -*-
"""differ 性能优化回归 + 基准测试。

覆盖：
- scan_local 增量复用（未变文件跳过 MD5）
- compute_diff 集合化实现的等价性与稳定性
- merge_entries 并行合并的正确性与线程安全（_DIR_CACHE 锁）
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import differ
from core.differ import (
    DiffEntry, DiffStatus, scan_local, scan_local_cached,
    compute_diff, merge_entries, merge_to_local, clear_dir_cache,
)


class FakeClient:
    """内存版客户端，模拟远程文件内容；get_file 可被 get_file_cached 调用。"""
    def __init__(self, data):
        self._data = data

    def get_file(self, path):
        return self._data.get(path, "")


def _make_tree(root: Path, n: int, size: int = 64) -> None:
    """生成 n 个文件，内容为可预测的伪随机字节。"""
    for i in range(n):
        p = root / f"sub_{i % 8}" / f"file_{i}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * size + f"\n{i}\n", encoding="utf-8")


class TestScanLocalIncremental(unittest.TestCase):
    def test_incremental_reuses_hash_for_unchanged_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, 200)
            calls = {"n": 0}
            orig = differ._file_hash

            def counting(path):
                calls["n"] += 1
                return orig(path)

            differ._file_hash = counting
            try:
                # 首次全量扫描，必然计算 MD5
                r1 = scan_local(str(root))
                self.assertGreater(calls["n"], 0)
                first_hash_calls = calls["n"]

                # 未改动任何文件，以 r1 为 prev 增量重扫 → 应跳过全部 MD5
                r2 = scan_local(str(root), prev=r1)
                self.assertEqual(calls["n"], first_hash_calls,
                                 "未变文件不应重新计算 MD5")
            finally:
                differ._file_hash = orig

            # 结果必须完全一致（哈希 + 大小 + mtime）
            self.assertEqual(r1, r2)

    def test_incremental_recomputes_changed_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, 50)
            r1 = scan_local(str(root))
            # 修改其中一个文件内容（mtime 必然变化）
            target = root / "sub_0" / "file_0.txt"
            time.sleep(0.01)
            target.write_text("CHANGED CONTENT\n", encoding="utf-8")
            r2 = scan_local(str(root), prev=r1)
            self.assertNotEqual(r1[target.relative_to(root).as_posix().replace("\\", "/")]["hash"],
                                r2[target.relative_to(root).as_posix().replace("\\", "/")]["hash"])

    def test_scan_local_cached_returns_prev_on_expiry(self):
        """缓存过期后以旧缓存为增量基线：结果包含 mtime 字段。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, 30)
            a = scan_local_cached(str(root), tree_ttl=300, use_cache=True)
            self.assertTrue(all("mtime" in v for v in a.values()))


class TestComputeDiffEquivalence(unittest.TestCase):
    def test_matches_reference_implementation(self):
        local = {
            "a.py": {"size": 10, "mtime": 1, "hash": "h1"},
            "b.py": {"size": 20, "mtime": 2, "hash": "h2"},
            "c.py": {"size": 30, "mtime": 3, "hash": "h3"},
            "only_local.txt": {"size": 5, "mtime": 9, "hash": "hl"},
        }
        remote = {
            "a.py": {"size": 10, "mtime": 1, "hash": "h1"},          # same
            "b.py": {"size": 99, "mtime": 2, "hash": "hX"},          # modified (size/hash diff)
            "c.py": {"size": 30, "mtime": 3, "hash": "hDIFF"},       # modified (hash diff, size same)
            "only_remote.txt": {"size": 7, "mtime": 4, "hash": "hr"},
        }

        res = compute_diff(local, remote)

        # 参考实现：逐路径判定（语义须与 compute_diff 一致：双方都有 hash 时只比 hash）
        def ref():
            s_same = s_mod = s_loc = s_rem = 0
            for p in set(local) | set(remote):
                l, r = local.get(p), remote.get(p)
                if l and r:
                    if l.get("hash") and r.get("hash"):
                        same = l["hash"] == r["hash"]
                    else:
                        same = l["size"] == r["size"]
                    if same:
                        s_same += 1
                    else:
                        s_mod += 1
                elif l:
                    s_loc += 1
                else:
                    s_rem += 1
            return s_same, s_mod, s_loc, s_rem

        self.assertEqual((res.same, res.modified, res.local_only, res.remote_only), ref())
        self.assertEqual(res.total, len(res.entries))

    def test_entry_order_deterministic(self):
        local = {f"f{i}.py": {"size": i, "mtime": i, "hash": f"h{i}"} for i in range(50)}
        remote = {f"g{i}.py": {"size": i, "mtime": i, "hash": f"g{i}"} for i in range(50)}
        res = compute_diff(local, remote)
        paths = [e.path for e in res.entries]
        self.assertEqual(paths, sorted(paths))

    def test_whitespace_only_crlf_vs_lf(self):
        """本地 CRLF、远程 LF 且内容语义相同时，应识别为 WHITESPACE_ONLY。"""
        # 模拟 scan_local 已计算的 norm_hash / norm_size
        local = {
            "a.py": {"size": 14, "mtime": 1, "hash": "h1",
                     "norm_hash": "nh1", "norm_size": 12},
        }
        # 远程无 hash，只有 size；norm_size(12) == remote size(12) → 应判定为行尾差异
        remote = {
            "a.py": {"size": 12},
        }
        res = compute_diff(local, remote, ignore_line_endings=True)
        self.assertEqual(res.entries[0].status, DiffStatus.WHITESPACE_ONLY)
        self.assertEqual(res.modified, 1)  # 汇总到 modified 保持 API 兼容
        self.assertEqual(res.summary()["whitespace_only"], 1)

    def test_whitespace_only_disabled(self):
        """关闭 ignore_line_endings 时，CRLF vs LF 仍标记为 MODIFIED。"""
        local = {
            "a.py": {"size": 14, "mtime": 1, "hash": "h1",
                     "norm_hash": "nh1", "norm_size": 12},
        }
        remote = {"a.py": {"size": 12}}
        res = compute_diff(local, remote, ignore_line_endings=False)
        self.assertEqual(res.entries[0].status, DiffStatus.MODIFIED)


class TestLineEndingNormalization(unittest.TestCase):
    def test_normalized_hash_ignores_crlf(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.txt"
            b = Path(td) / "b.txt"
            a.write_bytes(b"line1\r\nline2\r\n")
            b.write_bytes(b"line1\nline2\n")
            self.assertEqual(differ._normalized_hash(a), differ._normalized_hash(b))

    def test_merge_to_local_skips_crlf_only_change(self):
        with tempfile.TemporaryDirectory() as td:
            local = Path(td)
            target = local / "f.txt"
            target.write_bytes(b"line1\r\nline2\r\n")
            ok = merge_to_local(str(local), "f.txt", "line1\nline2\n")
            self.assertTrue(ok)
            # 不应改写文件（仍为 CRLF）
            self.assertEqual(target.read_bytes(), b"line1\r\nline2\r\n")

    def test_merge_to_local_writes_real_change(self):
        with tempfile.TemporaryDirectory() as td:
            local = Path(td)
            target = local / "f.txt"
            target.write_bytes(b"line1\r\nline2\r\n")
            ok = merge_to_local(str(local), "f.txt", "line1\nDIFFERENT\n")
            self.assertTrue(ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "line1\nDIFFERENT\n")


class TestMergeEntriesParallel(unittest.TestCase):
    def test_parallel_merge_all_files(self):
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "local"
            local.mkdir()
            n = 300
            data = {f"d/file_{i}.txt": f"content-{i}" for i in range(n)}
            entries = [DiffEntry(path=p, status=DiffStatus.REMOTE_ONLY) for p in data]
            client = FakeClient(data)
            clear_dir_cache()
            ok, fail, merged, failed = merge_entries(
                str(local), entries, client, "ns",
                max_workers=8, use_cache=False,
            )
            self.assertEqual(fail, 0)
            self.assertEqual(ok, n)
            self.assertEqual(len(merged), n)
            # 所有文件实际落盘
            for p in data:
                self.assertTrue((local / p).is_file())

    def test_dir_cache_thread_safe_under_concurrency(self):
        """高并发 merge_to_local 不应因 _DIR_CACHE 裸 set 竞争而崩溃或漏建目录。"""
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "local"
            local.mkdir()
            # 300 个不同父目录，确保 _DIR_CACHE 写入路径被密集触发
            def worker(i):
                merge_to_local(str(local), f"dir_{i}/f.txt", f"v{i}")
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(300)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for i in range(300):
                self.assertTrue((local / f"dir_{i}" / "f.txt").is_file())

    def test_parallel_faster_than_sequential_on_slow_fetch(self):
        """慢抓取（模拟网络）下，并行应显著快于串行。"""
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "local"
            local.mkdir()
            n = 80
            data = {f"f_{i}.txt": f"c{i}" for i in range(n)}
            client = FakeClient(data)
            entries = [DiffEntry(path=p, status=DiffStatus.REMOTE_ONLY) for p in data]

            def _run(workers):
                clear_dir_cache()
                t0 = time.time()
                merge_entries(str(local), entries, client, "ns2",
                              max_workers=workers, use_cache=False)
                return time.time() - t0

            # 用真实文件写入模拟 I/O；为放大并发差异，给 FakeClient 加 fetch 延迟
            class SlowClient(FakeClient):
                def get_file(self, path):
                    time.sleep(0.005)
                    return self._data.get(path, "")

            slow = SlowClient(data)
            clear_dir_cache()
            t_seq = time.time()
            merge_entries(str(local), entries, slow, "ns3", max_workers=1, use_cache=False)
            t_seq = time.time() - t_seq

            clear_dir_cache()
            t_par = time.time()
            merge_entries(str(local), entries, slow, "ns4", max_workers=8, use_cache=False)
            t_par = time.time() - t_par

            # 80 * 5ms = 400ms 串行；并发 8 应在 ~ (400/8 + 余量) 内完成
            self.assertLess(t_par, t_seq * 0.6,
                             f"并行({t_par:.2f}s) 应明显快于串行({t_seq:.2f}s)")


if __name__ == "__main__":
    unittest.main()
