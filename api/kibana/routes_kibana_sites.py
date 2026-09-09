# -*- coding: utf-8 -*-
"""Kibana 站点管理路由：列表 / 保存 / 删除 / 切换 / 连通性测试。

返回体统一 ``{"ok": bool, ...}``（对齐 k8s 路由风格）：用户可预期的问题
（站点不存在、地址为空、账号不对）都以 ``ok=False + error`` 返回，不抛 500。
密码只在保存时接收，**任何读取接口都不回传密码**。
"""
import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from core.errors import UserError
from core.kibana import (
    list_sites as _list_sites,
    add_or_update_site as _add_or_update_site,
    set_current_site as _set_current_site,
    delete_site as _delete_site,
    get_client,
    KibanaError,
)

router = APIRouter()


class SiteSaveReq(BaseModel):
    name: str
    label: str = ""
    base_url: str = ""
    username: str = ""
    password: str = ""          # 空串 = 保持原密码不变
    index_pattern: str = ""
    time_field: str = ""
    msg_field: str = ""
    field_prefix: str = ""
    verify_ssl: bool = False
    timeout: int = 30


class TestReq(BaseModel):
    name: str = ""              # 已保存站点名；为空则测下面这组临时配置
    base_url: str = ""
    username: str = ""
    password: str = ""
    index_pattern: str = ""
    time_field: str = ""
    verify_ssl: bool = False
    timeout: int = 30


def _err(ex: BaseException) -> str:
    return getattr(ex, "message", None) or str(ex)


@router.get("/api/kibana/sites")
async def api_kibana_sites():
    """列出所有 Kibana 站点（不含密码）与当前站点。"""
    try:
        sites = await asyncio.to_thread(_list_sites)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": _err(ex), "sites": [], "current": None}
    cur = next((s["name"] for s in sites if s.get("is_current")),
               sites[0]["name"] if sites else None)
    return {"ok": True, "sites": sites, "current": cur}


@router.post("/api/kibana/sites")
async def api_kibana_site_save(body: SiteSaveReq):
    """新增 / 更新一个站点。"""
    try:
        data = await asyncio.to_thread(
            _add_or_update_site,
            body.name,
            label=body.label,
            base_url=body.base_url,
            username=body.username,
            password=body.password,
            index_pattern=body.index_pattern,
            time_field=body.time_field,
            msg_field=body.msg_field,
            field_prefix=body.field_prefix,
            verify_ssl=body.verify_ssl,
            timeout=body.timeout,
        )
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex)}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": _err(ex)}
    return {"ok": True, "current": data.get("current")}


@router.post("/api/kibana/sites/delete")
async def api_kibana_site_delete(body: dict):
    name = (body or {}).get("name", "")
    try:
        data = await asyncio.to_thread(_delete_site, name)
    except UserError as ex:
        return {"ok": False, "error": _err(ex)}
    return {"ok": True, "current": data.get("current")}


@router.post("/api/kibana/sites/switch")
async def api_kibana_site_switch(body: dict):
    name = (body or {}).get("name", "")
    try:
        data = await asyncio.to_thread(_set_current_site, name)
    except UserError as ex:
        return {"ok": False, "error": _err(ex)}
    return {"ok": True, "current": data.get("current")}


@router.post("/api/kibana/test")
async def api_kibana_test(body: TestReq):
    """连通性测试。

    依次验证：Kibana 可达（``/api/status``）→ 账号可用（Console Proxy 打
    ``_cluster/health``）→ 索引模式能查到数据（一次 ``size=1`` 检索）。
    任一步失败都给出具体到步骤的错误，便于用户自行定位。
    """
    site = {
        "base_url": body.base_url,
        "username": body.username,
        "password": body.password,
        "index_pattern": body.index_pattern or "logstash-*",
        "time_field": body.time_field or "es_time",
        "msg_field": "log",
        "field_prefix": "kubernetes",
        "verify_ssl": body.verify_ssl,
        "timeout": body.timeout,
    }
    if body.name and not body.base_url:
        # 测已保存站点：用库里的配置（含已存密码）
        try:
            from core.kibana.config import get_site
            _, site = get_site(body.name)
        except UserError as ex:
            return {"ok": False, "error": _err(ex)}

    def _run():
        from core.kibana.client import KibanaClient
        cli = KibanaClient(site)
        try:
            result: dict = {"steps": []}

            st = cli.kibana_status()
            if st.get("http_status") in (401, 403):
                return {"ok": False, "error": "Kibana 认证失败（401/403）：账号或密码不正确。",
                        "steps": result["steps"]}
            result["steps"].append({"name": "kibana", "ok": st["http_status"] < 400,
                                    "detail": f'Kibana {st.get("kibana_version") or "?"} · {st.get("overall") or st.get("message") or st["http_status"]}'})
            result["kibana_version"] = st.get("kibana_version", "")

            health = cli.cluster_health()
            result["steps"].append({"name": "es", "ok": True,
                                    "detail": f'集群 {health.get("cluster_name") or "?"} · {health.get("status")} · {health.get("nodes")} 节点'})
            result["cluster"] = health

            dsl = {"size": 1, "track_total_hits": 10000,
                   "sort": [{cli.time_field: {"order": "desc"}}]}
            resp = cli.es_search(dsl)
            total = ((resp.get("hits") or {}).get("total") or {})
            total = total.get("value", 0) if isinstance(total, dict) else total
            result["steps"].append({"name": "index", "ok": bool(total),
                                    "detail": f'索引 {cli.index_pattern} 命中 {total} 条'})
            result["total"] = total
            return {"ok": True, **result}
        except KibanaError as ex:
            return {"ok": False, "error": str(ex)}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        finally:
            cli.close()

    return await asyncio.to_thread(_run)


@router.get("/api/kibana/index-patterns")
async def api_kibana_index_patterns(site: str = ""):
    """列出 Kibana 里已保存的索引模式（用于站点配置页自动补全）。"""
    try:
        cli = get_client(site or None)
        pats = await asyncio.to_thread(cli.index_patterns)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex), "patterns": []}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": _err(ex), "patterns": []}
    return {"ok": True, "patterns": pats}
