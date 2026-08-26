# -*- coding: utf-8 -*-
"""同步历史存储层（类 git 提交记录）。

拆分自 ``core/sync_history.py``。负责历史记录的落盘写入、目录管理与清除；
只读查询视图见 ``core.sync.view``。

设计要点：
- 每次同步操作记录一条 commit，存为 JSONL 文件（按天归档）
- 历史目录 sync_history/（开发态 <root>/sync_history；冻结态 ~/.jira-git-gui/sync_history）
- 线程安全（文件级写锁）
- 记录脱敏后的仓库别名，避免敏感信息落盘
"""
import json
import threading
import time
import uuid
from typing import Optional

from core.app_paths import get_data_root
from core.logger import get_logger

_log = get_logger()

# 历史根目录
HISTORY_DIR = get_data_root() / "sync_history"

# 写锁
_write_lock = threading.Lock()


def _ensure_dir() -> "Path":
    """确保历史目录存在。"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR


def _history_file(date_str: Optional[str] = None) -> "Path":
    """获取指定日期的历史文件（按天归档）。"""
    if not date_str:
        date_str = time.strftime("%Y%m%d")
    return _ensure_dir() / f"sync-{date_str}.jsonl"


def _desensitize(name: str) -> str:
    """对仓库名等敏感信息做轻量脱敏（保留可辨识度，隐藏细节）。"""
    if not name:
        return name
    # 仅保留首字符 + 长度标识，避免完整业务名落盘
    if len(name) <= 2:
        return name[0] + "*"
    return name[0] + f"***({len(name)})"


def record(
    repo_alias: str,
    local_dir: str,
    summary: dict,
    merged: list,
    failed: list,
    duration: float,
    status: str = "success",
    extra: Optional[dict] = None,
) -> str:
    """记录一次同步操作（类 git commit）。

    Args:
        repo_alias: 仓库别名（脱敏后存储）
        local_dir: 本地目录
        summary: 差异摘要 {total, same, modified, local_only, remote_only}
        merged: 已合并文件列表 [{path, status}]
        failed: 失败文件列表 [{path, error}]
        duration: 耗时（秒）
        status: 状态 success / partial / failed
        extra: 附加信息

    Returns:
        本次记录的唯一 ID（类 commit hash 短形式）
    """
    commit_id = uuid.uuid4().hex[:8]
    now = time.time()
    entry = {
        "id": commit_id,
        "ts": now,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": _desensitize(repo_alias),
        "local_dir": local_dir,
        "summary": summary,
        "merged_count": len(merged),
        "failed_count": len(failed),
        "duration": round(duration, 2),
        "status": status,
        "merged": merged[:200],   # 截断，避免单条过大
        "failed": failed[:200],
        "extra": extra or {},
    }

    with _write_lock:
        path = _history_file()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            _log.error("写入同步历史失败: %s", e)
    return commit_id


def clear(date_str: Optional[str] = None) -> int:
    """清除同步历史。

    Args:
        date_str: 指定日期，为空则清除全部

    Returns:
        清除的文件数
    """
    with _write_lock:
        if date_str:
            fp = _history_file(date_str)
            if fp.exists():
                try:
                    fp.unlink()
                    return 1
                except OSError:
                    return 0
            return 0
        count = 0
        for fp in _ensure_dir().glob("sync-*.jsonl"):
            try:
                fp.unlink()
                count += 1
            except OSError:
                continue
        return count
