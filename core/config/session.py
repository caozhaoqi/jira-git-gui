# -*- coding: utf-8 -*-
"""配置加载子模块（由 core/config.py 拆分，保持 import 兼容）。"""
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from core.app_paths import get_data_root
from core.models import ConnectConfig
from core.secure_storage import delete_secret, load_secret, store_secret

_BASE = get_data_root()
_SESSION_FILE = _BASE / ".session.json"
_SESSION_COOKIE_NAME = "jira.session-cookie"


def _write_session(data: dict) -> None:
    _BASE.mkdir(parents=True, exist_ok=True)
    tmp = _SESSION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(_SESSION_FILE)

from .connect import _BASE, _SESSION_FILE

def save_session(cookie: str, jira_url: str = "", username: str = "") -> None:
    """Store the Jira cookie in the OS credential store and metadata in .session.json."""
    data = {"jira_url": jira_url, "username": username}
    if cookie:
        data["cookie_ref"] = store_secret(_SESSION_COOKIE_NAME, cookie)
    old_ref = None
    if _SESSION_FILE.exists():
        try:
            old = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                old_ref = old.get("cookie_ref")
        except (OSError, ValueError):
            pass
    _write_session(data)
    if old_ref and old_ref != data.get("cookie_ref"):
        delete_secret(old_ref)


def load_session() -> dict:
    """Load session metadata and migrate any legacy plaintext cookie to OS storage."""
    if not _SESSION_FILE.exists():
        return {}
    data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid Jira session file format.")
    cookie_ref = data.get("cookie_ref")
    if cookie_ref:
        data["cookie"] = load_secret(cookie_ref)
    elif data.get("cookie"):
        data["cookie_ref"] = store_secret(_SESSION_COOKIE_NAME, data["cookie"])
        _write_session({key: value for key, value in data.items() if key != "cookie"})
    return data


def clear_session() -> None:
    """Delete the saved session and its OS credential."""
    if _SESSION_FILE.exists():
        data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("cookie_ref"):
            delete_secret(data["cookie_ref"])
        _SESSION_FILE.unlink()


def get_session_path() -> str:
    """返回会话文件路径（供 UI 提示用户）。"""
    return str(_SESSION_FILE)


# --------------------------------------------------------------------------- #
#  合并功能：仓库映射配置（优先从 .env 加载，避免硬编码敏感信息）
