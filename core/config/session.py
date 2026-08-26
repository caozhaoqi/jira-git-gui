# -*- coding: utf-8 -*-
"""配置加载子模块（由 core/config.py 拆分，保持 import 兼容）。"""
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from core.app_paths import get_data_root
from core.models import ConnectConfig

_BASE = get_data_root()
_SESSION_FILE = _BASE / ".session.json"

from .connect import _BASE, _SESSION_FILE

def save_session(cookie: str, jira_url: str = "", username: str = "") -> None:
    """把 cookie 等会话信息保存到 .session.json。"""
    try:
        data = {"cookie": cookie, "jira_url": jira_url, "username": username}
        _SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_session() -> dict:
    """从 ~/.jira_git_gui/session.json 读取会话；不存在返回空 dict。"""
    try:
        if _SESSION_FILE.exists():
            return json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def clear_session() -> None:
    """删除会话文件。"""
    try:
        if _SESSION_FILE.exists():
            _SESSION_FILE.unlink()
    except Exception:
        pass


def get_session_path() -> str:
    """返回会话文件路径（供 UI 提示用户）。"""
    return str(_SESSION_FILE)


# --------------------------------------------------------------------------- #
#  合并功能：仓库映射配置（优先从 .env 加载，避免硬编码敏感信息）

