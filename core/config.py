"""配置加载：从项目根目录 .env 自动读取连接配置。

设计原则：
- 纯逻辑、无 GUI 依赖，可在任意线程 / 测试中使用。
- 不引入 python-dotenv 依赖，自带最小解析器（KEY=VALUE，忽略空行与 # 注释，去引号）。
- 兼容多种键名别名，并容忍 .env 拼写误差（如 persoanl_access_token）。
- 真实环境变量（大写键名）优先级高于 .env 文件，便于 CI / 临时覆盖。
- Cookie 额外持久化到数据根目录 .session.json（开发态项目根 / 冻结态 ~/.jira-git-gui），启动自动读取。
"""
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from .app_paths import get_data_root
from .models import ConnectConfig

# 运行时数据根：开发态为项目根；冻结打包态为 ~/.jira-git-gui
_BASE = get_data_root()

# Cookie / 会话持久化文件
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


def _env_search_roots(project_root: Optional[Path] = None) -> list:
    """返回查找 .env 的候选根目录列表。

    冻结打包（PyInstaller / electron-builder）后，程序的 .env 会被收集进
    只读的 ``sys._MEIPASS`` 包内；运行时写入目录则移动到 ``~/.jira-git-gui``。
    因此优先在包内找 .env（默认配置），找不到再回退数据根。
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
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
#  云函数账号本地配置（含真实密码，绝不可提交到 git）
# --------------------------------------------------------------------------- #
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
# --------------------------------------------------------------------------- #
def load_hcm_whitelist(project_root: "Optional[Path]" = None) -> "dict":
    """读取 平台连接业务白名单。

    白名单项（改了会连不上平台）：
      - hcminner:           内部 OpenAPI 鉴权头 {header, value}
      - model_list_api:     真实日志查询接口路径（POST，拼在 server_url 之后）
      - reference_projects: 参考项目名（cloud-vue / core），合并比对识别用
      - platform_hosts:     真实平台域名白名单（占位，见 .local 覆盖）
      - proxy_target:       同源代理目标网关基址（占位，见 .local 覆盖）

    加载顺序（后者覆盖前者，敏感值优先来自 .local）：
      1) 内置 defaults（占位，无真实 IP/域名，可安全提交）
      2) config/hcm_whitelist.json（跟踪模板，敏感字段为占位符）
      3) config/hcm_whitelist.local.json（本机真实值，**已 gitignore，不入库**）

    注意：含真实服务器 IP / 域名的连接信息只允许存在于 *.local.json，
    该文件已被 .gitignore 忽略，请勿将真实值写回跟踪的 hcm_whitelist.json。
    找不到文件或解析失败时回退到内置默认值，保证服务不因配置缺失中断。
    """
    defaults = {
        "hcminner": {"header": "hcminner", "value": "1"},
        "model_list_api": {"path": "/api/hcm.model.list"},
        "reference_projects": {"names": ["cloud-vue", "core"]},
        "platform_hosts": {
            "hosts": []
        },
        "proxy_target": {"base_url": ""},
    }
    roots = _env_search_roots(project_root)
    merged = {k: dict(v) for k, v in defaults.items()}
    for root in roots:
        candidates = [
            root / "hcm_whitelist.json",
            root / "config" / "hcm_whitelist.json",
            root / "hcm_whitelist.local.json",
            root / "config" / "hcm_whitelist.local.json",
        ]
        for p in candidates:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                for k, v in data.items():
                    if k in merged and isinstance(v, dict) and isinstance(merged[k], dict):
                        merged[k].update(v)
                    else:
                        merged[k] = v
    # 环境变量最终覆盖（便于容器/CI 注入，不落盘）
    env_target = os.environ.get("HCM_PROXY_TARGET", "").strip()
    if env_target:
        merged.setdefault("proxy_target", {})["base_url"] = env_target
    env_hosts = os.environ.get("HCM_PLATFORM_HOSTS", "").strip()
    if env_hosts:
        merged.setdefault("platform_hosts", {})["hosts"] = [
            h.strip() for h in env_hosts.split(",") if h.strip()
        ]
    return merged
