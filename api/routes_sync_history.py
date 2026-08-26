# -*- coding: utf-8 -*-
"""同步历史（类 git log）路由。"""
from fastapi import HTTPException

from fastapi import APIRouter
from api.common import app
from core import sync_history as _history

router = APIRouter()


@router.get("/api/sync-history")
async def api_sync_history_list(limit: int = 50, date: str = ""):
    """列出同步历史（类 git log）。"""
    return {"entries": _history.list_history(limit=limit, date_str=date or None)}


@router.get("/api/sync-history/stats")
async def api_sync_history_stats():
    """同步统计信息。"""
    return _history.stats()


@router.get("/api/sync-history/{commit_id}")
async def api_sync_history_show(commit_id: str):
    """查看某次同步详情（类 git show）。"""
    entry = _history.show(commit_id)
    if not entry:
        raise HTTPException(404, "未找到该同步记录")
    return entry


@router.delete("/api/sync-history")
async def api_sync_history_clear(date: str = ""):
    """清空同步历史（可指定日期）。"""
    n = _history.clear(date_str=date or None)
    return {"ok": True, "cleared": n}
