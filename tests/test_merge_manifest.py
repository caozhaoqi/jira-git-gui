# -*- coding: utf-8 -*-
"""merge_manifest 纯逻辑单测（离线，无 Jira / 无真实合并）。

锁住断点续传的核心判定：
- content_hash 对 str/bytes 计算一致
- save/load 往返正确（损坏文件降级为空 dict）
- is_already_merged 在「manifest ok + 本地内容一致」时返回 True，
  在「manifest 未记录 / ok=False / 本地被改 / 本地缺失」时返回 False。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.diff import merge_manifest as _mm  # noqa: E402


def test_content_hash_str_and_bytes(tmp_path):
    assert _mm.content_hash("hello\n") == _mm.content_hash("hello\n")
    assert _mm.content_hash(b"hello\n") == _mm.content_hash("hello\n")
    assert _mm.content_hash(None) == ""
    # 不同内容不同 hash
    assert _mm.content_hash("a") != _mm.content_hash("b")


def test_save_load_roundtrip(tmp_path):
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    entries = {
        "a.txt": {"ok": True, "remote_hash": "deadbeef"},
        "b.txt": {"ok": False, "remote_hash": ""},
    }
    _mm.save_manifest(str(tmp_path / "repo"), entries)
    loaded = _mm.load_manifest(str(tmp_path / "repo"))
    assert loaded == entries


def test_load_missing_or_corrupt_is_empty(tmp_path):
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    # 缺失
    assert _mm.load_manifest(str(tmp_path / "nope")) == {}
    # 损坏 JSON
    p = _mm._manifest_path(str(tmp_path / "repo"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    assert _mm.load_manifest(str(tmp_path / "repo")) == {}


def test_is_already_merged_true(tmp_path):
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    local = tmp_path / "repo"
    local.mkdir()
    body = "remote content\n"
    (local / "a.txt").write_text(body, encoding="utf-8")
    h = _mm.content_hash(body)
    manifest = {"a.txt": {"ok": True, "remote_hash": h}}
    assert _mm.is_already_merged(str(local), "a.txt", manifest) is True


def test_is_already_merged_false_cases(tmp_path):
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    local = tmp_path / "repo"
    local.mkdir()
    body = "remote content\n"
    (local / "a.txt").write_text(body, encoding="utf-8")
    h = _mm.content_hash(body)
    # 未记录
    assert _mm.is_already_merged(str(local), "a.txt", {}) is False
    # 记录但 ok=False
    assert _mm.is_already_merged(str(local), "a.txt",
                                 {"a.txt": {"ok": False, "remote_hash": h}}) is False
    # 记录但 remote_hash 为空
    assert _mm.is_already_merged(str(local), "a.txt",
                                 {"a.txt": {"ok": True, "remote_hash": ""}}) is False
    # 本地内容被改
    (local / "a.txt").write_text("LOCAL EDIT\n", encoding="utf-8")
    assert _mm.is_already_merged(str(local), "a.txt",
                                 {"a.txt": {"ok": True, "remote_hash": h}}) is False
    # 本地文件缺失
    (local / "a.txt").unlink()
    assert _mm.is_already_merged(str(local), "a.txt",
                                 {"a.txt": {"ok": True, "remote_hash": h}}) is False


# ===== F4：冲突感知 3-way（base 缓存 + detect_conflict）===== #
BASE = "base\n"


def _seed_synced(tmp_path, rel="a.txt", base=BASE):
    """构造「已同步过一次」的状态：manifest 有 local/remote 双 hash + base 缓存。

    返回 (local_dir路径, rel, base_hash)。
    """
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    local = tmp_path / "repo"
    local.mkdir(exist_ok=True)
    h = _mm.content_hash(base)
    _mm.save_manifest(str(local), {rel: {"ok": True, "remote_hash": h, "local_hash": h}})
    _mm.save_base(str(local), h, base)
    return local, rel, h


def test_base_cache_roundtrip(tmp_path):
    local, _rel, h = _seed_synced(tmp_path)
    assert _mm.load_base(str(local), h) == BASE
    # 未缓存 / 空 hash → None
    assert _mm.load_base(str(local), "no-such-hash") is None
    assert _mm.load_base(str(local), "") is None
    # 二进制（bytes）不缓存（无法参与 3-way 文本合并）
    _mm.save_base(str(local), "binhash", b"\x00\x01")
    assert _mm.load_base(str(local), "binhash") is None


def test_detect_conflict_cases(tmp_path):
    local, rel, _h = _seed_synced(tmp_path)
    ldir = str(local)

    # A. 本地未改 + 远端改 → 无冲突（可安全自动合并）
    (local / rel).write_text(BASE, encoding="utf-8")
    assert _mm.detect_conflict(ldir, rel, "base\nremote-add\n")["conflict"] is False

    # B. 本地改 + 远端未改 → 无冲突（沿用既有「远端为准」语义）
    (local / rel).write_text("base\nlocal-add\n", encoding="utf-8")
    assert _mm.detect_conflict(ldir, rel, BASE)["conflict"] is False

    # C. 双方都改 → 冲突，且带回 base/ours/theirs 供 3-way
    info = _mm.detect_conflict(ldir, rel, "base\nremote-add\n")
    assert info["conflict"] is True
    assert info["kind"] == "both"
    assert info["base"] == BASE
    assert info["ours"] == "base\nlocal-add\n"
    assert info["theirs"] == "base\nremote-add\n"
    assert info["is_binary"] is False


def test_detect_conflict_no_snapshot_and_legacy(tmp_path):
    """无快照（首次合并）不冲突；F4 之前的旧 manifest（无 local_hash）也不误报。"""
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    local = tmp_path / "repo"
    local.mkdir()
    target = local / "a.txt"
    target.write_text("x\n", encoding="utf-8")

    # 无快照 → 不冲突
    assert _mm.detect_conflict(str(local), "a.txt", "y\n")["conflict"] is False

    # 旧 manifest（只有 remote_hash，没有 local_hash）：即便远端改了也不误报
    _mm.save_manifest(str(local), {"a.txt": {"ok": True,
                                            "remote_hash": _mm.content_hash("y\n")}})
    target.write_text("local-edit\n", encoding="utf-8")
    assert _mm.detect_conflict(str(local), "a.txt", "z\n")["conflict"] is False


def test_detect_conflict_binary_remote(tmp_path):
    """远端为二进制时无法 3-way：标记 is_binary 且 theirs 置空（由调用方降级处理）。"""
    _mm.get_data_root = lambda: tmp_path / "appdata"  # type: ignore[assignment]
    local = tmp_path / "repo"
    local.mkdir()
    (local / "a.bin").write_text("x\n", encoding="utf-8")
    info = _mm.detect_conflict(str(local), "a.bin", b"\x00\x01\x02")
    assert info["is_binary"] is True
    assert info["theirs"] is None
