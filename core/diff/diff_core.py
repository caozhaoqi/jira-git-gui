# -*- coding: utf-8 -*-
"""差异计算核心（自 ``core/diff_diff`` 拆分）。

提供：
- ``compute_diff``：对比单文件 old/new 文本，产出 ``DiffResult``
- ``file_diff``：对比磁盘上的两个文件（自动判断文本/二进制）
- ``is_whitespace_only_diff``：判断差异是否仅为空白/注释
- ``_is_text_file`` / ``_is_same_normalized``：辅助判定
"""
import difflib
import os
from pathlib import Path
from typing import Optional

from .models import (
    DiffStatus, DiffEntry, DiffResult, FileDiffResult, SKIP_DIRS, SKIP_FILES,
    _TEXT_EXTENSIONS, clear_dir_cache, _log,
)
from .normalize import canonical_text


def _is_text_file(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in _TEXT_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
        if b"\x00" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    except OSError:
        return False


def _is_same_normalized(a: str, b: str) -> bool:
    """空白/注释无关等价：两侧先做 canonical_text 再比较。"""
    na = canonical_text("<a>", a)
    nb = canonical_text("<b>", b)
    return na == nb


def compute_file_diff(name, old: Optional[str], new: Optional[str],
                      old_is_text=None, new_is_text=None) -> Optional[FileDiffResult]:
    """计算单个文件的差异（文本内容级对比）。

    - ``old``/``new`` 为文本内容或 ``None``（表示缺失）。
    - 返回 ``FileDiffResult(status, hunks, old_text, new_text)``；两侧都为 None 时返回 None。
    - 二进制文件：status=MODIFIED 且 hunks 为空（无法做行级比较）。

    ⚠️ 状态一律取 DiffStatus 的**既有成员**（SAME / MODIFIED / WHITESPACE_ONLY /
    LOCAL_ONLY / REMOTE_ONLY）。此前这里用的是 CHANGED / EQUAL / ADDED / REMOVED /
    BINARY —— 这些成员在 DiffStatus 上并不存在，任何比较都会抛
    ``AttributeError: type object 'DiffStatus' has no attribute 'CHANGED'``。
    """
    if old is not None and new is not None:
        if old_is_text is False or new_is_text is False:
            if old != new:
                return FileDiffResult(status=DiffStatus.MODIFIED, hunks=[],
                                      old_text=old, new_text=new)
            return FileDiffResult(status=DiffStatus.SAME, hunks=[],
                                  old_text=old, new_text=new)
        ca = canonical_text(name, old)
        cb = canonical_text(name, new)
        if _is_same_normalized(old, new):
            return FileDiffResult(status=DiffStatus.SAME, hunks=[],
                                  old_text=ca, new_text=cb)
        sm = difflib.SequenceMatcher(None, ca.splitlines(keepends=True),
                                     cb.splitlines(keepends=True))
        hunks = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            hunks.append({
                "tag": tag,
                "old": "".join(ca.splitlines(keepends=True)[i1:i2]),
                "new": "".join(cb.splitlines(keepends=True)[j1:j2]),
            })
        return FileDiffResult(status=DiffStatus.MODIFIED, hunks=hunks,
                              old_text=ca, new_text=cb)
    if old is None and new is not None:
        # 仅远端有 → REMOTE_ONLY（对应前端的 remote_only 徽标）
        return FileDiffResult(
            status=DiffStatus.REMOTE_ONLY, hunks=[], old_text=None,
            new_text=(new if new_is_text is False else canonical_text(name, new)))
    if old is not None and new is None:
        # 仅本地有 → LOCAL_ONLY
        return FileDiffResult(status=DiffStatus.LOCAL_ONLY, hunks=[],
                              old_text=canonical_text(name, old), new_text=None)
    return None


def format_unified_diff(res: Optional[FileDiffResult]) -> str:
    """把单文件 diff 结果渲染成统一差异文本；无差异时返回空串 ``""``。

    契约与 ``tests/test_differ_format.py`` 一致：内容相同（含仅行尾差异）返回 ``""``。
    """
    if res is None:
        return ""
    if res.status in (DiffStatus.SAME, DiffStatus.WHITESPACE_ONLY):
        return ""
    if not res.hunks:
        # 无行级 hunks：二进制差异 / 仅一侧存在，给一句可读说明
        if res.status == DiffStatus.REMOTE_ONLY:
            return "(远端新增文件，本地无此文件)"
        if res.status == DiffStatus.LOCAL_ONLY:
            return "(仅本地存在，远端无此文件)"
        return "(二进制文件，内容有差异，不做行级展示)"
    lines: list[str] = []
    for h in res.hunks:
        for ln in (h.get("old") or "").splitlines():
            lines.append("-" + ln)
        for ln in (h.get("new") or "").splitlines():
            lines.append("+" + ln)
    return "\n".join(lines)


def file_diff(path, new_content) -> str:
    """对比磁盘文件 ``path``（旧）与 ``new_content``（新），返回统一差异**文本**。

    契约（与 tests / api.routes_diff 一致）：**返回字符串**，无差异时为 ``""``。
    需要结构化结果（status / hunks / 两侧文本）请直接调用 ``compute_file_diff``。
    """
    p = Path(path)
    old_text = None
    old_is_text = None
    if p.exists():
        old_is_text = _is_text_file(p)
        if old_is_text:
            try:
                old_text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                old_is_text = False
                old_text = p.read_bytes().decode("utf-8", "replace")
        else:
            old_text = p.read_bytes().decode("utf-8", "replace")
    new_is_text = True if new_content is not None else None
    res = compute_file_diff(path, old_text, new_content,
                            old_is_text=old_is_text, new_is_text=new_is_text)
    return format_unified_diff(res)


def is_whitespace_only_diff(name, old, new) -> bool:
    """判断 old/new 的差异是否仅为空白或注释（无实质代码改动）。"""
    if old is None or new is None:
        return False
    return _is_same_normalized(old, new)


def compute_diff(local_files: dict, remote_files: dict,
                ignore_line_endings: bool = True) -> DiffResult:
    """目录级差异对比（基于扫描元数据，不读取文件内容）。

    约定（与 ``api.routes_diff`` 及测试一致）：
    - ``local_files`` / ``remote_files`` 为 ``path -> {size, mtime, hash,
      norm_hash?, norm_size?}`` 的字典（来自 ``scan_local`` / ``scan_remote``）。
    - 双方都有时，优先比 ``hash``；再比 ``size``；若 ``ignore_line_endings`` 且
      本地含 ``norm_size`` 且 ``norm_size == 远端 size``，则判定为 ``WHITESPACE_ONLY``
      （内容语义相同，仅行尾符差异）。
    - 仅一侧存在则为 ``LOCAL_ONLY`` / ``REMOTE_ONLY``。

    Args:
        local_files: 本地扫描结果字典
        remote_files: 远端扫描结果字典
        ignore_line_endings: 是否将"行尾符差异"识别为 WHITESPACE_ONLY

    Returns:
        DiffResult：聚合了全部条目的对比结果（含 summary()）。
    """
    result = DiffResult()
    for path in sorted(set(local_files) | set(remote_files)):
        l = local_files.get(path)
        r = remote_files.get(path)
        entry = DiffEntry(path=path)
        if l and r:
            entry.local_size = l.get("size")
            entry.remote_size = r.get("size")
            entry.local_hash = l.get("hash")
            entry.remote_hash = r.get("hash")
            # 严格相同：hash 优先，其次 size
            if l.get("hash") and r.get("hash"):
                same = l["hash"] == r["hash"]
            elif l.get("size") is not None and r.get("size") is not None:
                same = l["size"] == r["size"]
            else:
                same = False
            if same:
                entry.status = DiffStatus.SAME
                result.same += 1
            elif (ignore_line_endings
                  and l.get("norm_size") is not None
                  and r.get("size") is not None
                  and l["norm_size"] == r["size"]):
                entry.status = DiffStatus.WHITESPACE_ONLY
                result.modified += 1
            else:
                entry.status = DiffStatus.MODIFIED
                result.modified += 1
        elif l:
            entry.status = DiffStatus.LOCAL_ONLY
            entry.local_size = l.get("size")
            entry.local_hash = l.get("hash")
            result.local_only += 1
        else:
            entry.status = DiffStatus.REMOTE_ONLY
            entry.remote_size = r.get("size")
            entry.remote_hash = r.get("hash")
            result.remote_only += 1
        result.entries.append(entry)
    result.total = len(result.entries)
    return result
