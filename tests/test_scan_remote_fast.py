# -*- coding: utf-8 -*-
"""fast_hash 快扫回归测试（离线，无真实 Jira 连接）。

守住「远程扫描加速」这条需求的两个核心不变量：

1. **快扫必须零内容下载** —— ``fast_hash=True`` 时 ``scan_remote`` 只调 ``list_level``
   列目录树，绝不为任何文件调 ``get_file`` 下载内容算 md5。这正是把大仓库差异扫描
   从「分钟级 / O(N 次下载)」降到「秒级 / O(目录层数)」的关键。

2. **空 hash 仍能算出正确差异** —— compute_diff 在双方 hash 都为空时，退化为按 size
   比较（diff_core.py:187）。因此快扫产出的 ``{size, hash:""}`` 必须给出正确的
   大小级差异（modified / same / remote_only），而不是全部判为 same 或崩溃。

运行：./venv/bin/python -m pytest tests/test_scan_remote_fast.py -q
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.diff.diff_core as _dc
from core.diff.scan_remote import scan_remote as _scan_remote


class _Entry:
    """模拟 client.list_level 返回的目录项（.path/.type/.size 三属性）。"""
    def __init__(self, path, type_, size):
        self.path = path
        self.type = type_
        self.size = size


class _StubClient:
    """最小替身：list_level 返回固定树；get_file 记录是否被打。

    list_level 签名必须照抄 ``core.client.browse.list_level(client, repo_id, branch, path)``，
    这样 scan_remote 内部若把参数顺序写反会立即暴露。
    """
    def __init__(self):
        self.repo_id = "895"
        self.branch = "main"
        self.get_file_calls = []

    def list_level(self, repo_id, branch, path):
        assert repo_id == "895", "repo_id 位传错"
        assert branch == "main", "branch 位传错"
        if path == "":
            return [
                _Entry("sub", "dir", 0),
                _Entry("a.txt", "file", 10),
                _Entry("b.txt", "file", 20),
            ]
        if path == "sub":
            return [_Entry("sub/c.txt", "file", 5)]
        return []

    def get_file(self, path):
        self.get_file_calls.append(path)
        return ("remote-body", None)


def _local_files():
    """模拟 scan_local 的产物：含 size + hash（文本文件还带 norm_size）。"""
    return {
        "a.txt": {"size": 10, "hash": "h_a", "norm_size": 10},
        "b.txt": {"size": 20, "hash": "h_b", "norm_size": 20},
        "sub/c.txt": {"size": 5, "hash": "h_c", "norm_size": 5},
        "only_local.txt": {"size": 3, "hash": "h_l", "norm_size": 3},
    }


def test_fast_scan_does_not_download_content():
    """快扫模式下绝不调用 get_file —— 扫描加速的核心不变量。"""
    client = _StubClient()
    remote = _scan_remote(client, path="", fast_hash=True)
    assert client.get_file_calls == [], \
        "快扫不应下载任何文件内容，却调了 get_file：%r" % client.get_file_calls


def test_precise_scan_downloads_content():
    """对照：精确模式必须逐文件下载内容算 md5（验证 stub 本身可用）。"""
    client = _StubClient()
    remote = _scan_remote(client, path="", fast_hash=False)
    # 4 个文件（a/b/sub/c）都应被下载
    assert set(client.get_file_calls) == {"a.txt", "b.txt", "sub/c.txt"}, \
        "精确模式漏抓：%r" % client.get_file_calls


def test_fast_scan_records_size_only_empty_hash():
    """快扫条目结构：size 有值、hash 为空串。"""
    client = _StubClient()
    remote = _scan_remote(client, path="", fast_hash=True)
    # 目录项本身不入结果（只递归），故只有文件
    assert set(remote.keys()) == {"a.txt", "b.txt", "sub/c.txt"}, remote
    for meta in remote.values():
        if meta.get("is_dir"):
            continue
        assert meta["hash"] == "", "快扫 hash 必须为空：%r" % meta
        assert isinstance(meta["size"], int) and meta["size"] >= 0


def test_fast_scan_diff_is_size_based_and_correct():
    """空 hash 退化按 size 比较：大小变→MODIFIED，大小同→SAME，仅远端有→REMOTE_ONLY。"""
    client = _StubClient()
    remote = _scan_remote(client, path="", fast_hash=True)
    # 远端把 b.txt 改成不同大小（20→99），a.txt/sub/c.txt 大小不变；新增 remote_only.txt
    remote["b.txt"]["size"] = 99
    remote["remote_only.txt"] = {"size": 7, "hash": "", "is_dir": False}

    local = _local_files()
    result = _dc.compute_diff(local, remote, ignore_line_endings=True)

    by_path = {e.path: e for e in result.entries}
    assert by_path["a.txt"].status.value == "same", \
        "大小未变的文件应判为 same，实际：%r" % by_path["a.txt"].status
    assert by_path["sub/c.txt"].status.value == "same"
    assert by_path["b.txt"].status.value == "modified", \
        "大小变化的文件应判为 modified，实际：%r" % by_path["b.txt"].status
    assert by_path["remote_only.txt"].status.value == "remote_only"
    assert by_path["only_local.txt"].status.value == "local_only"
    assert result.same == 2 and result.modified == 1 \
        and result.remote_only == 1 and result.local_only == 1
