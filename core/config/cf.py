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

from .connect import _env_search_roots
from .merge import load_merge_config

_CF_ACCOUNTS_CACHE: "Optional[list[dict]]" = None


def load_cf_accounts(project_root: "Optional[Path]" = None, force: bool = False) -> "list[dict]":
    """从 cf_accounts.local.json 读取云函数账号列表（含密码）。

    该文件必须放在本地且已被 .gitignore 忽略（cf_accounts.local.json），
    绝不能进入版本库。找不到时回退到 cf_accounts.example.json（仅结构占位，
    无真实密码）。server_url / username / password 允许为空字符串（前端会提示
    填写，不强制剔除占位项）。

    结果在内存中缓存（force=True 时失效重读），避免自动登录/按需重登时反复读盘。

    返回: [{"name", "server_url", "username", "password"}, ...]
    """
    global _CF_ACCOUNTS_CACHE
    if not force and _CF_ACCOUNTS_CACHE is not None:
        return _CF_ACCOUNTS_CACHE
    roots = _env_search_roots(project_root)
    local = None
    example = None
    for root in roots:
        for cand in (root / "cf_accounts.local.json", root / "config" / "cf_accounts.local.json"):
            if cand.exists():
                local = cand
                break
        if local is not None:
            break
    if local is None:
        for root in roots:
            for cand in (root / "cf_accounts.example.json", root / "config" / "cf_accounts.example.json"):
                if cand.exists():
                    example = cand
                    break
            if example is not None:
                break
    src = local or example
    if src is None:
        _CF_ACCOUNTS_CACHE = []
        return []
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        _CF_ACCOUNTS_CACHE = []
        return []
    raw = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: "list[dict]" = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = (item.get("server_url") or "").strip()
        out.append({
            "name": (item.get("name") or url or "未命名").strip(),
            "server_url": url,
            "username": (item.get("username") or item.get("mobile") or "").strip(),
            "password": (item.get("password") or "").strip(),
        })
    _CF_ACCOUNTS_CACHE = out
    return out


def clear_cf_accounts_cache() -> None:
    """账号配置变更后使内存缓存失效（如运行时修改了 cf_accounts.local.json 后想重新读取）。"""
    global _CF_ACCOUNTS_CACHE
    _CF_ACCOUNTS_CACHE = None


# --------------------------------------------------------------------------- #
#  平台连接业务白名单（无敏感信息，可提交到 git，属于“改了会连不上平台”的
#  保留项：hcminner 头、真实接口路径、参考项目名、真实平台域名）

