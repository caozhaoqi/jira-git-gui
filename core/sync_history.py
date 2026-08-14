# -*- coding: utf-8 -*-
"""同步历史记录系统（类 git log）。

设计要点：
- 每次同步操作记录一条 commit，存为 JSON 文件
- 历史目录 sync_history/，按日期分文件，便于追溯
- 支持 list / show / clear 等操作，类似 git log / git show
- 线程安全（文件级锁）
- 记录脱敏后的仓库别名，避免敏感信息落盘
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .logger import get_logger

_log = get_logger()

# 历史根目录
HISTORY_DIR = Path(__file__).parent.parent / "sync_history"

# 写锁
_write_lock = threading.Lock()


def _ensure_dir() -> Path:
    """确保历史目录存在。"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return HISTORY_DIR


def _history_file(date_str: Optional[str] = None) -> Path:
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
    merged: list[dict],
    failed: list[dict],
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


def list_history(limit: int = 50, date_str: Optional[str] = None) -> list[dict]:
    """列出同步历史（类 git log）。

    Args:
        limit: 最多返回条数（按时间倒序）
        date_str: 指定日期（YYYYMMDD），为空则跨全部日期

    Returns:
        历史记录列表（不含 merged/failed 细节，仅摘要）
    """
    entries: list[dict] = []

    if date_str:
        files = [_history_file(date_str)]
    else:
        files = sorted(_ensure_dir().glob("sync-*.jsonl"), reverse=True)

    for fp in files:
        if not fp.exists():
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # 摘要视图：去掉细节
                    entries.append({
                        "id": e.get("id"),
                        "time": e.get("time"),
                        "ts": e.get("ts"),
                        "repo": e.get("repo"),
                        "local_dir": e.get("local_dir"),
                        "summary": e.get("summary"),
                        "merged_count": e.get("merged_count"),
                        "failed_count": e.get("failed_count"),
                        "duration": e.get("duration"),
                        "status": e.get("status"),
                    })
        except OSError:
            continue
        if len(entries) >= limit:
            break

    entries.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return entries[:limit]


def show(commit_id: str) -> Optional[dict]:
    """查看某次同步的详细信息（类 git show）。

    Args:
        commit_id: 记录 ID

    Returns:
        完整记录（含 merged/failed 细节），未找到返回 None
    """
    for fp in _ensure_dir().glob("sync-*.jsonl"):
        if not fp.exists():
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("id") == commit_id:
                        return e
        except OSError:
            continue
    return None


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


def stats() -> dict:
    """统计信息（类 git 简报）。"""
    total = 0
    success = 0
    partial = 0
    failed = 0
    total_merged = 0
    total_failed_files = 0
    total_duration = 0.0
    last_time = ""

    for fp in _ensure_dir().glob("sync-*.jsonl"):
        if not fp.exists():
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    st = e.get("status", "")
                    if st == "success":
                        success += 1
                    elif st == "partial":
                        partial += 1
                    else:
                        failed += 1
                    total_merged += e.get("merged_count", 0)
                    total_failed_files += e.get("failed_count", 0)
                    total_duration += e.get("duration", 0)
                    t = e.get("time", "")
                    if t > last_time:
                        last_time = t
        except OSError:
            continue

    return {
        "total_syncs": total,
        "success": success,
        "partial": partial,
        "failed": failed,
        "total_merged_files": total_merged,
        "total_failed_files": total_failed_files,
        "total_duration": round(total_duration, 2),
        "last_sync_time": last_time,
    }


def format_log(limit: int = 20) -> str:
    """格式化历史为类 git log 文本（用于 CLI 输出）。"""
    entries = list_history(limit=limit)
    if not entries:
        return "(无同步历史)"
    lines = []
    for e in entries:
        lines.append(
            f"commit {e['id']}\n"
            f"时间: {e['time']}\n"
            f"仓库: {e['repo']}\n"
            f"目录: {e['local_dir']}\n"
            f"状态: {e['status']}  耗时: {e['duration']}s\n"
            f"摘要: total={e['summary'].get('total',0)} "
            f"modified={e['summary'].get('modified',0)} "
            f"remote_only={e['summary'].get('remote_only',0)} "
            f"merged={e['merged_count']} failed={e['failed_count']}\n"
        )
    return "\n".join(lines)
