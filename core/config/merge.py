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

from .connect import _env_search_roots, _parse_env_file

def load_merge_config(project_root: "Optional[Path]" = None) -> "dict":
    """从 .env 读取合并功能的仓库映射与参数。

    返回:
        {
            "repo_map": {远程仓库名: 本地目录},
            "scan_workers": int,
            "tree_ttl": int,
            "file_ttl": int,
        }
    """
    root = Path(project_root) if project_root else _BASE
    env_path = None
    for cand in _env_search_roots(project_root):
        p = cand / ".env"
        if p.exists():
            env_path = p
            break
    env = _parse_env_file(env_path) if env_path else {}

    repo_map: "dict[str, str]" = {}
    for key, val in env.items():
        if key.startswith("MERGE_REPO_") and "|" in val:
            # 格式：<远程仓库名>|<本地绝对路径>
            name, _, local_dir = val.partition("|")
            name = name.strip()
            local_dir = local_dir.strip()
            if name and local_dir:
                repo_map[name] = local_dir

    def _int(key: str, default: int) -> int:
        try:
            return int(env.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
            "repo_map": repo_map,
            "scan_workers": _int("MERGE_SCAN_WORKERS", 3),
            "merge_workers": _int("MERGE_WORKERS", 4),
            "tree_ttl": _int("MERGE_CACHE_TREE_TTL", 3600),
            "file_ttl": _int("MERGE_CACHE_FILE_TTL", 86400),
            "scan_roots": env.get("MERGE_SCAN_ROOTS", "").strip(),
        }





