# -*- coding: utf-8 -*-
"""云函数运行状态监控（后端聚合层）。

把「按云函数聚合 dynamic_log / SyncOuterRecord」这套逻辑放到后端，供
**UI 面板** 与 **命令行脚本 tools/cf_monitor.py** 共用同一份实现 —— 避免两边
各自维护一套解析/判定规则而慢慢漂移（CLI 早期踩过的误报坑必须同步到 UI）。

为什么不是纯前端做
------------------
1. 鉴权 / token 自动重登 / SSL 旧式重协商 / 模型自动降级全在 ``cf_query_logs``
   里，前端不该再复刻一遍；
2. 聚合涉及几千行日志的结构化解析（``parse_cf_log_content``），放后端省一次
   大 JSON 传输后的重复计算；
3. 多页抓取是串行 HTTP 往返，后端做天然可控（前端做会占满浏览器连接池）。

关键判定规则（与 CLI 完全一致，改动务必同步更新 tests/test_cf_monitor.py）
---------------------------------------------------------------------------
- **模型自动降级**：不是每个租户都有 ``dynamic_log``。平台回 ``errcode 80001``
  表示「模型不存在」，此时换 ``SyncOuterRecord`` → ``SysLogRecord``，且**不能**
  当成登录失效去重登。
- **「失败 0」不是失败**：共享解析器把出现「失败」二字就当错误提示，导致
  ``解析失败 0`` 这类统计行被标成 ERROR。这里做二次判定，忽略零值统计，
  但保留真信号（❌ / traceback / errcode≠0 / 显式 ERROR 标签）。
- **页大小**：单页 200 条常被单个高频函数独占，其它函数根本进不了样本。
  默认 1000 条/页 × 3 页，实测可覆盖全部约 10 个云函数。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from api.common import logger
from api.cf.cf_logs import cf_query_logs
from api.cf.cf_diagnose import parse_cf_log_content

# ─────────────────────────────── 常量 ───────────────────────────────
# 模型探测顺序：dynamic_log 优先（字段最语义化），退到 SyncOuterRecord / SysLogRecord
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

# 健康度
HEALTH_OK = "OK"
HEALTH_WARN = "WARN"
HEALTH_ERROR = "ERROR"
HEALTH_UNKNOWN = "UNKNOWN"

# 告警阈值
ERROR_COUNT_THRESHOLD = 5   # 错误数 ≥ 5 → ERROR
ERROR_RATIO_THRESHOLD = 0.10  # 错误占比 ≥ 10% → WARN（样本小时的辅助判据）

# 默认抓取规模（见模块 docstring 的说明）
DEFAULT_PAGE_SIZE = 1000
DEFAULT_PAGES = 3
MAX_PAGE_SIZE = 1000
MAX_PAGES = 20


# ─────────────────────────────── 工具 ───────────────────────────────
def model_type_field(record_model: str) -> str:
    """该模型用于「只看某个云函数」过滤的字段名。"""
    return _MODEL_TYPE_FIELDS.get((record_model or "").strip().lower(), "log_type")


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


# 日志正文提取上限：详情要能看全（平台单条日志通常几十到几百字符，
# traceback 可能上千），但不能把整页 JSON 灌进接口。
_FULL_MSG_MAX = 4000
# 表格「最近一条」列只放短摘要，超出省略（展开详情里给全文）
_ROW_MSG_MAX = 160
# 展开详情里保留多少条不同的错误样本（同一函数可能有多种失败原因）
_ERR_SAMPLE_MAX = 8


def _row_full_message(row: dict, parsed: dict) -> str:
    """取一行的完整可读正文（保留换行，供详情面板展示）。

    content 实测有几种嵌套形态：
      - ``{"msg": "..."}``                 —— 最常见
      - ``{"content": "Traceback ..."}``   —— 异常行把 traceback 塞在 content 里
      - ``'{"content": "{\\"content\\": ...}"}'`` —— 双层字符串再包一层
    """
    msg: Any = parsed.get("message") or row.get("content") or ""
    if isinstance(msg, (dict, list)):
        msg = json.dumps(msg, ensure_ascii=False, indent=2)

    content = row.get("content")
    if isinstance(content, str):
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
                    # 字典里没有约定字段：整条格式化输出，信息不要丢
                    msg = json.dumps(nxt, ensure_ascii=False, indent=2)
                    break
                msg = inner
                cur = inner
            elif isinstance(nxt, str):
                msg = cur = nxt
            else:
                break

    text = str(msg).replace("\r\n", "\n").replace("\r", "\n")
    # 保留换行（traceback 靠缩进表达调用栈），只压掉行内连续空白与空行
    lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln.strip())
    return text[:_FULL_MSG_MAX]


def _row_message(row: dict, parsed: dict) -> str:
    """表格用的单行短摘要（由完整正文压平而来）。"""
    text = _row_full_message(row, parsed)
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:_ROW_MSG_MAX]


# ─────────────────────────── 误报防护 ───────────────────────────
# 共享解析器的 _ERROR_HINTS 含「失败」二字，于是「解析失败 0 / 失败 0」这类
# **统计类成功日志**会被判成 error（实测 SyncHuaqiRaw「扇出结束：… 解析失败 0 …」）。
# 监控看板一旦把正常日志标红，整块看板就不可信了 —— 做二次判定：
#   1) 「失败/错误/异常」后面紧跟 0（或「0 条/个/次」）视为统计 0，不算错；
#   2) 若没有其它错误信号（traceback / errcode≠0 / 显式标签），不算错。
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


def is_false_positive(raw: Any, parsed: dict) -> bool:
    """共享解析器判 error，但实际只是「失败 0」这类统计日志 → 不算错。"""
    if not parsed.get("is_error"):
        return False
    if not isinstance(raw, str) or not raw:
        return False
    for key in ("ERROR", "DIAG_ERR", "EXCEPTION", "FAILED"):
        if key in (parsed.get("tags") or {}):
            return False
    ec = parsed.get("errcode")
    if isinstance(ec, int) and ec != 0:
        return False
    if _ZERO_COUNT_RE.search(raw) and not _REAL_ERROR_RE.search(raw):
        return True
    return False


# ─────────────────────────────── 聚合 ───────────────────────────────
# RID / 请求号 / 时间戳这类前缀每条都不同，去重时必须先剥掉
_RID_PREFIX_RE = re.compile(r"\[RID:[^\]]*\]|\[[A-Za-z0-9_\-]{6,}\]")
_TS_PREFIX_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\b")


def _err_sample_key(full: str) -> str:
    """错误样本去重键：剥掉每条都不同的 RID / 时间戳，只留错误语义。

    实测「打卡时间不在审批范围内」这条错误重复 30+ 次，仅 RID 不同；
    若按原文去重会把样本位占满，导致第二条真正不同的错误看不到。
    """
    t = _RID_PREFIX_RE.sub("", full or "")
    t = _TS_PREFIX_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:300]


def aggregate(rows: "List[dict]") -> "Dict[str, dict]":
    """把原始日志行聚合成「按云函数名」的运行状态看板。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = _row_name(row)
        raw = row.get("content")
        parsed = parse_cf_log_content(raw, row.get("log_type", ""))
        is_err = bool(parsed.get("is_error")) and not is_false_positive(raw, parsed)

        b = buckets.setdefault(name, {
            "name": name,
            "total": 0,
            "error": 0,
            "warn": 0,
            "info": 0,
            "last_time": "",
            "last_message": "",
            "last_error": "",
            "last_error_full": "",
            "last_error_time": "",
            "errcodes": {},
            "stages": {},
            # 错误样本：同一函数 56 条错误往往不是同一个原因，只给「最近一条」
            # 会让用户以为只有一个问题。这里按正文去重保留最近几条（新→旧）。
            "_err_samples": [],
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
        # 错误行：维护「最近一条」与「多种错误样本」两份信息。
        # ⚠️ 两者必须分开：最近一条只在时间更新时才换（避免被旧日志覆盖），
        # 而样本要收集**所有**错误（含更早的），否则第二类失败原因永远看不到
        # —— 早期实现把样本收集写进了 ts>=last_error_time 分支，实测直接吞掉了历史 errors。
        if is_err:
            full = _row_full_message(row, parsed)
            short = _row_message(row, parsed)
            if ts >= b["last_error_time"]:
                b["last_error_time"] = ts
                b["last_error"] = short
                # 详情面板要能看全（短摘要只够表格一列）
                b["last_error_full"] = full

            samples = b["_err_samples"]
            # 去重键要忽略 RID / 时间戳这类每条都不同的前缀，否则同一错误
            # 重复 56 次会被当成 56 种不同的错误，把真正不同的第二条错误挤掉。
            key = _err_sample_key(full)
            if full:
                hit = next((x for x in samples if x["_key"] == key), None)
                if hit is None:
                    hit = {"time": ts, "short": short, "full": full, "_key": key}
                    samples.append(hit)
                elif ts > hit["time"]:
                    # 同一类错误再次出现：把时间刷新为「最近一次发生」，
                    # 样本按时间倒序才有意义（否则显示首次出现时间，容易误判已恢复）
                    hit["time"] = ts
                    hit["short"] = short
                    hit["full"] = full
                samples.sort(key=lambda x: x["time"], reverse=True)
                if len(samples) > _ERR_SAMPLE_MAX:
                    del samples[_ERR_SAMPLE_MAX:]

        ec = parsed.get("errcode")
        if not (isinstance(ec, int) and ec != 0):
            ec = _fallback_errcode(raw)
        if isinstance(ec, int) and ec != 0:
            b["errcodes"][str(ec)] = b["errcodes"].get(str(ec), 0) + 1
        st = parsed.get("stage") or _fallback_stage(raw)
        if st:
            b["stages"][st] = b["stages"].get(st, 0) + 1

    out: Dict[str, Dict[str, Any]] = {}
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
        # 错误码 / 阶段按次数降序（前端直接渲染，不用再排）
        b["top_errcodes"] = [
            {"code": k, "count": v}
            for k, v in sorted(b["errcodes"].items(), key=lambda kv: -kv[1])
        ]
        b["top_stages"] = [
            {"stage": k, "count": v}
            for k, v in sorted(b["stages"].items(), key=lambda kv: -kv[1])
        ]
        samples = b.pop("_err_samples", [])
        b["error_samples"] = [
            {"time": x["time"], "short": x["short"], "full": x["full"]} for x in samples
        ]
        out[name] = b
    return out


def summarize(buckets: "Dict[str, dict]") -> dict:
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


# ─────────────────────────────── 取数 ───────────────────────────────
def _extract_rows(result: Any) -> "Tuple[list, int]":
    """从 cf_query_logs 返回值提取 (rows, total)。

    返回形如 ``{method, raw, data}``，其中 ``data`` 可能是 list（HCM result
    直出数组）或 ``{list: [...], count: N}``（与 cf_stream._extract_rows 同口径）。
    """
    if not isinstance(result, dict):
        return [], 0
    payload = result.get("data")
    if payload is None:
        payload = result.get("result", result)
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


def _is_model_missing(err: Any) -> bool:
    """判定失败原因是否为「模型在当前租户不存在」（换模型即可，不是鉴权问题）。"""
    msg = str(err or "")
    return ("Unknown Model Name" in msg
            or "80001" in msg
            or ("model" in msg.lower() and ("不存在" in msg or "NotFound" in msg)))


async def _query_page(server_url: str, token: str, proxy: str, record_model: str,
                      log_type: str, page: int, page_size: int):
    """拉一页日志。返回 (rows, total, error, model_missing)。"""
    req = SimpleNamespace(
        server_url=server_url,
        token=token,
        log_type=log_type,
        record_model=record_model,
        page_index=page,
        page_size=page_size,
        proxy=proxy,
    )
    try:
        result = await cf_query_logs(req)
    except Exception as e:  # noqa: BLE001
        return [], 0, e, _is_model_missing(e)
    rows, total = _extract_rows(result)
    return rows, total, None, False


async def collect(server_url: str, token: str = "", proxy: str = "",
                  record_model: str = "auto", log_type: str = "",
                  page_size: int = DEFAULT_PAGE_SIZE,
                  pages: int = DEFAULT_PAGES) -> dict:
    """抓取并聚合一轮监控数据（供 UI 轮询调用）。

    参数：
      server_url   环境地址（必须是 cf_accounts 里已配置的环境，否则拿不到账号重登）
      token        可选，手动指定 token；留空则用缓存 / 自动重登
      proxy        可选代理
      record_model "auto"（默认，自动探测）或具体模型名
      log_type     只看某个云函数（按模型映射到 log_type / name 字段）
      page_size    每页条数（上限 1000）
      pages        抓几页（上限 20）

    返回：``{ok, server_url, at, record_model, models_tried, rows_fetched,
             platform_total, summary, functions[], error}``
    """
    server_url = (server_url or "").strip().rstrip("/")
    if not server_url:
        raise ValueError("请选择或填写云函数服务器地址（server_url）")

    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    pages = max(1, min(int(pages or DEFAULT_PAGES), MAX_PAGES))
    models = list(RECORD_MODELS) if (record_model or "auto") == "auto" else [record_model]

    rows: "List[dict]" = []
    total = 0
    used_model = ""
    last_err: Any = None
    models_tried: "List[str]" = []

    for model in models:
        models_tried.append(model)
        rows, total, last_err, missing = await _query_page(
            server_url, token, proxy, model, log_type, 1, page_size
        )
        if missing:
            # 该模型在此租户不存在 → 换下一个（不是鉴权问题，不重登）
            logger.info(f"[CF-MONITOR] 模型 {model} 不存在于 {server_url}，尝试下一个")
            continue
        if last_err is not None or not rows:
            # 有 rows 就认（即使报错也先把能拿到的展示出来）
            if not rows:
                continue
        used_model = model
        break

    # 多页抓取：首屏已拿到就继续翻页，直到取尽或到达上限
    if used_model and rows:
        for page in range(2, pages + 1):
            more, page_total, page_err, _missing = await _query_page(
                server_url, token, proxy, used_model, log_type, page, page_size
            )
            if page_err is not None and not more:
                break
            total = page_total or total
            rows.extend(more)
            if len(more) < page_size or (total and len(rows) >= total):
                break

    if not used_model:
        # 全部模型都失败：明确暴露真实错误，不要假装「成功但没数据」
        msg = str(last_err or f"所有模型均不可用（已尝试: {', '.join(models_tried)}）")
        logger.warning(f"[CF-MONITOR] 取数失败 {server_url}: {msg[:300]}")
        return {
            "ok": False,
            "server_url": server_url,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summarize({}),
            "functions": [],
            "record_model": "",
            "models_tried": models_tried,
            "rows_fetched": 0,
            "platform_total": 0,
            "error": msg,
        }

    buckets = aggregate(rows)
    stats = summarize(buckets)
    stats["platform_total"] = total
    stats["rows_fetched"] = len(rows)
    stats["record_model"] = used_model

    # 前端直接渲染的顺序：先按健康度（ERROR → WARN → UNKNOWN → OK），再按错误数、总数
    order = {HEALTH_ERROR: 0, HEALTH_WARN: 1, HEALTH_UNKNOWN: 2, HEALTH_OK: 3}
    funcs = sorted(
        buckets.values(),
        key=lambda b: (order.get(b["health"], 9), -b["error"], -b["total"]),
    )

    return {
        "ok": True,
        "server_url": server_url,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": stats,
        "functions": funcs,
        "record_model": used_model,
        "models_tried": models_tried,
        "rows_fetched": len(rows),
        "platform_total": total,
        "error": "",
    }
