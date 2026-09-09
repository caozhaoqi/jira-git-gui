# -*- coding: utf-8 -*-
"""Kibana 日志检索路由：主查询 / 上下文 / 直方图 / 导出。

三个端点共用同一套过滤条件（见 :func:`_filters_from`），保证「看到的列表」
与「导出的文件」「直方图」永远是同一份数据切片。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from pydantic import BaseModel

from core.errors import UserError
from core.kibana import get_client, KibanaError
from core.kibana import queries as q

router = APIRouter()

# 上下文查询要发两次请求（上文 + 下文），Console Proxy 单次约 4s，
# 串行就是 8s —— 并发掉，总耗时按单次算。
_CTX_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kibana-ctx")


class LogQuery(BaseModel):
    site: str = ""
    start: str = "now-1h"
    end: str = ""
    namespace: str = ""
    container: str = ""
    pod: str = ""
    pods: list = []              # 多选（树里勾了多个 Pod）
    app: str = ""
    host: str = ""
    keyword: str = ""
    exclude_keyword: str = ""
    levels: list = []            # ["ERROR","WARN"]
    size: int = 200
    from_: int = 0
    order: str = "desc"          # desc=最新在前；asc=最旧在前（跟随模式）


class ContextQuery(BaseModel):
    site: str = ""
    anchor: str = ""             # 锚点日志的 es_time（UTC ISO）
    pod: str = ""
    container: str = ""
    namespace: str = ""
    before: int = 50
    after: int = 50


def _err(ex: BaseException) -> str:
    return getattr(ex, "message", None) or str(ex)


def _filters_from(cli, body: LogQuery, **extra) -> list:
    return q.build_filters(
        time_field=cli.time_field, msg_field=cli.msg_field,
        field_prefix=cli.field_prefix,
        start=body.start, end=body.end,
        namespace=body.namespace, container=body.container,
        pod=body.pod, pods=body.pods, app=body.app, host=body.host,
        keyword=body.keyword, exclude_keyword=body.exclude_keyword,
        levels=body.levels, **extra)


@router.post("/api/kibana/logs")
async def api_kibana_logs(body: LogQuery):
    """日志主查询（分页）。"""
    try:
        cli = get_client(body.site or None)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex), "rows": []}

    def _run():
        filters = _filters_from(cli, body)
        dsl = q.build_search_dsl(
            filters, time_field=cli.time_field, size=body.size,
            from_=body.from_, order=body.order)
        resp = cli.es_search(dsl)
        return {
            "rows": q.parse_hits(resp, field_prefix=cli.field_prefix,
                                 msg_field=cli.msg_field,
                                 time_field=cli.time_field),
            "total": q.total_hits(resp),
            "took_ms": resp.get("took", 0),
        }

    try:
        data = await asyncio.to_thread(_run)
    except KibanaError as ex:
        return {"ok": False, "error": str(ex), "rows": []}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}", "rows": []}
    return {"ok": True, **data}


@router.post("/api/kibana/context")
async def api_kibana_context(body: ContextQuery):
    """取某条日志的上下文（前 N 行 + 后 N 行）。

    ``anchor`` 是那行日志的 ``es_time``（UTC）。两次查询并发执行，
    返回按时间正序拼接的完整片段，并把锚点行标出来供前端高亮。
    """
    if not body.anchor:
        return {"ok": False, "error": "缺少锚点时间（anchor）", "rows": []}
    try:
        cli = get_client(body.site or None)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex), "rows": []}

    filters = q.build_filters(
        time_field=cli.time_field, msg_field=cli.msg_field,
        field_prefix=cli.field_prefix,
        pod=body.pod, container=body.container, namespace=body.namespace,
    )

    def _one(direction: str, size: int):
        if size <= 0:
            return []
        dsl = q.build_context_dsl(filters, time_field=cli.time_field,
                                  anchor=body.anchor, size=size,
                                  direction=direction)
        resp = cli.es_search(dsl)
        return q.parse_hits(resp, field_prefix=cli.field_prefix,
                            msg_field=cli.msg_field, time_field=cli.time_field)

    def _run():
        # 两个方向都先 submit 再取结果 —— 请求是并发发出的，总耗时按单次算
        futs = {
            "before": _CTX_POOL.submit(_one, "before", int(body.before or 0)),
            "after": _CTX_POOL.submit(_one, "after", int(body.after or 0)),
        }
        out = {"before": [], "after": []}
        for key, fut in futs.items():
            try:
                out[key] = fut.result(timeout=cli.timeout + 10)
            except Exception:  # noqa: BLE001
                out[key] = []
        return out

    try:
        parts = await asyncio.to_thread(_run)
    except KibanaError as ex:
        return {"ok": False, "error": str(ex), "rows": []}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}", "rows": []}

    before = list(reversed(parts.get("before") or []))   # 上文按时间正序
    after = parts.get("after") or []
    rows = before + after
    return {
        "ok": True,
        "rows": rows,
        "anchor": body.anchor,
        "anchor_index": len(before),   # 锚点行在 rows 里的位置（已剔除锚点自身）
    }


@router.post("/api/kibana/histogram")
async def api_kibana_histogram(body: LogQuery):
    """时间直方图（总量 + 错误量），供「检索视角」顶部趋势图使用。"""
    try:
        cli = get_client(body.site or None)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex), "buckets": []}

    def _run():
        filters = _filters_from(cli, body)
        dsl = q.build_histogram_dsl(
            filters, time_field=cli.time_field, msg_field=cli.msg_field,
            start=body.start, end=body.end)
        resp = cli.es_search(dsl)
        # 聚合 DSL 关掉了 track_total_hits，hits.total 恒为 0，改用桶求和
        buckets = q.parse_histogram(resp)
        return {
            "buckets": buckets,
            "interval": q.pick_histogram_interval(body.start, body.end),
            "total": sum(b.get("count", 0) for b in buckets),
        }

    try:
        data = await asyncio.to_thread(_run)
    except KibanaError as ex:
        return {"ok": False, "error": str(ex), "buckets": []}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}", "buckets": []}
    return {"ok": True, **data}


@router.post("/api/kibana/export")
async def api_kibana_export(body: LogQuery):
    """导出当前查询结果为纯文本（前端拿到 content 后走 Blob 下载）。

    上限 20000 行：Console Proxy 单次 4s 且响应体直接进内存，无限导出会把
    后端顶死；真要全量请用 Kibana 自己的 CSV 导出。
    """
    try:
        cli = get_client(body.site or None)
    except (UserError, KibanaError) as ex:
        return {"ok": False, "error": _err(ex), "content": ""}

    limit = max(1, min(int(body.size or 1000), 20000))

    def _run():
        filters = _filters_from(cli, body)
        dsl = q.build_search_dsl(filters, time_field=cli.time_field,
                                 size=limit, from_=0, order="asc")
        resp = cli.es_search(dsl)
        rows = q.parse_hits(resp, field_prefix=cli.field_prefix,
                            msg_field=cli.msg_field, time_field=cli.time_field)
        lines = []
        for r in rows:
            head = " ".join(x for x in (r.get("ts") or "", r.get("level") or "",
                                        r.get("container") or "", r.get("pod") or "")
                            if x)
            lines.append(f"[{head}] {r.get('msg') or ''}")
        return {"content": "\n".join(lines), "count": len(rows)}

    try:
        data = await asyncio.to_thread(_run)
    except KibanaError as ex:
        return {"ok": False, "error": str(ex), "content": ""}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}", "content": ""}

    import time as _time
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    return {"ok": True, "filename": f"kibana-logs-{stamp}.log", **data}
