# -*- coding: utf-8 -*-
"""Kibana 站点配置（多站点：新增 / 更新 / 切换 / 删除）。

存储位置（对齐 Clash / CF 的「local 优先 + example 回退」机制）：
- ``config/kibana_sites.local.json``  —— 真实配置，**含明文密码，已 .gitignore**
- ``config/kibana_sites.example.json`` —— 占位模板，随仓库提交

首次运行（local 与 example 均不存在）会用 ``DEFAULT_SITE_SEED`` 播种 local 文件，
保证用户开箱即用；播种内容视为「上一次运维留下的现场配置」，不进版本库。
"""
import json
import os
import threading
from pathlib import Path

from core.errors import UserError

# core/kibana/../.. → 项目根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
KIBANA_SITES_FILE = _PROJECT_ROOT / "config" / "kibana_sites.local.json"
_KIBANA_EXAMPLE_FILE = _PROJECT_ROOT / "config" / "kibana_sites.example.json"

# 播种用的默认站点（与 k8s 一致：不入库，仅落在 .local.json）
DEFAULT_SITE_SEED = {
    "sites": {
        "hnrc": {
            "label": "default",
            "base_url": "http://localhost/kibana",
            "username": "elastic",
            "password": "elastic",
            "index_pattern": "logstash-*",
            "time_field": "es_time",
            "msg_field": "log",
            "field_prefix": "kubernetes",
            "verify_ssl": False,
            "timeout": 30,
        },
    },
    "current": "hnrc",
}

# 站点字段与其默认值（新增站点时缺省补齐）
SITE_DEFAULTS = {
    "label": "",
    "base_url": "",
    "username": "",
    "password": "",
    "index_pattern": "logstash-*",
    "time_field": "es_time",
    "msg_field": "log",
    "field_prefix": "kubernetes",
    "verify_ssl": False,
    "timeout": 30,
}

_lock = threading.Lock()


def _search_roots():
    """候选配置根目录：项目根 → 冻结包内 → 数据根。"""
    roots = [_PROJECT_ROOT]
    try:
        from core.config.connect import _env_search_roots
        roots += list(_env_search_roots())
    except Exception:  # noqa: BLE001
        pass
    return roots


def _candidate_files():
    """按优先级返回候选配置文件路径（local 优先，example 兜底）。"""
    cands = [KIBANA_SITES_FILE]
    for r in _search_roots():
        cands += [
            r / "config" / "kibana_sites.local.json",
            r / "config" / "kibana_sites.example.json",
            r / "kibana_sites.local.json",
            r / "kibana_sites.example.json",
        ]
    # 去重保序
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _seed_defaults():
    save_sites(DEFAULT_SITE_SEED)
    return json.loads(json.dumps(DEFAULT_SITE_SEED))


def _normalize(data: dict) -> dict:
    """补齐站点字段默认值、修正 current 指向。"""
    sites = data.get("sites")
    if not isinstance(sites, dict) or not sites:
        return None
    fixed = {}
    for name, site in sites.items():
        if not isinstance(site, dict):
            continue
        item = dict(SITE_DEFAULTS)
        item.update(site)
        # 去空格，避免 URL / 账号前后空白导致的诡异 401
        for k in ("base_url", "username", "index_pattern", "time_field",
                  "msg_field", "field_prefix"):
            item[k] = str(item.get(k) or "").strip()
        base = item["base_url"]
        # 兼容「填了 Kibana 首页 URL」的情况：只保留到 /kibana（若有）
        item["base_url"] = base.rstrip("/")
        fixed[name] = item
    if not fixed:
        return None
    cur = data.get("current")
    if cur not in fixed:
        cur = next(iter(fixed))
    return {"sites": fixed, "current": cur}


def _resolve_sites() -> tuple:
    """定位站点配置，返回 ``(data, source)``。

    ``source`` ∈ ``{local, example, seed}``，用于前端提示「当前用的是哪份配置」。

    判定顺序（对齐 ``core/k8s.env.load_envs`` 的「缺 local 就播种」）：
    1. ``config/kibana_sites.local.json`` 存在且可解析 → 用它（**真实配置**）
    2. 不存在 → 用 :data:`DEFAULT_SITE_SEED` 播种一份 local 再用
    3. 播不了（只读文件系统等）→ 才回退 ``.example.json`` 模板
    """
    # 1) local（含其它搜索根下的同名文件）
    for path in _candidate_files():
        if not path.name.endswith(".local.json") or not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        norm = _normalize(raw)
        if norm:
            return norm, "local"
    # 2) 播种
    try:
        return _seed_defaults(), "seed"
    except OSError:
        pass
    # 3) 模板兜底（占位值，不可用但至少不崩）
    for path in _candidate_files():
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        norm = _normalize(raw)
        if norm:
            return norm, "example"
    return {"sites": {}, "current": None}, "missing"


def load_sites() -> dict:
    """读取站点配置 ``{sites:{name:{...}}, current:name}``。"""
    return _resolve_sites()[0]


def sites_source() -> str:
    """返回当前生效配置的来源：``local`` / ``seed`` / ``example`` / ``missing``。"""
    return _resolve_sites()[1]


def save_sites(data: dict) -> None:
    """写回配置。只写 local 文件（example 是模板，不该被运行时改写）。"""
    path = KIBANA_SITES_FILE
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)  # 含密码，仅当前用户可读写
        except OSError:
            pass


def list_sites() -> list:
    """返回站点列表（**剥离密码**），键与前端 KibanaSite 一致。"""
    data = load_sites()
    cur = data.get("current")
    out = []
    for name, s in data["sites"].items():
        item = {k: v for k, v in s.items() if k != "password"}
        item["name"] = name
        item["is_current"] = name == cur
        item["has_password"] = bool(s.get("password"))
        out.append(item)
    return out


def get_site(name: str = None) -> tuple:
    """解析站点名 -> ``(name, site_dict)``。name 为 None 取 current。"""
    data = load_sites()
    if name is None:
        name = data.get("current")
    site = data["sites"].get(name)
    if site is None:
        raise UserError("未找到 Kibana 站点 '%s'，请先在「站点管理」中配置。" % name)
    if not (site.get("base_url") or "").strip():
        raise UserError("站点 '%s' 未配置 Kibana 地址。" % name)
    return name, site


def add_or_update_site(name: str, **fields) -> dict:
    """新增 / 更新站点。``password`` 传空串表示「保持原值不变」。"""
    name = (name or "").strip()
    if not name:
        raise UserError("站点名不能为空")
    data = load_sites()
    site = dict(data["sites"].get(name, {}))
    # 先补默认值，再叠加传入字段
    merged = dict(SITE_DEFAULTS)
    merged.update(site)
    for k, v in fields.items():
        if v is None:
            continue
        if k == "password" and v == "":
            continue  # 空密码 = 不修改
        merged[k] = v
    merged["base_url"] = str(merged.get("base_url") or "").strip().rstrip("/")
    if not merged["base_url"]:
        raise UserError("Kibana 地址不能为空")
    data["sites"][name] = merged
    if data.get("current") not in data["sites"]:
        data["current"] = name
    save_sites(data)
    return data


def set_current_site(name: str) -> dict:
    data = load_sites()
    if name not in data["sites"]:
        raise UserError("站点 '%s' 不存在。" % name)
    data["current"] = name
    save_sites(data)
    return data


def delete_site(name: str) -> dict:
    data = load_sites()
    if name in data["sites"]:
        del data["sites"][name]
        if data.get("current") == name:
            data["current"] = next(iter(data["sites"]), None)
        save_sites(data)
    return data


def clear_sites_cache() -> None:
    """配置变更后调用。

    当前实现每次 ``load_sites()`` 都直接读盘（无进程内缓存），因此这里只做
    显式占位，保持与 cf / hcm 同类模块一致的调用面，日后若加内存缓存无需改调用方。
    """
    return None
