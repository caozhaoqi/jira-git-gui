# -*- coding: utf-8 -*-
"""缓存统计与清理路由。"""
from fastapi import APIRouter
from api.common import app
from core import cache as _cache

router = APIRouter()


@router.get("/api/cache/info")
async def api_cache_info():
    """获取缓存统计信息。"""
    return _cache.cache_info()


@router.delete("/api/cache")
async def api_cache_clear(namespace: str = ""):
    """清空缓存（可指定命名空间）。"""
    if namespace:
        n = _cache.invalidate(namespace)
    else:
        n = _cache.clear_all()
    return {"ok": True, "cleared": n}
