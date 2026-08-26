# -*- coding: utf-8 -*-
"""服务配置管理路由（云函数 / HCM / Jira 系统配置）。

提供三套配置的手动增删改查：
  1. 云函数 / HCM 服务账号 —— 读写 config/cf_accounts.local.json
     （每条：name / server_url / username / password / type）
  2. HCM 代理与对象配置 —— 读写 config/hcm_whitelist.local.json
     （proxy_target.base_url / token / platform_hosts.hosts）
  3. Jira 系统配置 —— 读写 <data_root>/.env
     （jira_url / username / JIRA_MODE / personal_access_token / cookie）

设计要点：
  - 路径定位复用 core.config.cf._env_search_roots，与 load 逻辑完全一致，
    保证写入的就是运行时真正读取的那份 *.local.json。
  - 密码不出网：GET 列表只返回 has_password 布尔，绝不下发明文密码；
    更新时若密码留空表示「保留原密码」。
  - Jira 配置写入 .env 时仅更新相关键，保留其它键（MERGE_*、password）
    与注释，避免误伤。Jira 配置在 load_config() 时实时读取，无需重启。
  - 写盘后立即 clear_cf_accounts_cache()，并刷新 api.common /
    api.hcm.hcm_core 的模块级代理变量，使配置无需重启即时生效。
"""
import json
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.common import app, logger, _PROJECT_ROOT
from core.config.cf import _env_search_roots, clear_cf_accounts_cache
from core.config.connect import _BASE as _ENV_BASE, load_config as _load_connect_config

router = APIRouter()


# --------------------------------------------------------------------------- #
#  文件路径定位（与 core.config 加载逻辑对齐）
# --------------------------------------------------------------------------- #
def _cf_path() -> Path:
    """定位 cf_accounts.local.json：优先已存在的，否则落在 <data_root>/config 下。"""
    for root in _env_search_roots(None):
        for cand in (root / "cf_accounts.local.json", root / "config" / "cf_accounts.local.json"):
            if cand.exists():
                return cand
    return _env_search_roots(None)[0] / "config" / "cf_accounts.local.json"


def _hcm_path() -> Path:
    """定位 hcm_whitelist.local.json。"""
    for root in _env_search_roots(None):
        for cand in (root / "hcm_whitelist.local.json", root / "config" / "hcm_whitelist.local.json"):
            if cand.exists():
                return cand
    return _env_search_roots(None)[0] / "config" / "hcm_whitelist.local.json"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("[服务配置] 读取 %s 失败: %s", path, e)
        return default
    return data if isinstance(data, (dict, list)) else default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 原子替换，避免半写文件


# --------------------------------------------------------------------------- #
#  请求 / 响应模型
# --------------------------------------------------------------------------- #
class CloudFunctionReq(BaseModel):
    """云函数 / HCM 服务账号条目。"""
    name: str = ""
    server_url: str = ""
    username: str = ""
    password: str = ""
    type: str = "云函数"   # 云函数 | HCM（仅前端分组标签，不影响登录逻辑）


class HcmConfigReq(BaseModel):
    """HCM 代理与对象配置。"""
    base_url: str = ""
    token: str = ""
    hosts: List[str] = []


class JiraConfigReq(BaseModel):
    """Jira 系统配置（对应 ConnectConfig）。pat / cookie 留空表示保留现有值。"""
    jira_url: str = ""
    username: str = ""
    mode: str = "pat"        # pat | cookie
    pat: str = ""            # 个人访问令牌 Personal Access Token
    cookie: str = ""         # JSESSIONID=...; atlassian.xsrf.token=...


# --------------------------------------------------------------------------- #
#  云函数 / HCM 服务账号
# --------------------------------------------------------------------------- #
@router.get("/api/services/cloud-functions")
async def list_cloud_functions():
    """列出所有云函数 / HCM 服务账号（密码以 has_password 标记，不下发明文）。"""
    data = _read_json(_cf_path(), {"accounts": []})
    accounts = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(accounts, list):
        accounts = []
    out = []
    for i, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            continue
        out.append({
            "index": i,
            "name": acc.get("name", ""),
            "server_url": acc.get("server_url", ""),
            "username": acc.get("username", ""),
            "type": acc.get("type", "云函数"),
            "has_password": bool((acc.get("password") or "").strip()),
        })
    return {"ok": True, "path": str(_cf_path()), "items": out}


@router.post("/api/services/cloud-functions")
async def add_cloud_function(req: CloudFunctionReq):
    """新增一条云函数 / HCM 服务账号。"""
    if not req.server_url.strip() and not req.name.strip():
        return {"ok": False, "error": "服务名称与服务器地址至少填写一项"}
    data = _read_json(_cf_path(), {"accounts": []})
    if not isinstance(data, dict) or "accounts" not in data:
        data = {"accounts": []}
    accounts: list = data.setdefault("accounts", [])
    accounts.append({
        "name": req.name.strip(),
        "server_url": req.server_url.strip(),
        "username": req.username.strip(),
        "password": req.password,
        "type": req.type or "云函数",
    })
    _write_json(_cf_path(), data)
    clear_cf_accounts_cache()
    logger.info("[服务配置] 新增云函数账号 %s (%s)", req.name, req.server_url)
    return {"ok": True, "count": len(accounts)}


@router.put("/api/services/cloud-functions/{index}")
async def update_cloud_function(index: int, req: CloudFunctionReq):
    """按序号更新一条服务账号。password 留空表示保留原密码。"""
    data = _read_json(_cf_path(), {"accounts": []})
    accounts = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(accounts, list) or index < 0 or index >= len(accounts):
        return {"ok": False, "error": f"序号 {index} 不存在"}
    old = accounts[index]
    new_pw = req.password if req.password else (old.get("password") or "")
    accounts[index] = {
        "name": req.name.strip(),
        "server_url": req.server_url.strip(),
        "username": req.username.strip(),
        "password": new_pw,
        "type": req.type or old.get("type", "云函数"),
    }
    _write_json(_cf_path(), data)
    clear_cf_accounts_cache()
    logger.info("[服务配置] 更新云函数账号 #%d -> %s", index, req.name)
    return {"ok": True}


@router.delete("/api/services/cloud-functions/{index}")
async def delete_cloud_function(index: int):
    """按序号删除一条服务账号。"""
    data = _read_json(_cf_path(), {"accounts": []})
    accounts = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(accounts, list) or index < 0 or index >= len(accounts):
        return {"ok": False, "error": f"序号 {index} 不存在"}
    removed = accounts.pop(index)
    _write_json(_cf_path(), data)
    clear_cf_accounts_cache()
    logger.info("[服务配置] 删除云函数账号 #%d (%s)", index, removed.get("name", ""))
    return {"ok": True, "removed": removed.get("name", "")}


# --------------------------------------------------------------------------- #
#  HCM 代理与对象配置
# --------------------------------------------------------------------------- #
@router.get("/api/services/hcm-config")
async def get_hcm_config():
    """读取 HCM 代理配置（token 以 has_token 标记，不下发明文）。"""
    data = _read_json(_hcm_path(), {})
    proxy = data.get("proxy_target", {}) if isinstance(data, dict) else {}
    hosts = data.get("platform_hosts", {}).get("hosts", []) if isinstance(data, dict) else []
    token = (data.get("token", "") or "") if isinstance(data, dict) else ""
    return {
        "ok": True,
        "path": str(_hcm_path()),
        "base_url": (proxy.get("base_url", "") if isinstance(proxy, dict) else ""),
        "has_token": bool(token.strip()),
        "hosts": hosts if isinstance(hosts, list) else [],
    }


@router.post("/api/services/hcm-config")
async def save_hcm_config(req: HcmConfigReq):
    """保存 HCM 代理配置：base_url / token / platform_hosts。"""
    data = _read_json(_hcm_path(), {})
    if not isinstance(data, dict):
        data = {}
    # 保留已有但前端不管理的字段（如 hcminner / reference_projects）
    data.setdefault("platform_hosts", {})
    if not isinstance(data["platform_hosts"], dict):
        data["platform_hosts"] = {}
    data["platform_hosts"]["hosts"] = [h.strip() for h in req.hosts if h.strip()]
    data["proxy_target"] = {"base_url": req.base_url.strip()}
    # token 留空则保留原 token（避免误清空）
    if req.token.strip():
        data["token"] = req.token.strip()
    _write_json(_hcm_path(), data)

    # 即时刷新模块级变量，使 HCM 代理 / 直连无需重启即生效
    try:
        import api.common as common
        import api.hcm.hcm_core as hcm_core
        common._HCM_PROXY_TARGET = common.HCM_PROXY_TARGET = req.base_url.strip()
        common._HCM_PRESET_TOKEN = common.HCM_PRESET_TOKEN = (req.token.strip()
                                                              or common._HCM_PRESET_TOKEN)
        hcm_core._HCM_PROXY_TARGET = req.base_url.strip()
        hcm_core._HCM_PRESET_TOKEN = common._HCM_PRESET_TOKEN
    except Exception as e:
        logger.warning("[服务配置] 刷新 HCM 模块变量失败（重启后生效）: %s", e)

    logger.info("[服务配置] 保存 HCM 代理配置 base_url=%s hosts=%d", req.base_url, len(req.hosts))
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Jira 系统配置（读写 <data_root>/.env：jira_url / username / personal_access_token / cookie）
# --------------------------------------------------------------------------- #
@router.get("/api/services/jira-config")
async def get_jira_config():
    """读取 Jira 系统配置（pat / cookie 以布尔标记，不下发明文）。"""
    cfg, loaded, path = _load_connect_config()
    return {
        "ok": True,
        "loaded": loaded,
        "path": path,
        "jira_url": cfg.jira_url,
        "username": cfg.username,
        "mode": cfg.mode or "pat",
        "has_pat": bool(cfg.pat),
        "has_cookie": bool(cfg.cookie),
    }


@router.post("/api/services/jira-config")
async def save_jira_config(req: JiraConfigReq):
    """保存 Jira 系统配置到 .env。

    - 仅更新与 Jira 相关的键（jira_url / username / JIRA_MODE /
      personal_access_token / cookie），其余键（MERGE_*、password 等）与
      注释、空行一律原样保留，避免误伤其它配置。
    - personal_access_token / cookie 仅在非空时覆盖；留空表示保留现有值。
    """
    env_path = _ENV_BASE / ".env"
    desired = {
        "jira_url": req.jira_url.strip().rstrip("/"),
        "username": req.username.strip(),
        "JIRA_MODE": (req.mode.strip() or "pat").lower(),
    }
    if req.pat:
        desired["personal_access_token"] = req.pat
    if req.cookie:
        desired["cookie"] = req.cookie

    out_lines = []
    seen = set()
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", raw)
            if m and m.group(1) in desired and m.group(1) not in seen:
                out_lines.append(f"{m.group(1)}={desired[m.group(1)]}")
                seen.add(m.group(1))
            else:
                out_lines.append(raw)
    for k, v in desired.items():
        if k not in seen:
            out_lines.append(f"{k}={v}")

    tmp = env_path.with_name(".env.tmp")
    tmp.write_text("\n".join(out_lines).rstrip("\n") + "\n", encoding="utf-8")
    tmp.replace(env_path)

    logger.info("[服务配置] 保存 Jira 配置 jira_url=%s mode=%s", req.jira_url, req.mode)
    return {"ok": True, "path": str(env_path)}


# --------------------------------------------------------------------------- #
#  管理页面（独立于 React 构建产物，经 FastAPI 路由提供，避免被 vite 构建覆盖）
# --------------------------------------------------------------------------- #
@router.get("/services-config")
async def services_config_page():
    """返回服务配置管理页面（web/services-config.html）。"""
    html = _PROJECT_ROOT / "web" / "services-config.html"
    if not html.exists():
        return {"ok": False, "error": "页面文件缺失：web/services-config.html"}
    return FileResponse(str(html), media_type="text/html")
