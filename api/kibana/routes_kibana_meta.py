# -*- coding: utf-8 -*-
"""Kibana 元数据路由：字段枚举（命名空间/容器/Pod/应用/节点）与 Pod 概览。

性能约定（务必遵守，否则界面会「点一下卡 10 秒」）
------------------------------------------------
Console Proxy 单次请求有约 4s 的固定开销，因此：

1. **合并**：五个维度的枚举合成**一次**聚合请求返回（而不是各发一次）。
2. **缓存**：结果按「站点 + 查询条件」哈希进 ``core.cache``，TTL 见下方常量；
   失败响应**绝不落盘**（否则一次网络抖动会被缓存成永久空白）。
3. **并发**：确需多次请求时用线程池并发（见 logs 路由的上下文查询）。
"""
import asyncio
import hashlib
import json

from fastapi import APIRouter

from core import cache as _cache
from core.errors import UserError
from core.kibana import get_client, KibanaError
from core.kibana import queries as q

router = APIRouter()

# 枚举类（下拉框候选）变化很慢 → TTL 长一些
FIELDS_TTL = 300
# Pod 概览含实时日志量/错误数 → TTL 短一些
PODS_TTL = 60


def _cache_key(prefix: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return f"{prefix}-{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


def _err(ex: BaseException) -> str:
    return getattr(ex, "message", None) or str(ex)


@router.get("/api/kibana/fields")
async def api_kibana_fields(
    site: str = "",
    start: str = "",
    end: str = "",
    namespace: str = "",
    container: str = "",
    app: str = "",
    host: str = "",
    refresh: bool = False,
):
    """枚举各维度候选值（一次聚合拿到全部）。

    返回 ``{namespaces, containers, pods, apps, hosts}``，每项为
    ``[{key, count}]`` 按文档数倒序。
    """
    try:
        cli = get_client(site or None)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex)}

    params = dict(start=start, end=end, namespace=namespace,
                  container=container, app=app, host=host)
    ns = f"kibana-meta-{site or 'current'}"
    key = _cache_key("fields", params)
    if not refresh:
        hit = _cache.get(ns, key, FIELDS_TTL)
        if hit is not None:
            return {"ok": True, "cached": True, **hit}

    def _run():
        p = cli.field_prefix
        filters = q.build_filters(
            time_field=cli.time_field, msg_field=cli.msg_field, field_prefix=p,
            start=start, end=end, namespace=namespace, container=container,
            app=app, host=host,
        )
        def kf(name: str) -> str:
            return f"{p}.{name}.keyword" if p else f"{name}.keyword"
        aggs = {
            "namespaces": {"terms": {"field": kf("namespace_name"), "size": 50}},
            "containers": {"terms": {"field": kf("container_name"), "size": 100}},
            "pods": {"terms": {"field": kf("pod_name"), "size": 300}},
            "apps": {"terms": {"field": kf("labels.app"), "size": 100}},
            "hosts": {"terms": {"field": kf("host"), "size": 50}},
        }
        resp = cli.es_search(q.build_terms_dsl(filters, aggs))
        return {
            "namespaces": q.parse_buckets(resp, "namespaces"),
            "containers": q.parse_buckets(resp, "containers"),
            "pods": q.parse_buckets(resp, "pods"),
            "apps": q.parse_buckets(resp, "apps"),
            "hosts": q.parse_buckets(resp, "hosts"),
        }

    try:
        data = await asyncio.to_thread(_run)
    except KibanaError as ex:
        return {"ok": False, "error": str(ex)}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    _cache.set(ns, key, data)   # 只有成功才落盘
    return {"ok": True, "cached": False, **data}


@router.get("/api/kibana/pods")
async def api_kibana_pods(
    site: str = "",
    start: str = "now-1h",
    end: str = "",
    namespace: str = "",
    container: str = "",
    app: str = "",
    host: str = "",
    keyword: str = "",
    levels: str = "",           # 逗号分隔：ERROR,WARN
    refresh: bool = False,
):
    """Pod 概览：每个 Pod 的日志量、错误数、所属应用/容器/节点、最后写入时间。

    供「容器视角」左侧树使用——对应 k8s 面板里的 Pod 列表 + 重启/异常标记。
    """
    try:
        cli = get_client(site or None)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex)}

    params = dict(start=start, end=end, namespace=namespace, container=container,
                  app=app, host=host, keyword=keyword, levels=levels)
    ns = f"kibana-meta-{site or 'current'}"
    key = _cache_key("pods", params)
    if not refresh:
        hit = _cache.get(ns, key, PODS_TTL)
        if hit is not None:
            return {"ok": True, "cached": True, **hit}

    def _run():
        filters = q.build_filters(
            time_field=cli.time_field, msg_field=cli.msg_field,
            field_prefix=cli.field_prefix, start=start, end=end,
            namespace=namespace, container=container, app=app, host=host,
            keyword=keyword,
            levels=[x for x in (levels or "").split(",") if x.strip()],
        )
        dsl = q.build_pods_overview_dsl(
            filters, field_prefix=cli.field_prefix,
            msg_field=cli.msg_field, time_field=cli.time_field)
        resp = cli.es_search(dsl)
        # 聚合 DSL 关掉了 track_total_hits，hits.total 恒为 0；
        # 这里用各 Pod 桶的 doc_count 求和作为「命中总量」（语义等价且零额外开销）。
        rows = q.parse_pods_overview(resp)
        return {"pods": rows,
                "total": sum(r.get("count", 0) for r in rows)}

    try:
        data = await asyncio.to_thread(_run)
    except KibanaError as ex:
        return {"ok": False, "error": str(ex)}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    _cache.set(ns, key, data)
    return {"ok": True, "cached": False, **data}


@router.post("/api/kibana/cache/clear")
async def api_kibana_cache_clear(body: dict = None):
    """清空 Kibana 元数据缓存（站点切换或「强制刷新」时用）。"""
    site = (body or {}).get("site", "") or "current"
    removed = _cache.invalidate(f"kibana-meta-{site}")
    return {"ok": True, "removed": removed}
