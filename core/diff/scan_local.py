# -*- coding: utf-8 -*-
"""本地文件树扫描（含增量复用与 JSON 缓存）。"""
import hashlib
import os
from pathlib import Path
from typing import Optional

from core import cache
from .models import (
    SKIP_DIRS, SKIP_FILES, _TEXT_EXTENSIONS, _log,
)


def scan_local(local_dir: str, prev: Optional[dict] = None) -> dict[str, dict]:
    """递归扫描本地目录，返回 {relative_path: {size, mtime, hash, norm_hash, norm_size}}。

    增量优化（prev）：
    - prev 为上次扫描结果（可来自 JSON 缓存）。
    - 若某文件 size 与 mtime（st_mtime_ns）均未变化，且 prev 中已有 hash，
      直接复用旧 hash，跳过 MD5 整文件读取 —— 大仓库重复扫描可省下绝大部分磁盘 I/O。
    - 仅新增 / 修改（mtime 变化）的文件才重新计算 MD5。

    行尾归一化：
    - 对文本文件额外计算 norm_hash（\\r\\n 归一为 \\n 后的 MD5）和 norm_size。
    - 用于 compute_diff 识别 CRLF vs LF 导致的伪修改，避免无意义合并。
    """
    result = {}
    base = Path(local_dir)
    if not base.is_dir():
        return result

    for root, dirs, files in os.walk(base):
        # 原地修改 dirs 以跳过不需要的目录
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES:
                continue
            full = Path(root) / f
            try:
                rel = str(full.relative_to(base))
                # 统一为正斜杠
                rel = rel.replace("\\", "/")
                stat = full.stat()
                size = stat.st_size
                mtime = stat.st_mtime_ns
                is_text = _is_text_file(full)
                if prev:
                    pe = prev.get(rel)
                    # size + mtime 双校验：两者都未变才可安全复用旧 hash
                    if (pe and pe.get("size") == size
                            and pe.get("mtime") == mtime and pe.get("hash")):
                        h = pe["hash"]
                        norm_h = pe.get("norm_hash") if is_text else ""
                        norm_size = pe.get("norm_size") if is_text else size
                    elif is_text:
                        # 单次读取同时算 raw + 归一化 hash（省一次整文件 IO）
                        h, norm_h, norm_size = _file_hashes(full)
                    else:
                        h = _file_hash(full)
                        norm_h, norm_size = "", size
                else:
                    if is_text:
                        h, norm_h, norm_size = _file_hashes(full)
                    else:
                        h = _file_hash(full)
                        norm_h, norm_size = "", size
                entry: dict = {"size": size, "mtime": mtime, "hash": h}
                if is_text and norm_h:
                    entry["norm_hash"] = norm_h
                    entry["norm_size"] = norm_size
                result[rel] = entry
            except (OSError, PermissionError):
                continue
    return result


def scan_local_cached(
    local_dir: str,
    tree_ttl: int = 300,
    use_cache: bool = True,
) -> dict[str, dict]:
    """缓存优先的本地扫描（本地变更较快，TTL 默认 5 分钟）。

    Args:
        local_dir: 本地目录
        tree_ttl: 缓存有效期（秒）
        use_cache: 是否启用缓存

    Returns:
        {relative_path: {size, mtime, hash, norm_hash?, norm_size?}}
    """
    if not use_cache:
        return scan_local(local_dir)

    # 用目录绝对路径作为缓存命名空间与 key
    ns = "local"
    key = str(Path(local_dir).resolve())

    cached = cache.get(ns, key, tree_ttl)
    if cached is not None:
        _log.info("本地文件树命中缓存（%d 文件）", len(cached))
        return cached

    # 未命中/过期：以「最近一次扫描（忽略 TTL）」为增量基线，未变文件复用旧 hash，
    # 跳过 MD5 整文件读取。零 I/O 命中路径（上方）保留，此处仅加速过期后的重扫。
    prev = cache.get(ns, key, ttl=0)
    result = scan_local(local_dir, prev=prev)
    if result:
        cache.set(ns, key, result)
    return result


def _file_hash(path: Path) -> str:
    """计算文件 MD5（用于快速判断内容是否一致）。"""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return ""


def _file_hashes(path: Path) -> tuple:
    """一次读取同时计算 raw MD5 + 归一化 MD5 / 归一化尺寸。

    等价于原先 ``_file_hash`` + ``_normalized_hash`` + ``_normalized_size``
    的三次打开读取，但只读一遍文件——大仓库首次扫描可省下约一半磁盘 IO。

    Returns:
        (raw_md5, norm_md5, norm_size)；二进制（含 NUL）时 norm_md5 为空串，
        norm_size 为原始字节数（与 _normalized_size 行为一致）。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
        raw = hashlib.md5(data).hexdigest()
        if b"\x00" in data:
            return raw, "", len(data)
        norm_size = len(data.replace(b"\r\n", b"\n"))
        text = data.decode("utf-8", errors="replace")
        norm_h = hashlib.md5(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        return raw, norm_h, norm_size
    except (OSError, PermissionError):
        return "", "", 0


def _is_text_file(path: Path) -> bool:
    """基于扩展名判断文件是否为文本文件（用于行尾归一化比较）。"""
    ext = path.suffix.lower()
    if not ext and path.name.lower() == "makefile":
        return True
    return ext in _TEXT_EXTENSIONS


def _normalized_hash(path: Path) -> str:
    """计算文件内容的「归一化 MD5」：将 \\r\\n 统一为 \\n 后计算哈希。

    用于识别仅因行尾符（CRLF vs LF）差异导致 raw hash 不同的文本文件。
    读取失败时返回空字符串。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
        # 只处理看起来像文本的内容：遇到 NUL 字节则放弃归一化
        if b"\x00" in data:
            return ""
        text = data.decode("utf-8", errors="replace")
        normalized = text.replace("\r\n", "\n")
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()
    except (OSError, PermissionError):
        return ""


def _normalized_size(path: Path) -> int:
    """返回文件内容将 \\r\\n 替换为 \\n 后的字节数。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if b"\x00" in data:
            return len(data)
        return len(data.replace(b"\r\n", b"\n"))
    except (OSError, PermissionError):
        return 0
