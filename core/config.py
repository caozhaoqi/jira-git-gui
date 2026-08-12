"""配置加载：从项目根目录 .env 自动读取连接配置。

设计原则：
- 纯逻辑、无 GUI 依赖，可在任意线程 / 测试中使用。
- 不引入 python-dotenv 依赖，自带最小解析器（KEY=VALUE，忽略空行与 # 注释，去引号）。
- 兼容多种键名别名，并容忍 .env 拼写误差（如 persoanl_access_token）。
- 真实环境变量（大写键名）优先级高于 .env 文件，便于 CI / 临时覆盖。
"""
import os
from pathlib import Path
from typing import Optional, Tuple

from .models import ConnectConfig

# 项目根目录 = <root>/core/config.py 的上两级
_BASE = Path(__file__).resolve().parent.parent


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
            data[key] = val
    return data


# ConnectConfig 字段 -> 可接受的全部键名别名（顺序即优先级）
_FIELD_ALIASES = {
    "jira_url": ("JIRA_URL", "jira_url", "JIRA_HOST", "host"),
    "username": ("JIRA_USERNAME", "username", "user"),
    "mode": ("JIRA_MODE", "mode"),
    "pat": ("JIRA_PAT", "PERSONAL_ACCESS_TOKEN", "personal_access_token",
            "PAT", "pat", "persoanl_access_token"),
    "cookie": ("JIRA_COOKIE", "cookie", "COOKIE"),
}


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
    )


def load_config(project_root: Optional[Path] = None) -> Tuple[ConnectConfig, bool, str]:
    """读取 <root>/.env；存在则以其为默认配置，返回 (config, loaded, env_path)。"""
    root = Path(project_root) if project_root else _BASE
    env_path = root / ".env"
    if not env_path.exists():
        return ConnectConfig(), False, str(env_path)
    env = _parse_env_file(env_path)
    # 真实环境变量（大写键名）优先覆盖 .env 同名键
    for names in _FIELD_ALIASES.values():
        for n in names:
            if n in os.environ and os.environ[n] != "":
                env[n] = os.environ[n]
    return build_config(env), True, str(env_path)
