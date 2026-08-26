"""core.sync 子包 —— 由拆分后的子模块聚合而成，对外符号保持兼容。"""

# ---- 以下来自原 core/sync_history.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""同步历史记录系统（聚合壳，类 git log）。

原单体 ``core/sync_history.py`` 已按业务子域拆分为：
- ``core.sync.store``：写入 / 清除 / 目录管理（record、clear）
- ``core.sync.view``：只读查询（list_history、show、stats、format_log）

本文件仅做向后兼容的 re-export，对外符号与行为保持不变。
"""
from .store import (
    record, clear, HISTORY_DIR, _write_lock, _ensure_dir, _history_file, _desensitize,
)
from .view import list_history, show, stats, format_log

__all__ = [
    "record", "clear", "list_history", "show", "stats", "format_log",
    "HISTORY_DIR", "_write_lock", "_ensure_dir", "_history_file", "_desensitize",
]

