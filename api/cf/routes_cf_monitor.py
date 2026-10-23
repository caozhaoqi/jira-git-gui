# -*- coding: utf-8 -*-
"""云函数运行状态监控 —— 路由层（薄 HTTP 适配）。

业务逻辑在 ``api.cf.cf_monitor.collect``（与 tools/cf_monitor.py 共用同一套聚合规则）。

端点：
  GET  /api/cf/monitor/servers   可监控的环境列表（来自 cf_accounts + 已缓存 token）
  GET  /api/cf/monitor/models    可选的记录模型清单（前端「日志模型」下拉）
  POST /api/cf/monitor           抓取一轮并聚合（UI 轮询调用；支持时间窗口/只看错误）
  GET  /api/cf/monitor/history   本地快照历史（命令行写入的 snapshots.jsonl，供趋势查看）
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.common import logger, get_cf_accounts
from api.cf.cf_monitor import (
    collect, DEFAULT_PAGE_SIZE, DEFAULT_PAGES,
    RECORD_MODELS, _MODEL_TYPE_FIELDS, _empty_time_range,
)
from api.cf.cf_tokens import _CF_TOKEN_CACHE, TOKEN_CACHE_LOCK
from core.app_paths import get_data_root

router = APIRouter()

# 记录模型的中文说明（前端下拉展示用；英文/日文走 i18n，这里只给「自动」之外的兜底描述）
_MODEL_LABELS = {
    "dynamic_log": "云函数运行日志（字段最语义化，优先）",
    "SyncOuterRecord": "同步日志（云函数/同步任务共用的记录表）",
    "SysLogRecord": "系统日志（平台自身日志，非云函数）",
}


class CfMonitorReq(BaseModel):
    """监控抓取参数。"""
    server_url: str = ""
    token: str = ""
    proxy: str = ""
    record_model: str = "auto"   # auto | dynamic_log | SyncOuterRecord | SysLogRecord
    log_type: str = ""           # 只看某个云函数
    page_size: int = DEFAULT_PAGE_SIZE
    pages: int = DEFAULT_PAGES
    # 时间窗口（空 = 不限）。取回之后按行时间过滤，详见 api.cf.cf_monitor 模块 docstring。
    time_start: str = ""
    time_end: str = ""
    only_errors: bool = False


def _plausible_url(url: str) -> bool:
    """粗筛明显不可用的 server_url，避免把测试残留项排到可选列表前面。

    实测 cf_accounts / token 缓存里会留 ``http://x``、``http://test-cf-401.invalid``
    这类占位垃圾（自动化测试写入的）。它们不该出现在监控下拉框里，更不该被
    自动选中 —— 否则用户一进面板就是「无法连接 node name provided」的错误页。
    """
    u = (url or "").strip()
    if not u:
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(u)
    except Exception:  # noqa: BLE001
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname
    if host in ("x", "localhost", "127.0.0.1"):
        # 本地地址不是「云函数环境」；单个字母主机名一定是占位
        return False
    if host.endswith(".invalid") or host.endswith(".test") or host.endswith(".example"):
        return False
    # 正常域名含点；局域网 IP 也含点。只排除明显无意义的一级主机名
    return "." in host


def _servers() -> "list[dict]":
    """列出可监控环境：cf_accounts 里的账号 + 其 token 可用状态。

    排序刻意做成「最可能直接能用」优先：
      1. 有 token 且没有登录错误
      2. 有 token 但上次有错
      3. 无 token
    这样前端首次自动选中时不会落到一个注定失败的环境。
    """
    with TOKEN_CACHE_LOCK:
        cached = dict(_CF_TOKEN_CACHE)

    out = []
    seen = set()
    for acc in get_cf_accounts() or []:
        url = (acc.get("server_url") or "").strip().rstrip("/")
        if not url or url in seen or not _plausible_url(url):
            continue
        seen.add(url)
        entry = cached.get(url) or {}
        token_ok = bool((entry.get("token") or "").strip() or (entry.get("cookie") or "").strip())
        last_error = (entry.get("last_error") or "").strip()
        out.append({
            "name": (acc.get("name") or url).strip(),
            "server_url": url,
            "domain": (acc.get("domain") or "").strip(),
            "http_proxy": (acc.get("http_proxy") or "").strip(),
            "has_credentials": bool(acc.get("password") or entry.get("cookie")),
            # token 是否已缓存（用于给下拉框标注「需先登录」）
            "has_token": token_ok,
            "last_error": last_error,
            "token_ts": (entry.get("ts") or "").strip(),
        })
    # 已缓存 token 但不在 cf_accounts 的环境也列出来（用户可能手工登录过）
    for url, entry in cached.items():
        url = (url or "").strip().rstrip("/")
        if not url or url in seen or not isinstance(entry, dict):
            continue
        if not _plausible_url(url):
            continue
        token_ok = bool((entry.get("token") or "").strip() or (entry.get("cookie") or "").strip())
        if not token_ok:
            continue
        seen.add(url)
        out.append({
            "name": (entry.get("name") or url).strip(),
            "server_url": url,
            "domain": "",
            "http_proxy": "",
            "has_credentials": True,
            "has_token": True,
            "last_error": (entry.get("last_error") or "").strip(),
            "token_ts": (entry.get("ts") or "").strip(),
        })
    # 排序键：有token且无错 < 有token但有错 < 无token；同档按名称
    out.sort(key=lambda s: (
        0 if (s["has_token"] and not s["last_error"]) else
        1 if s["has_token"] else 2,
        s["name"],
    ))
    return out


@router.get("/api/cf/monitor/servers")
async def api_cf_monitor_servers():
    """可监控的环境列表（含 token 可用状态，不含明文）。"""
    try:
        return {"ok": True, "servers": _servers()}
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[CF-MONITOR] 读取环境列表失败: {e}")
        return {"ok": False, "servers": [], "error": str(e)}


@router.get("/api/cf/monitor/models")
async def api_cf_monitor_models():
    """可用的记录模型清单（前端「日志模型」下拉的数据源）。

    自动探测顺序即 ``RECORD_MODELS``：前端下拉第一项是「自动」，后面按这个顺序列。
    同时回传各模型用于「只看某个云函数」的字段名 —— 该字段随模型变化
    （dynamic_log→log_type，其余→name），前端可据此给出提示。
    """
    return {
        "ok": True,
        "models": [
            {
                "name": m,
                "label": _MODEL_LABELS.get(m, m),
                "type_field": _MODEL_TYPE_FIELDS.get(m.lower(), "log_type"),
            }
            for m in RECORD_MODELS
        ],
    }


@router.post("/api/cf/monitor")
async def api_cf_monitor(req: CfMonitorReq):
    """抓取一轮监控数据并按云函数聚合。

    约定（与项目其它代理接口一致）：业务失败也返回 HTTP 200 + ``ok:false``，
    由前端展示 ``error``；只有参数非法才用 400。这样前端不需要区分
    「网络错误」与「平台返回 error」两套分支。
    """
    try:
        return await collect(
            server_url=req.server_url,
            token=req.token,
            proxy=req.proxy,
            record_model=req.record_model,
            log_type=req.log_type,
            page_size=req.page_size,
            pages=req.pages,
            time_start=req.time_start,
            time_end=req.time_end,
            only_errors=req.only_errors,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[CF-MONITOR] 聚合失败: {e}")
        return {
            "ok": False,
            "server_url": req.server_url,
            "functions": [],
            "time_range": _empty_time_range(),
            "error": f"{type(e).__name__}: {e}",
        }


def _snapshot_file() -> Path:
    """快照文件位置：优先应用数据目录，其次项目 logs/（命令行早期写入的位置）。"""
    for base in (get_data_root(), Path(__file__).resolve().parent.parent.parent):
        p = base / "logs" / "cf_monitor" / "snapshots.jsonl"
        if p.exists():
            return p
    return get_data_root() / "logs" / "cf_monitor" / "snapshots.jsonl"


@router.get("/api/cf/monitor/history")
async def api_cf_monitor_history(limit: int = 50):
    """读取本地快照历史（命令行监控器每轮写一行，用于看趋势）。"""
    limit = max(1, min(int(limit or 50), 500))
    path = _snapshot_file()
    if not path.exists():
        return {"ok": True, "path": str(path), "entries": [], "total": 0}

    entries = []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            entries.append({
                "ts": d.get("ts", ""),
                "server_url": d.get("server_url", ""),
                "summary": d.get("summary") or {},
            })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CF-MONITOR] 读取快照失败: {e}")
        return {"ok": False, "path": str(path), "entries": [], "total": 0, "error": str(e)}

    return {
        "ok": True,
        "path": str(path),
        "entries": entries,
        "total": len(lines),
    }
