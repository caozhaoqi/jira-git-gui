# -*- coding: utf-8 -*-
"""配置加载子模块（由 core/config.py 拆分，保持 import 兼容）。"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

from core.app_paths import get_data_root
from core.models import ConnectConfig
from core.secure_storage import load_secret, store_secret

_BASE = get_data_root()
_SESSION_FILE = _BASE / ".session.json"

def _parse_env_file(path: Path) -> dict:
    """极简 .env 解析：忽略空行与 # 注释，按 KEY=VALUE 拆分，去前后空白与成对引号。"""
    data: dict = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # 去掉成对引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            data[key] = load_secret(val) if val.startswith("keyring://") else val
    return data


# ConnectConfig 字段 -> 可接受的全部键名别名（顺序即优先级）
_FIELD_ALIASES = {
    "jira_url": ("JIRA_URL", "jira_url", "JIRA_HOST", "host"),
    "username": ("JIRA_USERNAME", "username", "user"),
    "mode": ("JIRA_MODE", "mode"),
    "pat": ("JIRA_PAT", "PERSONAL_ACCESS_TOKEN", "personal_access_token",
            "PAT", "pat", "persoanl_access_token"),
    "cookie": ("JIRA_COOKIE", "cookie", "COOKIE"),
    "http_proxy": ("JIRA_HTTP_PROXY", "jira_http_proxy"),
}

_SECRET_ALIASES = {
    "personal_access_token": {
        "JIRA_PAT", "PERSONAL_ACCESS_TOKEN", "personal_access_token",
        "PAT", "pat", "persoanl_access_token",
    },
    "cookie": {"JIRA_COOKIE", "cookie", "COOKIE"},
}


def _migrate_env_secrets(path: Path) -> None:
    """Move legacy Jira secrets in the user .env into the OS credential store."""
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = []
    changed = False
    for raw in lines:
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$", raw)
        if not match:
            updated.append(raw)
            continue
        leading, key, separator, value = match.groups()
        value = value.strip()
        canonical = next((name for name, aliases in _SECRET_ALIASES.items() if key in aliases), None)
        if canonical and value:
            quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
            raw_value = value[1:-1] if quoted else value
            if raw_value.startswith("keyring://"):
                load_secret(raw_value)
            else:
                ref = store_secret(f"jira:{key}", raw_value)
                value = f"{value[0]}{ref}{value[0]}" if quoted else ref
                changed = True
        updated.append(f"{leading}{key}{separator}{value}")
    if changed:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(updated) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)


def build_config(env: dict) -> ConnectConfig:
    """把已解析的键值对映射成 ConnectConfig（按别名查找，后者不覆盖前者）。"""
    def pick(*names: str) -> str:
        for n in names:
            if n in env and env[n] != "":
                return env[n]
        return ""

    return ConnectConfig(
        jira_url=pick(*_FIELD_ALIASES["jira_url"]).rstrip("/"),
        username=pick(*_FIELD_ALIASES["username"]),
        mode=(pick(*_FIELD_ALIASES["mode"]) or "pat").lower(),
        pat=pick(*_FIELD_ALIASES["pat"]),
        cookie=pick(*_FIELD_ALIASES["cookie"]),
        http_proxy=pick(*_FIELD_ALIASES["http_proxy"]),
    )


def _env_search_roots(project_root: Optional[Path] = None) -> list:
    """返回查找 .env 的候选根目录列表。

    冻结打包（PyInstaller / electron-builder）后，用户配置从数据根目录读取。
    运行时凭据不会从程序包内加载。
    """
    if project_root is not None:
        return [Path(project_root)]
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
    roots.append(_BASE)
    return roots


def load_config(project_root: Optional[Path] = None) -> Tuple[ConnectConfig, bool, str]:
    """读取 <root>/.env；存在则以其为默认配置，返回 (config, loaded, env_path)。"""
    for root in _env_search_roots(project_root):
        env_path = root / ".env"
        if env_path.exists():
            if project_root is None and root == _BASE:
                _migrate_env_secrets(env_path)
            env = _parse_env_file(env_path)
            # 真实环境变量（大写键名）优先覆盖 .env 同名键
            for names in _FIELD_ALIASES.values():
                for n in names:
                    if n in os.environ and os.environ[n] != "":
                        env[n] = os.environ[n]
            return build_config(env), True, str(env_path)
    fallback = _env_search_roots(project_root)[-1] / ".env"
    return ConnectConfig(), False, str(fallback)


# --------------------------------------------------------------------------- #
#  Cookie / 会话持久化
# --------------------------------------------------------------------------- #
