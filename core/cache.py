# -*- coding: utf-8 -*-
"""JSON 文件缓存系统。

设计要点：
- 缓存以 JSON 文件存储在 cache/ 目录下
- 支持 TTL（生存时间）自动过期
- 支持命名空间隔离（按仓库 ID 分目录）
- 线程安全（文件级锁）
- 缓存命中时直接返回，未命中时调用 fetcher 获取并写入
"""
import json
import os
import time
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .logger import get_logger

_log = get_logger()

# 缓存根目录
CACHE_DIR = Path(__file__).parent.parent / "cache"

# 文件锁
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(key: str) -> threading.Lock:
    """获取指定 key 的文件锁。"""
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _cache_path(namespace: str, key: str) -> Path:
    """获取缓存文件路径。"""
    safe_key = key.replace("/", "_").replace("\\", "_").replace(":", "_")
    return CACHE_DIR / namespace / f"{safe_key}.json"


def get(namespace: str, key: str, ttl: int = 3600) -> Optional[Any]:
    """从缓存读取数据。

    Args:
        namespace: 命名空间（如 repo ID）
        key: 缓存键（如文件路径）
        ttl: 缓存生存时间（秒），0 表示永不过期

    Returns:
        缓存的数据，未命中或已过期返回 None
    """
    path = _cache_path(namespace, key)
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)

        # 检查 TTL
        if ttl > 0:
            cached_at = entry.get("_cached_at", 0)
            if time.time() - cached_at > ttl:
                return None

        return entry.get("data")
    except (json.JSONDecodeError, OSError) as e:
        _log.debug("缓存读取失败 %s/%s: %s", namespace, key, e)
        return None


def set(namespace: str, key: str, data: Any) -> bool:
    """写入缓存数据。"""
    path = _cache_path(namespace, key)
    lock = _get_lock(f"{namespace}/{key}")

    with lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "_cached_at": time.time(),
                "data": data,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
            return True
        except (OSError, TypeError) as e:
            _log.debug("缓存写入失败 %s/%s: %s", namespace, key, e)
            return False


def get_or_fetch(
    namespace: str,
    key: str,
    fetcher: Callable[[], Any],
    ttl: int = 3600,
) -> Any:
    """优先从缓存获取，未命中则调用 fetcher 获取并缓存。

    Args:
        namespace: 命名空间
        key: 缓存键
        fetcher: 数据获取函数
        ttl: 缓存生存时间（秒），0 表示永不过期

    Returns:
        缓存或新获取的数据
    """
    # 1. 先尝试缓存
    cached = get(namespace, key, ttl)
    if cached is not None:
        return cached

    # 2. 调用 fetcher 获取
    data = fetcher()
    if data is not None:
        set(namespace, key, data)

    return data


def invalidate(namespace: str, key: str = "") -> int:
    """使缓存失效。

    Args:
        namespace: 命名空间
        key: 指定 key 则只清除该项，为空则清除整个命名空间

    Returns:
        清除的缓存条目数
    """
    if key:
        path = _cache_path(namespace, key)
        if path.exists():
            try:
                path.unlink()
                return 1
            except OSError:
                return 0
        return 0

    # 清除整个命名空间
    ns_dir = CACHE_DIR / namespace
    if not ns_dir.exists():
        return 0

    count = 0
    for f in ns_dir.glob("*.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            continue
    return count


def clear_all() -> int:
    """清空所有缓存。"""
    if not CACHE_DIR.exists():
        return 0

    count = 0
    for f in CACHE_DIR.rglob("*.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            continue
    return count


def cache_info() -> dict:
    """获取缓存统计信息。"""
    if not CACHE_DIR.exists():
        return {"total_entries": 0, "total_size": 0, "namespaces": []}

    entries = 0
    total_size = 0
    namespaces = []

    for ns_dir in CACHE_DIR.iterdir():
        if not ns_dir.is_dir():
            continue
        ns_entries = 0
        ns_size = 0
        for f in ns_dir.glob("*.json"):
            ns_entries += 1
            try:
                ns_size += f.stat().st_size
            except OSError:
                continue
        entries += ns_entries
        total_size += ns_size
        namespaces.append({
            "name": ns_dir.name,
            "entries": ns_entries,
            "size": ns_size,
        })

    return {
        "total_entries": entries,
        "total_size": total_size,
        "namespaces": namespaces,
    }
