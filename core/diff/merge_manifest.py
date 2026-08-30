# -*- coding: utf-8 -*-
"""批量合并的断点续传 manifest。

每次 ``/api/diff/merge-batch`` 完成后，把每条文件的结果（ok / 远端内容 hash）落盘，
下次合并时过滤掉「已成功且本地内容仍与记录 hash 一致」的文件 —— 这些文件**不再抓取、
不再重写**，直接计入已完成，实现真正跳过（省掉重抓缓存那一步）。

⚠️ manifest 存于应用数据目录 sidecar（``get_data_root()/merge_state/<safe_local_dir>/``），
**不写入 local_dir 内部**：否则 ``.merge_manifest.json`` 会作为 local_only 文件出现在下次 diff 扫描里，
污染用户仓库 / git status。按 local_dir 绝对路径做命名空间隔离，与仓库解耦。
"""
import hashlib
import json
import threading
from pathlib import Path
from typing import Optional

from core.app_paths import get_data_root
from .models import _log

# 每个 local_dir 一把锁，避免并发合并同一目录时 manifest 写竞争
_MANIFEST_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _manifest_path(local_dir: str) -> Path:
    """manifest 落盘路径：按 local_dir 绝对路径做安全命名。"""
    safe = (
        str(Path(local_dir).resolve())
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )
    return get_data_root() / "merge_state" / safe / "manifest.json"


def _lock_for(local_dir: str) -> threading.Lock:
    with _GUARD:
        if local_dir not in _MANIFEST_LOCKS:
            _MANIFEST_LOCKS[local_dir] = threading.Lock()
        return _MANIFEST_LOCKS[local_dir]


def load_manifest(local_dir: str) -> dict:
    """读取 manifest，返回 {path: {"ok": bool, "remote_hash": str}}。

    文件缺失或损坏返回空 dict（等价于「无续传记录」，全部重新合并）。
    """
    path = _manifest_path(local_dir)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {}) or {}
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("合并 manifest 读取失败 %s: %s", local_dir, e)
        return {}


def save_manifest(local_dir: str, entries: dict) -> None:
    """原子写入 manifest（写临时文件后 rename，避免崩溃留下半截 JSON）。"""
    path = _manifest_path(local_dir)
    lock = _lock_for(local_dir)
    with lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"entries": entries}, f, ensure_ascii=False)
            tmp.replace(path)
        except OSError as e:
            _log.warning("合并 manifest 写入失败 %s: %s", local_dir, e)


def is_already_merged(local_dir: str, rel_path: str, manifest: dict) -> bool:
    """manifest 标记成功 且 本地文件当前内容 hash 与记录一致 → 无需再合并。

    仅比较原始字节 md5：文本文件若仅因 CRLF/LF 行尾差异被判「归一化相同」，
    merge_to_local 本会跳过写入，但此处 md5 不会相等 → 返回 False，下次仍会重抓并
    交由 merge_to_local 跳过写入（无副作用，仅多一次网络抓取）。
    """
    rec = manifest.get(rel_path)
    if not rec or not rec.get("ok"):
        return False
    remote_hash = rec.get("remote_hash") or ""
    if not remote_hash:
        return False
    target = Path(local_dir) / rel_path
    if not target.exists() or not target.is_file():
        return False
    try:
        h = hashlib.md5()
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest() == remote_hash
    except OSError:
        return False


def content_hash(content) -> str:
    """远端内容的 md5（str/bytes 统一为字节后计算）。

    与 merge_to_local 实际写入的字节一致：文本按 utf-8 编码，二进制用原始字节。
    """
    if content is None:
        return ""
    body = content.encode("utf-8") if isinstance(content, str) else content
    if body is None:
        return ""
    return hashlib.md5(body).hexdigest()
