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
