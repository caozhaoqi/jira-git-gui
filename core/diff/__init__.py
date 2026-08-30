"""core.diff 子包 —— 由拆分后的子模块聚合而成，对外符号保持兼容。"""

# ---- 以下来自原 core/differ.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""本地目录 vs 远程仓库的差异对比（聚合兼容层）。

实现已按职责拆分到子模块：
  - core.diff.models : 数据模型 / 常量 / 目录缓存状态
  - core.diff   : 本地/远程文件树扫描（含 JSON 缓存）
  - core.diff   : 差异计算（unified diff / 归一化 / 行尾符差异）
  - core.diff  : 合并（远程 → 本地）+ 并行批量合并

本文件仅做重导出，确保 `from core import differ` / `from core.diff import X`
的外部调用方无需改动。
"""
from .models import (
    DiffStatus, DiffEntry, DiffResult,
    SKIP_DIRS, SKIP_FILES, _TEXT_EXTENSIONS,
    clear_dir_cache, _DIR_CACHE, _DIR_CACHE_LOCK, _log,
)
__all__ = [
    "DiffStatus", "DiffEntry", "DiffResult",
    "SKIP_DIRS", "SKIP_FILES", "_TEXT_EXTENSIONS",
    "clear_dir_cache", "scan_local", "scan_local_cached",
    "scan_remote", "scan_remote_parallel", "scan_remote_cached", "get_file_cached",
    "compute_diff", "file_diff", "is_whitespace_only_diff", "canonical_text",
    "merge_to_local", "merge_to_local_bytes", "merge_entries",
]


# ---- 以下来自原 core/diff_scan.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""本地/远程文件树扫描 —— 聚合兼容层。

实现已按职责拆分到子模块：
  - core.diff.scan_local  : 本地扫描 / 增量复用 / JSON 缓存 / 行尾归一化
  - core.diff.scan_remote : 远程扫描（并行）/ 内容缓存读取

本文件对子模块做 re-export，保证 ``from core.diff import *``（differ /
routes_diff / run_merge）的既有调用方式不变。
"""
from .scan_local import (  # noqa: E402,F401
    scan_local,
    scan_local_cached,
    _file_hash,
    _file_hashes,
    _is_text_file,
    _normalized_hash,
    _normalized_size,
)
from .scan_remote import (  # noqa: E402,F401
    scan_remote,
    _scan_remote_dir,
    scan_remote_parallel,
    scan_remote_cached,
    get_file_cached,
)

__all__ = [
    "scan_local",
    "scan_local_cached",
    "scan_remote",
    "scan_remote_parallel",
    "scan_remote_cached",
    "get_file_cached",
    "_scan_remote_dir",
    "_file_hash",
    "_file_hashes",
    "_is_text_file",
    "_normalized_hash",
    "_normalized_size",
]


# ---- 以下来自原 core/diff_diff.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""差异计算引擎（聚合壳）。

原单体 ``core/diff_diff.py`` 已按业务子域拆分为：
- ``core.diff.diff_core``：差异计算核心（``compute_diff`` / ``file_diff`` / ``is_whitespace_only_diff``）
- ``core.diff.normalize``：规范化工具（``canonical_text`` / ``_strip_jsonc_comments``）

本文件仅做向后兼容的 re-export，对外符号与行为保持不变。
"""
from .diff_core import (
    compute_diff, file_diff, is_whitespace_only_diff,
    DiffResult, _is_text_file, _is_same_normalized,
)
from .normalize import (
    canonical_text, _strip_jsonc_comments, _JSON_EXTENSIONS, _XML_EXTENSIONS,
)

__all__ = [
    "compute_diff", "file_diff", "is_whitespace_only_diff",
    "DiffResult", "_is_text_file", "_is_same_normalized",
    "canonical_text", "_strip_jsonc_comments",
    "_JSON_EXTENSIONS", "_XML_EXTENSIONS",
]


# ---- 以下来自原 core/diff_merge.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""合并引擎（聚合壳）。

原单体 ``core/diff_merge.py`` 已按业务子域拆分为：
- ``core.diff.merge_file``：单文件合并（内容比对 / 写入 / 权限修复）
- ``core.diff.merge_entries``：并行批量合并

本文件仅做向后兼容的 re-export，对外符号与行为保持不变。
"""
from .merge_file import (
    merge_to_local, merge_to_local_bytes, _write_file, _force_writable,
)
from .merge_entries import merge_entries
from .merge_manifest import (
    load_manifest, save_manifest, is_already_merged, content_hash,
)

__all__ = [
    "merge_to_local", "merge_to_local_bytes", "_write_file", "_force_writable",
    "merge_entries",
    "load_manifest", "save_manifest", "is_already_merged", "content_hash",
]

