#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云函数运行状态本地监控器。

复用项目已有的 CF token 缓存 / 刷新 / SSL 配置（api.cf.cf_tokens / api.cf.cf_login），
周期性拉取 HCM ``dynamic_log`` 记录模型，解析每条日志为结构化字段
（复用 api.cf.cf_diagnose.parse_cf_log_content），按**云函数名**聚合成一份运行状态看板：

    ┌ 云函数名 ─────────┬ 总数 ─┬ 错误 ─┬ 警告 ─┬ 最近一条 ─┬ 健康度 ┐
    │ SyncGdyyEmpPhoto │   128 │    12 │     3 │ 19:07:26  │  ERROR │

设计目标
--------
1. **零依赖**：只用项目已在用的 httpx，不引入新包。
2. **复用既有链路**：鉴权、SSL 兼容（旧式重协商）、token 自动刷新全部走项目现成代码，
   避免「脚本里再写一遍登录逻辑」导致两边行为漂移。
3. **可离线自检**：``--dry-run`` 用内置样例日志跑通全流程，不发任何网络请求。
4. **本地留痕**：每轮快照落 JSONL（默认 ``logs/cf_monitor/``），便于事后回看趋势。

云函数名从哪来
--------------
``dynamic_log`` 的行有两个形态的「函数名」字段：
- ``log_type``：动态日志模型带（形如 ``SyncGdyyEmpPhoto``）
- ``name``     ：``SyncOuterRecord`` 这类模型没有 log_type，用 name 承载描述语义

两者取其一作为归属的云函数名（与 cf_logs._model_type_fallbacks 同口径）。

用法示例
--------
    # 监控某个环境（-s 传 URL 或 cf_accounts 里的名字），每 30 秒一轮
    python tools/cf_monitor.py -s https://21qor.hcmcloud.cn --interval 30

    # 用账号名（自动解析 URL），只看最近 200 条
    python tools/cf_monitor.py -s "21qor-公有云 黑龙江农信社" --page-size 200

    # 跑 3 轮就退出，并把结果导出 JSON
    python tools/cf_monitor.py -s https://21qor.hcmcloud.cn --rounds 3 --json out.json

    # 只看报错的云函数（退出码非 0，便于接 CI / 巡检脚本）
    python tools/cf_monitor.py -s https://21qor.hcmcloud.cn --only-errors --once

    # 离线自检（不联网）
    python tools/cf_monitor.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# 让脚本可直接 `python tools/cf_monitor.py` 运行：把项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from api.cf.cf_tokens import (  # noqa: E402
    TOKEN_CACHE_LOCK,
    _CF_TOKEN_CACHE,
    _cf_ssl_context,
    _HCM_MODEL_LIST_API,
    _HCM_HCMINNER_HEADER,
    _HCM_HCMINNER_VALUE,
)
from api.cf.cf_login import cf_refresh_token  # noqa: E402
from api.cf.cf_diagnose import parse_cf_log_content  # noqa: E402


# ─────────────────────────────── 常量 ───────────────────────────────
# 记录模型与「云函数名字段」的对应关系。
#
# ⚠️ 实测：并非所有环境都有 dynamic_log 模型。
#   - dynamic_log      : 动态日志模型，函数名在 ``log_type``
#   - SyncOuterRecord  : 同步外层记录，**没有 log_type**，函数名在 ``name``
#   - SysLogRecord     : 系统运行日志，函数名在 ``name``，字段是 dimension/category/status
# 同一个租户里 dynamic_log 可能不存在（平台回 errcode 80001「您请求的数据不存在」），
# 此时须退到 SyncOuterRecord 才能拿到数据 —— 故默认按顺序探测。
RECORD_MODELS = ("dynamic_log", "SyncOuterRecord", "SysLogRecord")
DEFAULT_RECORD_MODEL = RECORD_MODELS[0]

# 各模型用于「只看某个云函数」过滤的字段名（与 api.cf.cf_logs._MODEL_TYPE_FIELDS 同口径）
_MODEL_TYPE_FIELDS = {
    "dynamic_log": "log_type",
    "syncouterrecord": "name",
    "syslogrecord": "name",
}

# 云函数名候选字段（按优先级）：dynamic_log 用 log_type；其余模型用 name
_NAME_FIELDS = ("log_type", "name", "cf_name", "func_name", "script_name")

# 健康度判定
HEALTH_OK = "OK"
HEALTH_WARN = "WARN"
HEALTH_ERROR = "ERROR"
HEALTH_UNKNOWN = "UNKNOWN"

# 告警阈值
ERROR_RATIO_THRESHOLD = 0.10   # 错误占比 > 10% → WARN
ERROR_COUNT_THRESHOLD = 5      # 错误数 ≥ 5 → ERROR（绝对量优先于占比，避免小样本误报）

# 历史快照落盘目录（相对项目根）
SNAPSHOT_DIRNAME = "logs/cf_monitor"

# 终端配色（无色终端自动降级）
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{color}m{text}\033[0m"


_C_RESET = "0"
_C_DIM = "2"
_C_RED = "31"
_C_GREEN = "32"
_C_YELLOW = "33"
_C_BLUE = "36"
_C_BOLD = "1"


# ─────────────────────────────── 工具 ───────────────────────────────
def _resolve_server(server: str) -> str:
    """server 可以是完整 URL，也可以是 cf_accounts 里的 name（自动解析出 URL）。"""
    if urlparse(server).scheme:
        return server.strip().rstrip("/")
    try:
        from core.config.cf import load_cf_accounts
        for acc in (load_cf_accounts() or []):
            name = (acc.get("name") or "")
            key = "cf:" + (acc.get("server_url") or "").strip().rstrip("/")
            if server in (name, key, acc.get("server_url")):
                return (acc.get("server_url") or "").strip().rstrip("/")
    except Exception:
        pass
    raise SystemExit(f"无法解析服务器 '{server}'，请直接传完整 URL（https://...）")


def _account_for(server_url: str) -> "dict":
    """按 server_url 找账号（拿 http_proxy / 登录凭据）。"""
    try:
        from core.config.cf import load_cf_accounts
        for acc in (load_cf_accounts() or []):
            if (acc.get("server_url") or "").strip().rstrip("/") == server_url.rstrip("/"):
                return acc
    except Exception:
        pass
    return {}


def _load_token(server_url: str, proxy: str):
    """从缓存取 token/cookie；没有或为空则现刷一次。"""
    with TOKEN_CACHE_LOCK:
        cached = _CF_TOKEN_CACHE.get(server_url.rstrip("/"))
    cached = cached if isinstance(cached, dict) else {}
    token = (cached.get("token") or "").strip()
    cookie = (cached.get("cookie") or "").strip()
    if not (token or cookie):
        fresh = asyncio.run(cf_refresh_token(server_url, proxy))
        if isinstance(fresh, dict):
            token = (fresh.get("token") or "").strip()
            cookie = (fresh.get("cookie") or "").strip()
    cookie = cookie or (f"token={token}" if token else "")
    return token, cookie


def _build_attempts(token: str, cookie: str):
    base = {"Content-Type": "application/json"}
    return [
        {"name": "cookie", "headers": {**base, "Cookie": cookie}},
        {"name": "bearer_hcminner",
         "headers": {**base, "Authorization": f"Bearer {token}",
                     _HCM_HCMINNER_HEADER: _HCM_HCMINNER_VALUE}},
        {"name": "header_token", "headers": {**base, "token": token}},
    ]


def _is_session_err(status: int, errcode: Any, errmsg: str) -> bool:
    """会话类失败判定（与 api.common._cf_is_session_err 同口径）。"""
    msg = str(errmsg or "")
    return (
        status in (401, 403)
        or errcode in (17003, 51006)
        or any(k in msg for k in ("未登录", "登录过期", "未授权", "unauthorized", "请先登录"))
    )


def _extract_rows(result: Any) -> "tuple":
    """从 HCM 响应里取出 (rows, total)。

    ``result`` 可能是 list（直出数组）或 ``{list: [...], count: N}``，
    也可能再嵌一层 ``{data: ...}``（与 cf_stream._extract_rows 同口径）。
    """
    payload = result
    if isinstance(payload, dict):
        payload = payload.get("data", payload)
    if isinstance(payload, list):
        return payload, len(payload)
    if not isinstance(payload, dict):
        return [], 0
    rows = payload.get("list")
    if isinstance(rows, list):
        return rows, int(payload.get("count") or len(rows))
    return [], 0


def _is_model_missing(errcode: Any, errmsg: str) -> bool:
    """判定「模型在当前租户不存在」。

    平台回 errcode 80001「您请求的数据不存在」/「Unknown Model Name」——
    这不是鉴权问题，换模型即可，不该触发重登。
    """
    if errcode == 80001:
        return True
    return "Unknown Model Name" in str(errmsg or "")


# ─────────────────────────────── 取数 ───────────────────────────────
async def _fetch_once(server_url: str, proxy: str, token: str, cookie: str, payload: dict):
    """三种鉴权依次尝试；返回 (rows, total, need_refresh, err, model_missing)。"""
    attempts = _build_attempts(token, cookie)
    last_err = "未知错误"
    for att in attempts:
        try:
            kw = dict(timeout=30, follow_redirects=True, verify=_cf_ssl_context())
            if proxy:
                kw["proxy"] = proxy
            else:
                # 显式 transport 会忽略顶层 verify，需在 transport 也传 ssl 上下文
                kw["transport"] = httpx.AsyncHTTPTransport(verify=_cf_ssl_context())
            async with httpx.AsyncClient(**kw) as client:
                resp = await client.post(
                    server_url + _HCM_MODEL_LIST_API, json=payload, headers=att["headers"]
                )
                if resp.status_code >= 400:
                    last_err = f"[{att['name']}] HTTP {resp.status_code}: {resp.text[:300]}"
                    if _is_session_err(resp.status_code, None, resp.text[:300]):
                        return [], 0, True, last_err, False
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    last_err = f"[{att['name']}] 返回非JSON: {resp.text[:300]}"
                    continue
                if isinstance(data, dict) and data.get("errcode") not in (None, 0):
                    msg = data.get("errmsg") or data.get("description") or str(data)[:300]
                    last_err = f"[{att['name']}] 业务失败: {msg}"
                    if _is_model_missing(data.get("errcode"), msg):
                        return [], 0, False, last_err, True
                    if _is_session_err(0, data.get("errcode"), msg):
                        return [], 0, True, last_err, False
                    continue
                result = data.get("result", data) if isinstance(data, dict) else data
                rows, total = _extract_rows(result)
                return rows, total, False, "", False
        except Exception as e:  # noqa: BLE001
            last_err = f"[{att['name']}] {type(e).__name__}: {e}"
            continue
    return [], 0, False, last_err, False


async def fetch_logs(server_url: str, proxy: str, token: str, cookie: str,
                     payload: dict) -> "tuple":
    """拉一页日志；token 失效时自动刷新一次并重试。返回 (rows, total, err)。"""
    rows, total, need_refresh, err, _missing = await _fetch_once(
        server_url, proxy, token, cookie, payload
    )
    if need_refresh:
        fresh = await cf_refresh_token(server_url, proxy)
        if isinstance(fresh, dict) and (fresh.get("token") or fresh.get("cookie")):
            token = (fresh.get("token") or "").strip()
            cookie = (fresh.get("cookie") or "").strip() or f"token={token}"
            rows, total, _, err2, _m2 = await _fetch_once(
                server_url, proxy, token, cookie, payload
            )
            if rows or total:
                return rows, total, ""
            err = err2 or err
    return rows, total, err


def model_type_field(record_model: str) -> str:
    """该模型用于「只看某个云函数」过滤的字段名。"""
    return _MODEL_TYPE_FIELDS.get((record_model or "").strip().lower(), "log_type")


# ─────────────────────────────── 解析聚合 ───────────────────────────────
def _row_name(row: dict) -> str:
    """取该行归属的云函数名。"""
    for f in _NAME_FIELDS:
        v = row.get(f)
        if v and str(v).strip():
            return str(v).strip()
    return "(未知)"


def _row_time(row: dict) -> str:
    """行时间（create_* 优先，update_* 兜底）。"""
    for k in ("create_time", "createTime", "created_at", "create_date",
              "update_time", "updateTime", "updated_at"):
        v = row.get(k)
        if v not in (None, "", 0):
            return str(v)
    return ""


def _row_message(row: dict, parsed: dict) -> str:
    """取一行的人类可读摘要（用于「最近一条」展示）。

    content 实测有几种嵌套形态：
      - ``{"msg": "..."}``                 —— 最常见
      - ``{"content": "Traceback ..."}``   —— 异常行把 traceback 塞在 content 里
      - ``'{"content": "{\\"content\\": ...}"}'`` —— 双层字符串再包一层
    """
    msg: Any = parsed.get("message") or row.get("content") or ""
    if isinstance(msg, (dict, list)):
        msg = json.dumps(msg, ensure_ascii=False)

    content = row.get("content")
    if isinstance(content, str):
        # 最多解两层嵌套 JSON：content → {"msg"/"content"} → 字符串
        cur: Any = content
        for _ in range(2):
            if not isinstance(cur, str):
                break
            try:
                nxt = json.loads(cur)
            except ValueError:
                break
            if isinstance(nxt, dict):
                inner = nxt.get("msg") or nxt.get("content") or nxt.get("message")
                if inner is None:
                    break
                msg = inner
                cur = inner
            elif isinstance(nxt, str):
                msg = cur = nxt
            else:
                break

    msg = str(msg).replace("\n", " ").replace("\r", " ").strip()
    # 压缩连续空白，避免 traceback 里的大段缩进把表格撑爆
    msg = re.sub(r"\s{2,}", " ", msg)
    return msg[:160]


# 纯文本日志里的 errcode / stage 兜底提取（共享解析器只认字典型 content）。
# 注意：实测平台日志里的错误码有 ``errcode=80002``、``80002：system_errors_msg…``、
# 「错误号:80002」等多种写法，中文全角冒号 ``：`` 也要覆盖，否则错误码统计全是空。
_ERRCODE_RE = re.compile(
    r"errcode[\"']?\s*[:：=]\s*\"?(-?\d{1,6})"
    r"|(?:错误号|错误码|error[_ ]?code)[\"']?\s*[:：=]\s*\"?(-?\d{1,6})"
    r"|(?<![\d.])([5-9]\d{4})(?=\s*[:：])",
    re.I,
)
_STAGE_RE = re.compile(r"\bstage[\"']?\s*[:：=]\s*[\"']?([A-Za-z_][\w.\-]{0,40})", re.I)


def _fallback_errcode(content: Any):
    """从纯文本 content 里兜底抠出 errcode（返回 int 或 None）。

    三类写法都能抓：
      - ``errcode=80002``
      - ``错误号:80002`` / ``错误码：80002``
      - ``降级逐条重试：80002：system_errors_msg…``（裸 5 位码 + 冒号，平台最常见）
    """
    if not isinstance(content, str) or not content:
        return None
    m = _ERRCODE_RE.search(content)
    if not m:
        return None
    raw = next((g for g in m.groups() if g), None)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _fallback_stage(content: Any) -> str:
    """从纯文本 content 里兜底抠出 stage（返回空串表示没有）。"""
    if not isinstance(content, str) or not content:
        return ""
    m = _STAGE_RE.search(content)
    return m.group(1) if m else ""


# ─────────────────────────── 误报防护 ───────────────────────────
# 共享解析器（parse_cf_log_content）用的 _ERROR_HINTS 里有「失败」二字，
# 于是「解析失败 0 / 失败 0 / 失败: 0」这类**统计类成功日志**会被判成错误。
# 实测 SyncHuaqiRaw 的「扇出结束：… 解析失败 0 …」就被误报成 ERROR。
# 监控看板一旦把正常日志标红，整块看板就不可信了 —— 这里做二次判定：
#   1) 若正文里的「失败/错误/异常」**后面紧跟 0**（或「0 条/个/次」），视为统计 0，不算错；
#   2) 若正文没有任何其它错误信号（traceback / errcode / 显式 ERROR 标签），不算错。
_ZERO_COUNT_RE = re.compile(
    r"(?:失败|错误|异常|error[s]?|failed|failure)\s*[:：=]?\s*0(?![0-9])"
    r"|(?:失败|错误|异常)\s*0\s*(?:条|个|次|行|项|处)",
    re.I,
)
_REAL_ERROR_RE = re.compile(
    r"traceback|exception|errcode[\"']?\s*[:=]\s*\"?-?[1-9]"
    r"|\bERROR\b|DIAG_ERR|EXCEPTION|FAILED|❌|✗"
    r"|失败(?!\s*[:：=]?\s*0)|错误号|执行api错误|超时|拒绝|异常(?!\s*[:：=]?\s*0)",
    re.I,
)


def _is_false_positive(raw: Any, parsed: dict) -> bool:
    """共享解析器判 error，但实际只是「失败 0」这类统计日志 → 不算错。"""
    if not parsed.get("is_error"):
        return False
    if not isinstance(raw, str) or not raw:
        return False
    # 有明确错误信号（traceback / errcode≠0 / 显式标签）就尊重原判
    for key in ("ERROR", "DIAG_ERR", "EXCEPTION", "FAILED"):
        if key in (parsed.get("tags") or {}):
            return False
    ec = parsed.get("errcode")
    if isinstance(ec, int) and ec != 0:
        return False
    # 命中「失败 0」类统计写法，且没有其它真实错误信号 → 误报
    if _ZERO_COUNT_RE.search(raw) and not _REAL_ERROR_RE.search(raw):
        return True
    return False


def aggregate(rows: "list") -> "dict":
    """把原始日志行聚合成「按云函数名」的运行状态看板。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = _row_name(row)
        raw = row.get("content")
        parsed = parse_cf_log_content(raw, row.get("log_type", ""))
        is_err = bool(parsed.get("is_error")) and not _is_false_positive(raw, parsed)
        b = buckets.setdefault(name, {
            "name": name,
            "total": 0,
            "error": 0,
            "warn": 0,
            "info": 0,
            "last_time": "",
            "last_message": "",
            "last_error": "",
            "last_error_time": "",
            "errcodes": {},
            "stages": {},
        })
        b["total"] += 1
        level = (parsed.get("level") or "").upper()
        if is_err:
            b["error"] += 1
            level = level or "ERROR"
        elif level in ("WARN", "WARNING"):
            b["warn"] += 1
        elif level in ("INFO", "DEBUG"):
            b["info"] += 1

        ts = _row_time(row)
        if ts and ts >= b["last_time"]:
            b["last_time"] = ts
            b["last_message"] = _row_message(row, parsed)
        # 「最近一条错误」独立记录：不能拿最新一条（可能是成功日志）冒充
        if is_err and ts >= b["last_error_time"]:
            b["last_error_time"] = ts
            b["last_error"] = _row_message(row, parsed)

        ec = parsed.get("errcode")
        if not (isinstance(ec, int) and ec != 0):
            # 兜底：共享解析器只从「字典型 content」里取 errcode，
            # 纯文本里的 "errcode=1002" 会漏掉 —— 监控场景不能漏。
            ec = _fallback_errcode(raw)
        if isinstance(ec, int) and ec != 0:
            b["errcodes"][str(ec)] = b["errcodes"].get(str(ec), 0) + 1
        st = parsed.get("stage") or _fallback_stage(raw)
        if st:
            b["stages"][st] = b["stages"].get(st, 0) + 1

    # 健康度判定：
    #   ERROR —— 错误绝对量够多（真的在坏）
    #   WARN  —— 错误占比高但样本小（刚出问题 / 偶发）
    #   OK    —— 本轮无错误
    out: "Dict[str, Dict[str, Any]]" = {}
    for name, b in buckets.items():
        ratio = (b["error"] / b["total"]) if b["total"] else 0.0
        if b["error"] >= ERROR_COUNT_THRESHOLD:
            health = HEALTH_ERROR
        elif b["error"]:
            health = HEALTH_WARN
        elif b["total"]:
            health = HEALTH_OK
        else:
            health = HEALTH_UNKNOWN
        b["error_ratio"] = round(ratio, 4)
        b["health"] = health
        out[name] = b
    return out


def summarize(buckets: "dict") -> "dict":
    """总览统计。"""
    funcs = list(buckets.values())
    return {
        "functions": len(funcs),
        "total_rows": sum(f["total"] for f in funcs),
        "errors": sum(f["error"] for f in funcs),
        "warnings": sum(f["warn"] for f in funcs),
        "error_functions": sum(1 for f in funcs if f["health"] == HEALTH_ERROR),
        "warn_functions": sum(1 for f in funcs if f["health"] == HEALTH_WARN),
        "ok_functions": sum(1 for f in funcs if f["health"] == HEALTH_OK),
    }


# ─────────────────────────────── 展示 ───────────────────────────────
def _health_color(health: str) -> str:
    return {
        HEALTH_ERROR: _C_RED,
        HEALTH_WARN: _C_YELLOW,
        HEALTH_OK: _C_GREEN,
    }.get(health, _C_DIM)


def _health_badge(health: str) -> str:
    badge = {
        HEALTH_ERROR: "ERROR",
        HEALTH_WARN: "WARN ",
        HEALTH_OK: "OK   ",
        HEALTH_UNKNOWN: "---- ",
    }.get(health, "---- ")
    return _c(badge, _health_color(health))


def render_table(buckets: "dict") -> str:
    """渲染看板表格（等宽对齐）。"""
    if not buckets:
        return _c("(无数据)", _C_DIM)

    lines = []
    header = (
        f"{'':<1} {'云函数':<27} {'总数':>6} {'错误':>6} {'警告':>6} "
        f"{'错误率':>8}  {'健康':<6} {'最近活动':<20} 最近一条"
    )
    lines.append(_c(header, _C_BOLD))
    lines.append(_c("─" * 120, _C_DIM))

    # 排序：先按健康度（ERROR → WARN → OK），再按错误数降序，最后按总数降序
    order = {HEALTH_ERROR: 0, HEALTH_WARN: 1, HEALTH_UNKNOWN: 2, HEALTH_OK: 3}
    items = sorted(
        buckets.values(),
        key=lambda b: (order.get(b["health"], 9), -b["error"], -b["total"]),
    )
    for b in items:
        name = b["name"] if len(b["name"]) <= 27 else b["name"][:26] + "…"
        ratio = f"{b['error_ratio'] * 100:.1f}%"
        last = (b["last_time"] or "-")
        msg = b["last_message"] or "-"
        # 「最近一条」列展示最新一条日志；若该函数有错，错误明细里另给 last_error。
        # 这里给行首加状态标记，避免「最近一条看着正常、健康度却是 ERROR」的困惑。
        mark = {"ERROR": "✗", "WARN": "!", "OK": "·"}.get(b["health"], "·")
        lines.append(
            f"{mark} {name:<27} {b['total']:>6} {b['error']:>6} {b['warn']:>6} "
            f"{ratio:>8}  {_health_badge(b['health'])} {last:<20} {msg}"
        )

    # 错误云函数明细（错误码 / 阶段）
    err_funcs = [b for b in items if b["error"]]
    if err_funcs:
        lines.append("")
        lines.append(_c("异常明细", _C_BOLD))
        for b in err_funcs:
            codes = "、".join(f"{k}×{v}" for k, v in
                             sorted(b["errcodes"].items(), key=lambda kv: -kv[1])) or "-"
            stages = "、".join(f"{k}×{v}" for k, v in
                              sorted(b["stages"].items(), key=lambda kv: -kv[1])) or "-"
            lines.append(_c(f"  ✗ {b['name']}", _C_RED))
            lines.append(_c(f"      错误码: {codes}", _C_DIM))
            lines.append(_c(f"      阶段:   {stages}", _C_DIM))
            if b["last_error"]:
                when = b.get("last_error_time") or ""
                lines.append(_c(f"      最近错误{(' (' + when + ')') if when else ''}: {b['last_error']}", _C_DIM))
    return "\n".join(lines)


def render_summary(stats: "dict", server_url: str, ts: str) -> str:
    """一行总览。"""
    return (
        f"{_c('■', _C_BLUE)} {server_url}  "
        f"{_c('云函数', _C_DIM)} {stats['functions']}  "
        f"{_c('日志', _C_DIM)} {stats['total_rows']}  "
        f"{_c('错误', _C_RED)} {stats['errors']}  "
        f"{_c('告警函数', _C_YELLOW)} {stats['error_functions']}  "
        f"{_c('正常', _C_GREEN)} {stats['ok_functions']}  "
        f"{_c(ts, _C_DIM)}"
    )


# ─────────────────────────────── 落盘 ───────────────────────────────
def snapshot_dir() -> Path:
    """快照目录（项目根下 logs/cf_monitor）。"""
    root = Path(__file__).resolve().parent.parent
    d = root / SNAPSHOT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_snapshot(server_url: str, buckets: "dict", stats: "dict", out_json: str = "") -> str:
    """写 JSONL 快照 + 可选导出 JSON，返回快照文件路径。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "server_url": server_url,
        "summary": stats,
        "functions": list(buckets.values()),
    }
    jl = snapshot_dir() / "snapshots.jsonl"
    try:
        with jl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(_c(f"[warn] 快照写入失败: {e}", _C_YELLOW), file=sys.stderr)

    if out_json:
        try:
            Path(out_json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001
            print(_c(f"[warn] JSON 导出失败: {e}", _C_YELLOW), file=sys.stderr)
    return str(jl)


# ─────────────────────────────── 离线自检 ───────────────────────────────
# 样例刻意覆盖三类真实坑：
#   1. ❌ ECM download 失败     —— 真错误（带 ❌）
#   2. 「解析失败 0 / 失败 0」  —— **误报诱因**：共享解析器会判 error，必须挡掉
#   3. 嵌套 JSON 里的 Traceback —— 真错误，且消息要能解包出来给人看
_DRY_RUN_ROWS = [
    {"id": 1, "name": "SyncGdyyEmpPhoto", "log_type": "SyncGdyyEmpPhoto",
     "create_time": "2026-10-19 10:00:01",
     "content": '{"msg": "[RID:aa01] [2026-10-19.1] 工号=000233 ✅ 同步完成 emp_id=6350924"}'},
    {"id": 2, "name": "SyncGdyyEmpPhoto", "log_type": "SyncGdyyEmpPhoto",
     "create_time": "2026-10-19 10:00:02",
     "content": '{"msg": "[RID:aa02] [2026-10-19.2] 工号=000234 ❌ ECM download 失败：docId 返回空字节"}'},
    {"id": 3, "name": "SyncGdyyEmpPhoto", "log_type": "SyncGdyyEmpPhoto",
     "create_time": "2026-10-19 10:00:03",
     "content": '{"msg": "[RID:aa03] [2026-10-19.3] 工号=000235 ❌ ECM download 失败：docId 返回空字节"}'},
    {"id": 4, "name": "DailyReport", "log_type": "DailyReport",
     "create_time": "2026-10-19 10:01:00",
     "content": '[RID:bb01] [DAILY] stage=start 开始生成日报'},
    {"id": 5, "name": "DailyReport", "log_type": "DailyReport",
     "create_time": "2026-10-19 10:02:00",
     "content": '[RID:bb02] [DAILY] stage=end errcode=1002 生成失败'},
    {"id": 6, "name": "CleanTemp", "log_type": "CleanTemp",
     "create_time": "2026-10-19 10:03:00",
     "content": '[RID:cc01] [CLEAN] 清理临时文件完成'},
    # 误报诱因：统计类成功日志（失败 0）——必须判为 OK
    {"id": 7, "name": "SyncHuaqiRaw", "log_type": "SyncHuaqiRaw",
     "create_time": "2026-10-19 10:04:00",
     "content": '{"msg": "[RID:hq01] 扇出结束：请求 80 / 解析失败 0 / 新建 83 / 更新 0"}'},
    {"id": 8, "name": "SyncHuaqiRaw", "log_type": "SyncHuaqiRaw",
     "create_time": "2026-10-19 10:04:30",
     "content": '{"msg": "[RID:hq02] 落库：解析成功 19 条，解析失败 0 条；新建 0 / 更新 19"}'},
    # 嵌套两层 JSON 里的 Traceback —— 真错误
    {"id": 9, "name": "employee_effect", "log_type": "employee_effect",
     "create_time": "2026-10-19 10:05:00",
     "content": json.dumps({"content": "Traceback (most recent call last):\n"
                                      '  File "<dynamic_script:3929>", line 382, in _process_one\n'
                                      "KeyError: 'employee_id'"}, ensure_ascii=False)},
]


def dry_run() -> int:
    """离线自检：用样例数据跑通聚合 + 渲染，不发网络请求。"""
    buckets = aggregate(_DRY_RUN_ROWS)
    stats = summarize(buckets)
    print(_c("=== 离线自检 (dry-run，不联网) ===", _C_BOLD))
    print(render_summary(stats, "dry-run://local", datetime.now().strftime("%H:%M:%S")))
    print()
    print(render_table(buckets))
    print()
    print(_c("明细 JSON:", _C_DIM))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    # 5 个云函数 / 9 条日志 / 4 条真错误（2 ECM + 1 errcode=1002 + 1 Traceback）
    assert stats["functions"] == 5, stats
    assert stats["total_rows"] == 9, stats
    assert stats["errors"] == 4, stats
    # 误报防护：SyncHuaqiRaw 的「解析失败 0」不得计入错误
    assert buckets["SyncHuaqiRaw"]["error"] == 0, buckets["SyncHuaqiRaw"]
    assert buckets["SyncHuaqiRaw"]["health"] == HEALTH_OK, buckets["SyncHuaqiRaw"]
    # 错误码兜底：纯文本里的 errcode=1002 也要被抓到
    assert buckets["DailyReport"]["errcodes"].get("1002") == 1, buckets["DailyReport"]
    # 阶段兜底：纯文本里的 stage=end 也要被抓到
    assert buckets["DailyReport"]["stages"].get("end") == 1, buckets["DailyReport"]
    # 嵌套 JSON 解包：Traceback 要能显示出来（而不是一整坨转义字符）
    assert "Traceback" in buckets["employee_effect"]["last_error"], buckets["employee_effect"]
    print(_c("✓ 自检通过：解析 / 聚合 / 健康度 / 误报防护 / 嵌套解包 / errcode·stage 兜底均正常",
             _C_GREEN))
    return 0


# ─────────────────────────────── 主流程 ───────────────────────────────
async def _collect_rows(server_url: str, proxy: str, token: str, cookie: str,
                        model: str, page_size: int, log_type: str,
                        max_pages: int) -> "tuple":
    """按页抓取某模型，直到拿满 max_pages 或数据取尽。返回 (rows, total, err, model_missing)。"""
    filter_dict = {}
    if log_type:
        # 过滤字段随模型而变：dynamic_log 用 log_type，SyncOuterRecord 用 name
        filter_dict[model_type_field(model)] = log_type

    rows: "list" = []
    total = 0
    err = ""
    for page in range(1, max(1, max_pages) + 1):
        payload = {
            "model": model,
            "page_index": page,
            "page_size": page_size,
            "filter_dict": filter_dict,
        }
        page_rows, page_total, page_err, missing, _ = await _fetch_once(
            server_url, proxy, token, cookie, payload
        )
        if missing:
            return rows, total, page_err, True
        if page_err and not page_rows:
            # 可能是 token 失效 → 刷新一次重试该页
            err = page_err
            fresh = await cf_refresh_token(server_url, proxy)
            if isinstance(fresh, dict) and (fresh.get("token") or fresh.get("cookie")):
                token = (fresh.get("token") or "").strip()
                cookie = (fresh.get("cookie") or "").strip() or f"token={token}"
                page_rows, page_total, page_err, missing2, _ = await _fetch_once(
                    server_url, proxy, token, cookie, payload
                )
                if missing2:
                    return rows, total, page_err, True
            if page_err and not page_rows:
                return rows, total, page_err, False
        total = page_total or total
        rows.extend(page_rows)
        # 该页不足一页 → 已到末尾；或累计已达平台总数
        if len(page_rows) < page_size or (total and len(rows) >= total):
            break
    return rows, total, err, False


async def monitor_once(server_url: str, proxy: str, token: str, cookie: str,
                       page_size: int, record_model: str, log_type: str,
                       max_pages: int = 1) -> "tuple":
    """跑一轮：拉日志 → 聚合 → 返回 (buckets, stats, err, used_model)。

    ``record_model`` 为 "auto" 时按 RECORD_MODELS 顺序探测：
    平台回「模型不存在」（errcode 80001）就换下一个模型，直到取到数据。
    """
    models = list(RECORD_MODELS) if record_model == "auto" else [record_model]
    last_err = ""
    used_model = ""
    rows: "list" = []
    total = 0
    tried: "list[str]" = []

    for model in models:
        tried.append(model)
        rows, total, err, missing = await _collect_rows(
            server_url, proxy, token, cookie, model, page_size, log_type, max_pages
        )
        if missing:
            # 该模型在此租户不存在 → 换下一个（不是鉴权问题，不重登）
            last_err = err
            continue
        if rows or not err:
            used_model = model
            last_err = err
            break
        last_err = err

    if not used_model:
        # 全部模型都失败：不要假装「成功但没数据」，把真实错误带出去
        used_model = ""
        if not last_err:
            last_err = f"所有模型均不可用（已尝试: {', '.join(tried)}）"

    buckets = aggregate(rows)
    stats = summarize(buckets)
    stats["platform_total"] = total
    stats["rows_fetched"] = len(rows)
    stats["fetch_error"] = last_err
    stats["record_model"] = used_model
    stats["models_tried"] = tried
    return buckets, stats, last_err, used_model


def main() -> int:
    ap = argparse.ArgumentParser(
        description="本地监控云函数运行状态（按云函数聚合 HCM 记录模型日志）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-s", "--server", default="",
                    help="HCM 服务器 URL，或 cf_accounts 里的 name（自动解析 URL）")
    ap.add_argument("-i", "--interval", type=int, default=30, help="轮询间隔秒（默认 30）")
    ap.add_argument("-n", "--rounds", type=int, default=0,
                    help="监控轮数，0 = 一直跑（Ctrl-C 退出）")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（等价 --rounds 1）")
    ap.add_argument("--page-size", type=int, default=1000,
                    help="每页条数（默认 1000，平台实测上限；小页会被高频函数独占，覆盖不到其它函数）")
    ap.add_argument("--pages", type=int, default=3,
                    help="每轮抓几页（默认 3；单页常被高频函数独占，3 页实测可覆盖全部约 10 个云函数，耗时 ~2.6s）")
    ap.add_argument("--record-model", default="auto",
                    help="记录模型名：auto（默认，按 dynamic_log→SyncOuterRecord→SysLogRecord 探测）"
                         " / dynamic_log / SyncOuterRecord / SysLogRecord")
    ap.add_argument("--log-type", default="", help="只看某个云函数（对应 log_type / name）")
    ap.add_argument("--proxy", default="", help="代理地址（默认取账号自带 http_proxy）")
    ap.add_argument("--token", default="", help="手动传 token（留空则用缓存/自动刷新）")
    ap.add_argument("--json", metavar="PATH", help="把最后一轮结果导出为 JSON")
    ap.add_argument("--only-errors", action="store_true",
                    help="只展示有错误的云函数（退出码非 0，便于巡检/CI）")
    ap.add_argument("--no-snapshot", action="store_true", help="不写本地 JSONL 快照")
    ap.add_argument("--dry-run", action="store_true", help="离线自检（不联网）")
    args = ap.parse_args()

    if args.dry_run:
        return dry_run()
    if not args.server:
        ap.error("必须提供 -s/--server（或用 --dry-run 离线自检）")

    server_url = _resolve_server(args.server)
    acc = _account_for(server_url)
    proxy = args.proxy or (acc.get("http_proxy") or "")

    if args.token.strip():
        token, cookie = args.token.strip(), f"token={args.token.strip()}"
    else:
        token, cookie = _load_token(server_url, proxy)

    if not (token or cookie):
        print(_c(f"✗ 没有可用凭证：{server_url}（请先在 GUI 里登录，或用 --token 指定）", _C_RED))
        return 2

    rounds = 1 if args.once else (args.rounds if args.rounds > 0 else 0)
    interval = max(5, min(int(args.interval or 30), 3600))

    print(_c(f"■ 云函数运行状态监控 —— {server_url}", _C_BOLD))
    print(_c(f"  间隔 {interval}s · 每页 {args.page_size} 条 × {args.pages} 页 · 模型 {args.record_model}"
             f"{' · 过滤 ' + args.log_type if args.log_type else ''}", _C_DIM))
    print(_c("  Ctrl-C 退出", _C_DIM))
    print()

    rc = 0
    round_no = 0
    try:
        while True:
            round_no += 1
            buckets, stats, err, used_model = asyncio.run(
                monitor_once(server_url, proxy, token, cookie,
                            args.page_size, args.record_model, args.log_type, args.pages)
            )
            ts = datetime.now().strftime("%H:%M:%S")
            print(render_summary(stats, server_url, ts))
            if used_model and used_model != args.record_model:
                print(_c(f"  (模型自动降级: {args.record_model} → {used_model})", _C_DIM))

            if err:
                print(_c(f"  ✗ 取数失败: {err}", _C_RED))
                if not stats.get("record_model"):
                    print(_c("    可能是 token 过期（HCM token 默认 2 小时）/ 凭证无权限；"
                             "请先在 GUI 里重新登录，或用 --token 指定", _C_DIM))
                rc = 3
            elif args.only_errors:
                errs = {k: v for k, v in buckets.items() if v["error"]}
                if errs:
                    print(render_table(errs))
                    rc = max(rc, 1)
                else:
                    print(_c("  ✓ 没有检测到错误的云函数", _C_GREEN))
            else:
                print(render_table(buckets))
                if stats["error_functions"]:
                    rc = max(rc, 1)

            if not args.no_snapshot:
                save_snapshot(server_url, buckets, stats, args.json if round_no == rounds or rounds == 1 else "")
            elif args.json and (round_no == rounds or rounds == 1):
                Path(args.json).write_text(
                    json.dumps({"summary": stats, "functions": list(buckets.values())},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            print()
            if rounds and round_no >= rounds:
                break
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        print()
        print(_c("已停止监控", _C_DIM))

    return rc


if __name__ == "__main__":
    sys.exit(main())
