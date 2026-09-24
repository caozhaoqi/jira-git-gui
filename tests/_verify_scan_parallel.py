# -*- coding: utf-8 -*-
"""scan_remote_parallel 新版（递归并发 + 细粒度进度）离线验证。

不依赖 pytest：直接 python tests/_verify_scan_parallel.py 运行，断言失败即报错退出。
"""
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.diff.scan_remote import scan_remote, scan_remote_parallel  # noqa: E402


class _Entry:
    def __init__(self, path, type_, size):
        self.path = path
        self.type = type_
        self.size = size


# 一棵有深度的树：树根 a/ 下再嵌套，制造「深层串行」场景
TREE = {
    "": [
        _Entry("a", "dir", 0), _Entry("b", "dir", 0),
        _Entry("root.txt", "file", 10),
    ],
    "a": [_Entry("a/c", "dir", 0), _Entry("a/af.txt", "file", 11)],
    "b": [_Entry("b/d", "dir", 0), _Entry("b/bf.txt", "file", 12)],
    "a/c": [_Entry("a/c/e", "dir", 0), _Entry("a/c/cf.txt", "file", 13)],
    "b/d": [_Entry("b/d/df.txt", "file", 14)],
    "a/c/e": [_Entry("a/c/e/ef.txt", "file", 15)],
}


class _StubClient:
    def __init__(self):
        self.repo_id = "895"
        self.branch = "main"
        self.get_file_calls = []
        self.list_calls = 0
        self.lock = threading.Lock()

    def list_level(self, repo_id, branch, path):
        assert repo_id == "895" and branch == "main"
        with self.lock:
            self.list_calls += 1
        return TREE.get(path, [])

    def get_file(self, path):
        with self.lock:
            self.get_file_calls.append(path)
        return ("body-" + path, None)


def _assert(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("  ok:", msg)


print("== 1. 并发结果 == 串行结果（精确模式）==")
c = _StubClient()
par = scan_remote_parallel(c, max_workers=4, path="", fast_hash=False)
c_ser = _StubClient()  # 串行对照用独立 client，避免 get_file_calls 串扰
ser = scan_remote(c_ser, path="", fast_hash=False)
_assert(set(par.keys()) == set(ser.keys()),
        "并发与串行扫描出的文件集合一致: %r" % sorted(par.keys()))
for k in par:
    _assert(par[k]["hash"] == ser[k]["hash"] and par[k]["size"] == ser[k]["size"],
            "文件 %s 的 size/hash 一致" % k)
# 并发下 get_file 调用顺序不确定，只校验「集合 + 次数」正确
expected_calls = {"root.txt", "a/af.txt", "b/bf.txt",
                 "a/c/cf.txt", "b/d/df.txt", "a/c/e/ef.txt"}
_assert(set(c.get_file_calls) == expected_calls,
        "精确模式逐文件下载且集合正确: %r" % sorted(c.get_file_calls))
_assert(len(c.get_file_calls) == len(expected_calls),
        "每个文件恰好下载一次（无重复）: %d" % len(c.get_file_calls))

print("== 2. 快扫不下载内容（并发下仍成立）==")
c2 = _StubClient()
par2 = scan_remote_parallel(c2, max_workers=4, path="", fast_hash=True)
_assert(c2.get_file_calls == [], "快扫并发模式绝不下内容: %r" % c2.get_file_calls)
_assert(all(m["hash"] == "" for m in par2.values()), "快扫 hash 全为空")

print("== 3. 细粒度进度：单调递增 + 终态 done==discovered ==")
c3 = _StubClient()
progress = []
def _on(scanned, pending, processed, dirs_seen):
    progress.append((scanned, pending, processed, dirs_seen))
scan_remote_parallel(c3, max_workers=4, path="", fast_hash=True, on_progress=_on)
# 小树瞬间扫完，0.2s 限频会把中间回调合并，故只要求 >=2（首条 + 终态）。
# 真实大仓库（数千目录、数分钟）会按 ~5 次/秒平滑推进，条不再冻结。
_assert(len(progress) >= 2, "至少收到首条与终态两次进度回调：%d 次" % len(progress))
# 单调性：processed 单调不减，dirs_seen 单调不减
prev_p = prev_d = -1
for scanned, pending, processed, dirs_seen in progress:
    _assert(processed >= prev_p, "processed 单调不减")
    _assert(dirs_seen >= prev_d, "dirs_seen 单调不减")
    _assert(processed <= dirs_seen, "processed <= dirs_seen")
    _assert(pending == max(dirs_seen - processed, 0), "pending == dirs_seen - processed")
    prev_p, prev_d = processed, dirs_seen
last = progress[-1]
_assert(last[2] == last[3], "终态 processed==dirs_seen（%d==%d）" % (last[2], last[3]))
# ratio 应平滑从低到高（首条 < 末条，除非树极小）
_assert(progress[0][2] / max(progress[0][3], 1) <= last[2] / max(last[3], 1) + 1e-9,
        "ratio 整体非降")

print("== 4. should_cancel 能提前停止 ==")
c4 = _StubClient()
calls = {"n": 0}
def _should_cancel():
    calls["n"] += 1
    return calls["n"] > 2  # 列完前两个目录后就取消
scan_remote_parallel(c4, max_workers=2, path="", fast_hash=True,
                     should_cancel=_should_cancel)
_assert(c4.list_calls < len(TREE), "取消后未扫完全部目录（list_calls=%d < %d）" %
        (c4.list_calls, len(TREE)))

print("\nALL CHECKS PASSED")
