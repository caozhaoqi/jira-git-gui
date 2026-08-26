# -*- coding: utf-8 -*-
"""差异对比的数据模型、常量与目录缓存状态。"""
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.logger import get_logger

_log = get_logger()

# 合并时已确认存在的父目录集合，避免每个文件都 mkdir(parents=True)
_DIR_CACHE: set[str] = set()
# 并发合并下保护 _DIR_CACHE 的读写
_DIR_CACHE_LOCK = threading.Lock()

def clear_dir_cache() -> None:
    """清空父目录缓存（建议每个批量合并任务开始前调用一次）。"""
    with _DIR_CACHE_LOCK:
        _DIR_CACHE.clear()

# 扫描时跳过的目录名
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", ".trae", ".qoder", ".playwright-cli", ".workbuddy",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
}

# 扫描时跳过的文件名
SKIP_FILES = {".DS_Store", "Thumbs.db", ".gitignore"}

# 通常按行尾符差异处理的文本文件扩展名（用于 compute_diff 的 whitespace_only 启发）
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".scala", ".groovy",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".go", ".rs", ".swift",
    ".rb", ".php", ".pl", ".pm", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".html", ".htm", ".xml", ".xhtml", ".svg",
    ".css", ".scss", ".sass", ".less", ".styl",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".config",
    ".sql", ".lua", ".vim", ".el", ".clj", ".erl", ".ex", ".exs",
    ".dockerfile", ".gitignore", ".gitattributes", ".editorconfig", "Makefile",
}


class DiffStatus(str, Enum):
    SAME = "same"           # 内容相同
    MODIFIED = "modified"   # 双方都有但内容不同
    WHITESPACE_ONLY = "whitespace_only"  # 仅行尾符/空白差异（文本文件归一化后相同）
    LOCAL_ONLY = "local_only"   # 仅本地存在
    REMOTE_ONLY = "remote_only" # 仅远程存在


@dataclass
class DiffEntry:
    """单文件的差异信息。"""
    path: str               # 相对路径
    status: DiffStatus = DiffStatus.SAME
    local_size: Optional[int] = None
    remote_size: Optional[int] = None
    local_hash: Optional[str] = None
    remote_hash: Optional[str] = None


@dataclass
class DiffResult:
    """整个扫描的结果。"""
    entries: list[DiffEntry] = field(default_factory=list)
    total: int = 0
    same: int = 0
    modified: int = 0
    local_only: int = 0
    remote_only: int = 0
    error: str = ""

    def summary(self) -> dict:
        # 注意：diffState 里的 modified 计数已包含 WHITESPACE_ONLY，
        # 这里额外给出 whitespace_only 便于前端单独展示。
        return {
            "total": self.total,
            "same": self.same,
            "modified": self.modified,
            "whitespace_only": sum(1 for e in self.entries if e.status == DiffStatus.WHITESPACE_ONLY),
            "local_only": self.local_only,
            "remote_only": self.remote_only,
        }

