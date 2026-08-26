# -*- coding: utf-8 -*-
"""远程文件树扫描与内容缓存读取。"""
import concurrent.futures
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from core import cache
from .models import _log
from .scan_local import _file_hash


def scan_remote(client, path: str = "") -> dict[str, dict]:
    """递归扫描远端仓库某路径下的文件，返回 {相对路径: {size, hash}}。

    Args:
        client: 已配置的 JiraGitClient
        path:   起始路径（默认为根）

    Returns:
        {relative_path: {size, hash, is_dir}}
    """
    result = {}
    _scan_remote_dir(client, path, result)
    return result


def _scan_remote_dir(client, path: str, result: dict):
    """递归扫描目录（内部使用）。"""
    try:
        entries = client.list_level(path)
    except Exception:
        _log.warning("远端目录扫描失败：%s", path)
        return
    for e in entries:
        rel = e.path
        if e.is_dir:
            _scan_remote_dir(client, rel, result)
        else:
            try:
                content = client.get_file(rel)
                h = hashlib.md5(content).hexdigest() if content else ""
                result[rel] = {"size": e.size, "hash": h, "is_dir": False}
            except Exception:
                _log.warning("远端文件读取失败：%s", rel)
                result[rel] = {"size": e.size, "hash": "", "is_dir": False}


def scan_remote_parallel(
    client,
    max_workers: int = 8,
    path: str = "",
    on_progress=None,
) -> dict[str, dict]:
    """并行递归扫描远端仓库（带进度回调）。

    Args:
        client: 已配置的 JiraGitClient
        max_workers: 并发线程数（默认 8）
        path: 起始路径（默认根）
        on_progress: 进度回调 progress(done, total)

    Returns:
        {relative_path: {size, hash, is_dir}}
    """
    # 先拿根层，再并行展开各子目录
    entries = client.list_level(path)
    result = {}
    dirs = []
    files = []
    for e in entries:
        if e.is_dir:
            dirs.append(e.path)
        else:
            files.append(e)

    # 先处理根层文件
    for e in files:
        _collect_file(client, e, result)

    total = len(dirs) + 1
    done = 0

    def worker(d: str):
        sub = {}
        _scan_remote_dir(client, d, sub)
        return sub

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(worker, d): d for d in dirs}
        for fut in as_completed(futs):
            sub = fut.result()
            result.update(sub)
            done += 1
            if on_progress:
                on_progress(done, total)

    return result


def _collect_file(client, e, result: dict):
    try:
        content = client.get_file(e.path)
        h = hashlib.md5(content).hexdigest() if content else ""
        result[e.path] = {"size": e.size, "hash": h, "is_dir": False}
    except Exception:
        _log.warning("远端文件读取失败：%s", e.path)
        result[e.path] = {"size": e.size, "hash": "", "is_dir": False}


def scan_remote_cached(
    client,
    tree_ttl: int = 3600,
    use_cache: bool = True,
    max_workers: int = 8,
    path: str = "",
) -> dict[str, dict]:
    """缓存优先的远端扫描（远端较少变更，TTL 默认 1 小时）。

    Args:
        client: 已配置的 JiraGitClient
        tree_ttl: 缓存有效期（秒）
        use_cache: 是否启用缓存
        max_workers: 并发线程数
        path: 起始路径

    Returns:
        {relative_path: {size, hash, is_dir}}
    """
    if not use_cache:
        return scan_remote_parallel(client, max_workers=max_workers, path=path)

    # 用 server+repo 作为命名空间，path 作为 key（每次扫描一个仓库根）
    ns = "remote"
    key = f"{client.server_url}|{client.repo}|{path}"

    cached = cache.get(ns, key, tree_ttl)
    if cached is not None:
        _log.info("远端文件树命中缓存（%d 文件）", len(cached))
        return cached

    result = scan_remote_parallel(
        client, max_workers=max_workers, path=path
    )
    if result:
        cache.set(ns, key, result)
    return result


def get_file_cached(
    client,
    path: str,
    namespace: str = "default",
    ttl: int = 86400,
    use_cache: bool = True,
    content_hash: str = "",
) -> Optional[bytes]:
    """带内容缓存的远端文件读取。

    Args:
        client: 已配置的 JiraGitClient
        path: 文件相对路径
        namespace: 缓存命名空间（通常为 repo_id），用于隔离不同仓库的缓存
        ttl: 缓存有效期（秒，默认 1 天）
        use_cache: 是否启用缓存
        content_hash: 可选，远端 hash；若与缓存一致则跳过下载

    Returns:
        文件内容 bytes；读取失败返回 None
    """
    ns = f"file:{namespace}"
    key = path

    if use_cache:
        cached = cache.get(ns, key, ttl)
        if cached is not None:
            # 若提供了远端 hash，且缓存 hash 与之相同，直接复用
            if content_hash and cached.get("hash") == content_hash:
                return cached.get("content")
            # 否则仅当大小一致时复用（粗粒度）
            if not content_hash:
                return cached.get("content")

    try:
        content = client.get_file(path)
    except Exception:
        _log.warning("远端文件读取失败：%s", path)
        return None

    if content is None:
        return None

    if use_cache:
        cache.set(ns, key, {"hash": content_hash, "content": content})
    return content
