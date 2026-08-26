# -*- coding: utf-8 -*-
"""同步历史查询视图（类 git log / show）。

拆分自 ``core/sync_history.py``。只读查询，依赖 ``core.sync.store`` 的
目录/文件定位辅助。写入与清除见 ``core.sync.store``。
"""
import json
from typing import Optional

from .store import _ensure_dir, _history_file


def list_history(limit: int = 50, date_str: Optional[str] = None) -> list:
    """列出同步历史（类 git log）。

    Args:
        limit: 最多返回条数（按时间倒序）
        date_str: 指定日期（YYYYMMDD），为空则跨全部日期

    Returns:
        历史记录列表（不含 merged/failed 细节，仅摘要）
    """
    entries: list = []

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
