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


# ===== F4：冲突感知 3-way 合并的 base 内容缓存 =====
# 合并成功时把「远端内容（即同步快照）」按 remote_hash 落盘，供日后冲突时做 3-way。
# 仅缓存文本（str）；二进制不参与 3-way，冲突时仅提供 ours/theirs 二选一。
def _base_dir(local_dir: str) -> Path:
    safe = (
        str(Path(local_dir).resolve())
        .replace("/", "_").replace("\\", "_").replace(":", "_")
    )
    return get_data_root() / "merge_state" / safe / "bases"


def save_base(local_dir: str, remote_hash: str, content) -> None:
    """缓存某次同步的远端内容（base），按 remote_hash 去重。"""
    if not remote_hash or not isinstance(content, str):
        return
    try:
        d = _base_dir(local_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{remote_hash}.txt").write_text(content, encoding="utf-8")
    except OSError as e:
        _log.warning("合并 base 缓存写入失败 %s: %s", local_dir, e)


def load_base(local_dir: str, remote_hash: str):
    """读取缓存的 base 内容（冲突时用于 3-way）。无则返回 None。"""
    if not remote_hash:
        return None
    p = _base_dir(local_dir) / f"{remote_hash}.txt"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def detect_conflict(local_base: str, rel_path: str, remote_content) -> dict:
    """合并前的 3-way 冲突检测（F4）。

    冲突 = 存在上次同步快照 且 本地相对快照有改动 且 远端相对快照有改动
    （即两边都基于同一旧版本各自改了 → 盲覆盖会吞掉本地改动）。

    返回 conflict(bool) 及 base/ours/theirs，供前端 3-way 合并视图。
    注意：仅当 remote_content 为文本(str) 时才提供 3-way；二进制退化为 ours/theirs 二选一。
    """
    target = Path(local_base) / rel_path
    local_content = ""
    local_hash = ""
    if target.exists() and target.is_file():
        try:
            local_content = target.read_text(encoding="utf-8", errors="replace")
            local_hash = content_hash(local_content)
        except OSError:
            local_content = ""
    remote_hash = content_hash(remote_content)
    manifest = load_manifest(local_base)
    rec = manifest.get(rel_path) or {}
    snap_local = rec.get("local_hash")
    snap_remote = rec.get("remote_hash")
    has_snapshot = bool(rec.get("ok")) and bool(snap_remote)
    # 文件不存在（remote_only 首次合并）时 local_hash 为空，不算「本地改过」→ 不冲突。
    # 同时要求快照里有 local_hash：F4 之前落盘的旧 manifest 没有该字段，
    # 无法判断本地是否改过，此时按「未改」处理，避免对旧数据误报冲突
    # （首次成功合并后即会补写 local_hash，之后冲突检测恢复正常）。
    local_changed = bool(snap_local) and bool(local_hash) and local_hash != snap_local
    remote_changed = bool(remote_hash) and remote_hash != snap_remote
    conflict = bool(has_snapshot) and local_changed and remote_changed
    base = None
    if conflict and snap_remote:
        try:
            base = load_base(local_base, snap_remote)
        except Exception:
            base = None
    return {
        "conflict": conflict,
        "kind": "both" if conflict else None,
        "remote_hash": remote_hash,
        "local_hash": local_hash,
        "base": base,
        "ours": local_content,
        "theirs": remote_content if isinstance(remote_content, str) else None,
        "is_binary": not isinstance(remote_content, str),
        "snapshot": rec,
    }
