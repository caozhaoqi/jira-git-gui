# -*- coding: utf-8 -*-
"""云函数日志实时刷新：后端按间隔轮询 HCM 第一页（最新），新日志经 SSE 推送。

HCM ``hcm.model.list`` 是纯请求-响应接口，没有服务端流式通道。这里在后端起一个
**可取消的 asyncio 任务**，按 ``interval`` 秒轮询 page 1（最新），用行 key（id 优先，
时间+内容哈希兜底）去重，仅把**新增**的日志通过事件总线
``broadcast("cf_log_update", ...)`` 推给前端 —— 前端复用既有 ``/api/events`` SSE
通道（与 ``cf_token_update`` 等事件同源），体验等价于 WebSocket 推送。

生命周期：``POST /api/cf/logs/stream``（action = start / stop / status）。
- 全局同时只允许一个流：start 会先取消旧流；
- 首轮轮询只做「静默种子」（记录已见行、不推送），避免把整页旧日志当新日志刷屏；
- token 失效（PermissionError）时先清空 token 改走缓存/自动重登再试，仍失败才停流；
- 连续 5 次其它错误也停流，避免无意义空转。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

from api.common import logger
from api.eventbus import broadcast
from api.cf.cf_logs import cf_query_logs

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_cfg: dict = {}  # 当前流参数（含 interval / streaming，供 status 查询与前端展示）

_SEEN_MAX = 8000   # 去重集合上限（超出丢弃最旧一半）
_SEEN_KEEP = 4000
_MAX_ERRORS = 5    # 连续失败多少次后停流


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _meta(cfg: dict) -> dict:
    """广播里带上的流标识字段，便于前端确认是哪条流。"""
    return {
        "server_url": cfg.get("server_url", ""),
        "log_type": cfg.get("log_type", ""),
        "record_model": cfg.get("record_model", ""),
    }


def _row_key(row: Any) -> str:
    """行去重 key：id 优先，缺 id 时用「时间 + 内容」哈希兜底（与前端 cfRowKey 同口径）。"""
    if not isinstance(row, dict):
        return "h:" + hashlib.md5(str(row).encode("utf-8")).hexdigest()
    rid = row.get("id")
    if rid is None:
        rid = row.get("_id")
    if rid is not None:
        return "id:" + str(rid)
    basis = json.dumps(
        [row.get("create_time"), row.get("update_time"), row.get("content"),
         row.get("log_type"), row.get("name")],
        ensure_ascii=False, default=str,
    )
    return "h:" + hashlib.md5(basis.encode("utf-8")).hexdigest()


def _row_time(row: dict) -> str:
    """行时间（与前端 logRowTime 同口径：create_* 优先，update_* 兜底）。"""
    for k in ("create_time", "createTime", "created_at", "create_date",
              "update_time", "updateTime", "updated_at"):
        v = row.get(k)
        if v not in (None, "", 0):
            return str(v)
    return ""


def _extract_rows(res: Any) -> tuple:
    """从 cf_query_logs 返回值提取 (rows, total)。

    返回形如 ``{result: {method, raw, data}, error, is_session}``，其中 ``data``
    可能是 list（HCM result 直出数组）或 ``{list: [...], count: N}``。
    """
    if not isinstance(res, dict):
        return [], 0
    payload = res.get("data") or res.get("result") or res
    if isinstance(payload, list):
        return payload, len(payload)
    if not isinstance(payload, dict):
        return [], 0
    inner = payload.get("data")
    if isinstance(inner, list):
        return inner, len(inner)
    if isinstance(inner, dict):
        rows = inner.get("list") or []
        return rows, int(inner.get("count") or len(rows))
    rows = payload.get("list")
    if isinstance(rows, list):
        return rows, int(payload.get("count") or len(rows))
    return [], 0


async def _stream_loop(cfg: dict, interval: int) -> None:
    global _cfg
    seen: dict[str, None] = {}  # 有序去重：插入序≈时间序，超限时从最旧开始丢
    errs = 0
    first = True
    try:
        while True:
            try:
                req = SimpleNamespace(**cfg, page_index=1)
                res = await cf_query_logs(req)
                rows, total = _extract_rows(res)
                errs = 0
                if first:
                    for r in rows:
                        seen[_row_key(r)] = None
                    first = False
                    # 首轮种子：只记录基线，不推送（前端开启流之前已手动查过一次）
                    broadcast("cf_log_update", {
                        "ok": True, "seeded": True, "rows": [], "new_count": 0,
                        "total": total, "ts": _now(), **_meta(cfg),
                    })
                else:
                    fresh = []
                    for r in rows:
                        k = _row_key(r)
                        if k not in seen:
                            seen[k] = None
                            fresh.append(r)
                    if len(seen) > _SEEN_MAX:
                        for k in list(seen)[:len(seen) - _SEEN_KEEP]:
                            seen.pop(k, None)
                    latest = _row_time(fresh[0]) if fresh else (_row_time(rows[0]) if rows else "")
                    broadcast("cf_log_update", {
                        "ok": True, "rows": fresh, "new_count": len(fresh),
                        "total": total, "latest_time": latest, "ts": _now(),
                        **_meta(cfg),
                    })
            except asyncio.CancelledError:
                raise
            except PermissionError as e:
                # token 失效：先清空 token 改走缓存/自动重登再试；已是缓存路径仍失败 → 停流
                if cfg.get("token"):
                    logger.warning("[CF-STREAM] 手动 token 失效，改走缓存/自动重登重试")
                    cfg = {**cfg, "token": ""}
                    broadcast("cf_log_update", {
                        "ok": False, "error": f"{e}（已改用缓存 token / 自动重登重试）",
                        "ts": _now(), **_meta(cfg),
                    })
                else:
                    broadcast("cf_log_update", {
                        "ok": False, "stopped": True, "error": str(e),
                        "ts": _now(), **_meta(cfg),
                    })
                    _cfg = {**_cfg, "streaming": False}
                    return
            except Exception as e:  # noqa: BLE001
                errs += 1
                logger.warning(f"[CF-STREAM] 拉取失败({errs}/{_MAX_ERRORS}): {e}")
                broadcast("cf_log_update", {
                    "ok": False, "error": str(e), "ts": _now(), **_meta(cfg),
                })
                if errs >= _MAX_ERRORS:
                    broadcast("cf_log_update", {
                        "ok": False, "stopped": True,
                        "error": f"连续 {errs} 次拉取失败，实时刷新已停止",
                        "ts": _now(), **_meta(cfg),
                    })
                    _cfg = {**_cfg, "streaming": False}
                    return
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise


async def start_stream(server_url: str, token: str = "", proxy: str = "",
                       log_type: str = "", record_model: str = "dynamic_log",
                       page_size: int = 100, interval: int = 5) -> dict:
    """启动（或重启）日志实时流。全局同时只有一个流。"""
    global _task, _cfg
    async with _lock:
        if _task and not _task.done():
            _task.cancel()
            try:
                await _task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        cfg = {
            "server_url": (server_url or "").strip().rstrip("/"),
            "token": (token or "").strip(),
            "proxy": (proxy or "").strip(),
            "log_type": (log_type or "").strip(),
            "record_model": (record_model or "dynamic_log").strip() or "dynamic_log",
            "page_size": max(10, min(int(page_size or 100), 500)),
        }
        interval = max(3, min(int(interval or 5), 120))
        _cfg = {**cfg, "interval": interval, "streaming": True}
        _task = asyncio.create_task(_stream_loop(cfg, interval))
        logger.info(f"[CF-STREAM] 已启动：{cfg['server_url']} model={cfg['record_model']} "
                    f"log_type={cfg['log_type'] or '-'} interval={interval}s")
    return {"ok": True, **_cfg}


async def stop_stream() -> dict:
    """停止当前流（若有），并广播 stopped 事件。"""
    global _task, _cfg
    async with _lock:
        was = bool(_cfg.get("streaming"))
        if _task and not _task.done():
            _task.cancel()
            try:
                await _task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        _task = None
        _cfg = {**_cfg, "streaming": False}
        if was:
            broadcast("cf_log_update", {"ok": True, "stopped": True, "ts": _now(), **_meta(_cfg)})
        logger.info("[CF-STREAM] 已停止")
    return {"ok": True, "streaming": False}


def stream_status() -> dict:
    """当前流状态（参数不含 token 明文）。"""
    return {"ok": True, **{k: v for k, v in _cfg.items() if k != "token"}}
