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


def scan_remote(client, path: str = "", fast_hash: bool = False) -> dict[str, dict]:
    """递归扫描远端仓库某路径下的文件，返回 {相对路径: {size, hash}}。

    Args:
        client:     已配置的 JiraGitClient
        path:       起始路径（默认为根）
        fast_hash:  True=快扫，仅记录 size（不下载内容算 md5），compute_diff 退化为
                    按大小比较；False=精确，逐文件下载内容算 md5（慢但能识别
                    「大小相同但内容不同」的修改）。默认 False 保持旧行为。

    Returns:
        {relative_path: {size, hash, is_dir}}
    """
    result = {}
    _scan_remote_dir(client, path, result, fast_hash)
    return result


def _scan_remote_dir(client, path: str, result: dict, fast_hash: bool = False):
    """递归扫描目录（内部使用）。

    ``fast_hash=True`` 时不再为每个文件调用 ``get_file`` 下载内容算 md5，
    只记录 size —— 这是「远程扫描加速」的核心：一次差异扫描从 O(N 次下载)
    降到 O(目录层数) 次 list_level 请求，对大仓库从分钟级降到秒级。
    """
    try:
        entries = client.list_level(client.repo_id, client.branch, path)
    except Exception as e:
        # 注意：这里曾把 TypeError（list_level 缺 branch/path 参数）吞成一句 warning，
        # 导致远端树被误判为「空」，进而算出错误差异。异常类型与信息必须带上。
        _log.warning("远端目录扫描失败：%s（%s: %s）", path, type(e).__name__, e)
        return
    for e in entries:
        rel = e.path
        if e.type == "dir":
            _scan_remote_dir(client, rel, result, fast_hash)
        else:
            if fast_hash:
                # 快扫：不下载内容，仅记录 size（hash 留空，compute_diff 退化为 size 比较）
                result[rel] = {"size": e.size, "hash": "", "is_dir": False}
                continue
            try:
                # get_file 的契约是返回 (content, error)，必须解包。
                # 不解包会把 tuple 当成文件内容：md5(tuple) 崩溃，或把 tuple 写进本地文件。
                content, err = client.get_file(rel)
                if err or content is None:
                    _log.warning("远端文件读取失败：%s（%s）", rel, err or "内容为空")
                    result[rel] = {"size": e.size, "hash": "", "is_dir": False}
                else:
                    body = content.encode("utf-8") if isinstance(content, str) else content
                    h = hashlib.md5(body).hexdigest()
                    result[rel] = {"size": e.size, "hash": h, "is_dir": False}
            except Exception as ex:
                _log.warning("远端文件读取失败：%s（%s: %s）", rel, type(ex).__name__, ex)
                result[rel] = {"size": e.size, "hash": "", "is_dir": False}


def scan_remote_parallel(
    client,
    max_workers: int = 8,
    path: str = "",
    on_progress=None,
    should_cancel=None,
    fast_hash: bool = False,
) -> dict[str, dict]:
    """并行递归扫描远端仓库（带进度回调）。

    Args:
        client: 已配置的 JiraGitClient
        max_workers: 并发线程数（默认 8）
        path: 起始路径（默认根）
        on_progress: 进度回调 progress(scanned, pending, processed, dirs_seen)
        should_cancel: 取消回调，返回 True 时尽快停止
        fast_hash: True=快扫（仅记录 size，不下载内容）；False=精确（逐文件算 md5）

    Returns:
        {relative_path: {size, hash, is_dir}}
    """
    # 先拿根层，再并行展开各子目录
    entries = client.list_level(client.repo_id, client.branch, path)
    result = {}
    dirs = []
    files = []
    for e in entries:
        if e.type == "dir":
            dirs.append(e.path)
        else:
            files.append(e)

    # 先处理根层文件
    for e in files:
        _collect_file(client, e, result, fast_hash)

    total_dirs = len(dirs)
    done = 0

    def worker(d: str):
        if should_cancel and should_cancel():
            return {}
        sub = {}
        _scan_remote_dir(client, d, sub, fast_hash)
        return sub

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(worker, d): d for d in dirs}
        for fut in as_completed(futs):
            if should_cancel and should_cancel():
                break
            sub = fut.result()
            result.update(sub)
            done += 1
            if on_progress:
                # 协议需与调用方一致：progress(scanned, pending, processed, dirs_seen)。
                # 旧实现只传 (done, total) 两个参数，与 routes_diff.py 的四参回调不匹配。
                on_progress(len(result), max(total_dirs - done, 0), done, total_dirs)

    return result


def _collect_file(client, e, result: dict, fast_hash: bool = False):
    if fast_hash:
        # 快扫：不下载内容，仅记录 size（hash 留空）
        result[e.path] = {"size": e.size, "hash": "", "is_dir": False}
        return
    try:
        # 同 _scan_remote_dir：get_file 返回 (content, error)，必须解包
        content, err = client.get_file(e.path)
        if err or content is None:
            _log.warning("远端文件读取失败：%s（%s）", e.path, err or "内容为空")
            result[e.path] = {"size": e.size, "hash": "", "is_dir": False}
            return
        body = content.encode("utf-8") if isinstance(content, str) else content
        h = hashlib.md5(body).hexdigest()
        result[e.path] = {"size": e.size, "hash": h, "is_dir": False}
    except Exception as ex:
        _log.warning("远端文件读取失败：%s（%s: %s）", e.path, type(ex).__name__, ex)
        result[e.path] = {"size": e.size, "hash": "", "is_dir": False}


def scan_remote_cached(
    client,
    namespace: str = "",
    tree_ttl: int = 3600,
    use_cache: bool = True,
    max_workers: int = 8,
    path: str = "",
    on_progress=None,
    should_cancel=None,
    fast_hash: bool = False,
) -> dict[str, dict]:
    """缓存优先的远端扫描（远端较少变更，TTL 默认 1 小时）。

    Args:
        client: 已配置的 JiraGitClient
        namespace: 缓存命名空间（调用方传 repo_id，用于隔离不同仓库的缓存）
        tree_ttl: 缓存有效期（秒）
        use_cache: 是否启用缓存
        max_workers: 并发线程数
        path: 起始路径
        on_progress: 进度回调 progress(scanned, pending, processed, dirs_seen)
        should_cancel: 取消回调，返回 True 时尽快停止
        fast_hash: True=快扫（仅记录 size，不下载内容）；False=精确（逐文件算 md5）

    Returns:
        {relative_path: {size, hash, is_dir}}
    """
    if not use_cache:
        return scan_remote_parallel(
            client, max_workers=max_workers, path=path,
            on_progress=on_progress, should_cancel=should_cancel,
            fast_hash=fast_hash,
        )

    # 缓存 key：优先用调用方传入的 namespace，否则回退到 client.repo_id。
    # ⚠️ 切勿使用 client.server_url / client.repo —— JiraGitClient 上没有这两个属性，
    #    用它们会抛 AttributeError，使整个差异扫描直接失败。
    # fast_hash 纳入 key：快扫(仅 size)与精确(带 md5)结果结构不同，必须分两套缓存，
    # 否则精确模式可能命中快扫的空 hash 缓存而误判「全部相同」。
    ns = "remote"
    ns_id = namespace or getattr(client, "repo_id", "") or "default"
    key = f"{ns_id}|{path}|{'f' if fast_hash else 'p'}"

    cached = cache.get(ns, key, tree_ttl)
    if cached is not None:
        _log.info("远端文件树命中缓存（%d 文件，%s）", len(cached), "快扫" if fast_hash else "精确")
        return cached

    result = scan_remote_parallel(
        client, max_workers=max_workers, path=path,
        on_progress=on_progress, should_cancel=should_cancel,
        fast_hash=fast_hash,
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
        # get_file 返回 (content, error) —— 必须解包。
        # 若把 tuple 原样返回，下游 file_diff 会对它调用 splitlines() 而崩溃
        # （AttributeError: 'tuple' object has no attribute 'splitlines'）。
        content, err = client.get_file(path)
    except Exception as ex:
        _log.warning("远端文件读取失败：%s（%s: %s）", path, type(ex).__name__, ex)
        return None

    if err or content is None:
        _log.warning("远端文件读取失败：%s（%s）", path, err or "内容为空")
        return None

    if use_cache:
        cache.set(ns, key, {"hash": content_hash, "content": content})
    return content
