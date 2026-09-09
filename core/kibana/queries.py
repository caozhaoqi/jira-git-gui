# -*- coding: utf-8 -*-
"""ES DSL 构造与响应解析（**纯函数**，不依赖 FastAPI / 网络，便于单测）。

约定
----
- 站内字段：``kubernetes.pod_name`` 等是 ``text`` 类型，**聚合与精确过滤必须用
  ``.keyword`` 子字段**（实测直接聚合会报 fielddata disabled）。
- 时间字段默认 ``es_time``，值为 **UTC**（日志正文里的时间才是 +08:00 本地时间）。
  因此本模块接收的绝对时间一律按「本机本地时间」解释，统一转成 UTC 再下发。
- 日志级别没有独立字段，嵌在正文里（如 ``... stderr F INFO 2814... [a.py:1] ...``），
  由 :func:`guess_level` 从正文前段抽取。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ES 的相对时间表达式，直接透传（now-1h / now/d / now-7d …）
_REL_TIME_RE = re.compile(r"^now([-+*/]\d+[smhdwMy])*(/[smhdwMy])?$", re.I)

_LEVEL_RE = re.compile(
    r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL|SEVERE)\b", re.I)

# 前端下拉里的级别 -> 实际参与检索的关键词（正文匹配，命中其一即算）
LEVEL_KEYWORDS = {
    "TRACE": ["trace"],
    "DEBUG": ["debug"],
    "INFO": ["info"],
    "WARN": ["warn", "warning"],
    "ERROR": ["error", "fatal", "critical", "exception"],
}


# --------------------------------------------------------------------------- #
#  时间处理
# --------------------------------------------------------------------------- #
def is_relative_time(value: str) -> bool:
    return bool(_REL_TIME_RE.match((value or "").strip()))


def to_es_time(value: str) -> Optional[str]:
    """把用户输入的时间转成 ES 可接受的 UTC 字符串。

    - 空 → ``None``
    - ES 相对表达式（``now-1h`` / ``now/d`` …）→ 原样返回
    - 绝对时间 → 按**本机本地时区**解析，转 UTC，输出 ``YYYY-MM-DDTHH:MM:SS.sssZ``

    支持 ``2026-09-04 15:00``/``2026-09-04T15:00``/``2026-09-04T15:00:00+08:00``
    等常见写法；解析失败时原样返回（交给 ES 报错，便于定位输入问题）。
    """
    v = (value or "").strip()
    if not v:
        return None
    if is_relative_time(v):
        return v
    try:
        s = v.replace("Z", "").strip()
        # 去掉小数秒（datetime.fromisoformat 在 3.9 只支持 3/6 位）
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        dt = datetime.fromisoformat(s)
    except ValueError:
        return v
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 朴素时间按本机时区解释
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def build_time_range(start: str, end: str, time_field: str = "es_time") -> Optional[dict]:
    """构造 ``{"range": {time_field: {...}}}``；两端都空则返回 None。"""
    gt, lte = to_es_time(start), to_es_time(end)
    if not gt and not lte:
        return None
    bounds: Dict[str, Any] = {}
    if gt:
        bounds["gte"] = gt
    if lte:
        bounds["lte"] = lte
    return {"range": {time_field: bounds}}


def pick_histogram_interval(start: str, end: str) -> str:
    """按时间跨度挑一个直方图间隔（目标：60~200 根柱子）。"""
    span = _span_seconds(start, end)
    if span is None:
        return "1h"
    for sec, interval in (
        (15 * 60, "10s"), (60 * 60, "30s"), (6 * 3600, "1m"),
        (24 * 3600, "5m"), (3 * 86400, "15m"), (7 * 86400, "30m"),
        (30 * 86400, "2h"),
    ):
        if span <= sec:
            return interval
    return "6h"


def _span_seconds(start: str, end: str) -> Optional[float]:
    """粗略估算跨度秒数；含相对时间时按「now」求值。"""
    def _epoch(v: str) -> Optional[float]:
        if not v:
            return None
        if is_relative_time(v):
            m = re.match(r"^now-(\d+)([smhdwMy])$", v)
            if not m:
                # now / now+1h / now/d 等无法简单换算，交给默认分支处理
                return None
            n = int(m.group(1))
            unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
                    "M": 2592000, "y": 31536000}[m.group(2)]
            return datetime.now().timestamp() - n * unit
        try:
            s = re.sub(r"\.(\d{6})\d+", r".\1", v.replace("Z", "").strip())
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt.timestamp()
        except ValueError:
            return None

    now = datetime.now().timestamp()
    a, b = _epoch(start), _epoch(end)
    # 缺省端补齐：end 空 → 现在；start 空 → end 往前 1 小时（双空则按 1 小时算）
    if b is None:
        b = now
    if a is None:
        a = b - 3600
    return max(0.0, b - a)


# --------------------------------------------------------------------------- #
#  过滤条件
# --------------------------------------------------------------------------- #
def term_filter(field: str, value: str) -> Optional[dict]:
    return {"term": {field: value}} if value else None


def terms_filter(field: str, values: Sequence[str]) -> Optional[dict]:
    vals = [v for v in (values or []) if v]
    return {"terms": {field: vals}} if vals else None


def keyword_filter(msg_field: str, keyword: str) -> Optional[dict]:
    """关键词检索。

    含 ``*``/``?``/``AND``/``OR``/``NOT``/``:`` 时走 ``query_string``（支持
    ``level:ERROR AND pod:xxx`` 这类写法），否则走 ``match_phrase``（更快、
    不会被特殊字符炸掉）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None
    if re.search(r"[*?:]|^\s*(AND|OR|NOT)\s|\s(AND|OR|NOT)\s", kw):
        return {"query_string": {"query": kw, "default_field": msg_field}}
    return {"match_phrase": {msg_field: kw}}


def level_filter(msg_field: str, levels: Sequence[str]) -> Optional[dict]:
    """日志级别过滤：级别无独立字段，降级为正文 OR 匹配。"""
    words: List[str] = []
    for lv in (levels or []):
        words += LEVEL_KEYWORDS.get(str(lv).upper(), [])
    words = list(dict.fromkeys(words))
    if not words:
        return None
    if len(words) == 1:
        return {"match_phrase": {msg_field: words[0]}}
    return {"bool": {"should": [{"match_phrase": {msg_field: w}} for w in words],
                     "minimum_should_match": 1}}


def build_filters(
    *,
    time_field: str = "es_time",
    msg_field: str = "log",
    field_prefix: str = "kubernetes",
    start: str = "",
    end: str = "",
    namespace: str = "",
    namespaces: Sequence[str] = None,
    container: str = "",
    containers: Sequence[str] = None,
    pod: str = "",
    pods: Sequence[str] = None,
    app: str = "",
    host: str = "",
    keyword: str = "",
    levels: Sequence[str] = None,
    exclude_keyword: str = "",
) -> List[dict]:
    """把各维度条件拼成 ``bool.filter`` 列表（**全部是与关系**）。"""
    p = (field_prefix or "").strip(".")
    def kf(name: str, kw: bool = True) -> str:
        base = f"{p}.{name}" if p else name
        return f"{base}.keyword" if kw else base

    fl: List[dict] = []
    tr = build_time_range(start, end, time_field)
    if tr:
        fl.append(tr)
    for f in (
        term_filter(kf("namespace_name"), namespace),
        terms_filter(kf("namespace_name"), namespaces),
        term_filter(kf("container_name"), container),
        terms_filter(kf("container_name"), containers),
        term_filter(kf("pod_name"), pod),
        terms_filter(kf("pod_name"), pods),
        term_filter(kf("labels.app"), app),
        term_filter(kf("host"), host),
        keyword_filter(msg_field, keyword),
        level_filter(msg_field, levels),
    ):
        if f:
            fl.append(f)
    neg = (exclude_keyword or "").strip()
    if neg:
        fl.append({"bool": {"must_not": [keyword_filter(msg_field, neg)]}})
    return fl


# --------------------------------------------------------------------------- #
#  查询体构造
# --------------------------------------------------------------------------- #
def build_search_dsl(
    filters: Sequence[dict],
    *,
    time_field: str = "es_time",
    size: int = 200,
    from_: int = 0,
    order: str = "desc",
    track_total_hits: Any = 10000,
) -> dict:
    """日志检索 DSL。

    ``order=asc`` 用于「跟随最新」场景先取 oldest 再正序展示；
    ``order=desc`` 用于常规「最新在前」。
    """
    size = max(1, min(int(size or 200), 1000))
    from_ = max(0, min(int(from_ or 0), 9000))
    return {
        "size": size,
        "from": from_,
        "track_total_hits": track_total_hits,
        "query": {"bool": {"filter": list(filters or [])}} if filters
                 else {"match_all": {}},
        "sort": [{time_field: {"order": "desc" if order != "asc" else "asc"}}],
    }


def build_terms_dsl(
    filters: Sequence[dict],
    aggs: Dict[str, dict],
    size: int = 0,
) -> dict:
    """纯聚合 DSL（``size=0`` 不返回原始文档）。"""
    return {
        "size": size,
        "track_total_hits": False,
        "query": {"bool": {"filter": list(filters or [])}} if filters
                 else {"match_all": {}},
        "aggs": aggs or {},
    }


def build_pods_overview_dsl(
    filters: Sequence[dict],
    *,
    field_prefix: str = "kubernetes",
    msg_field: str = "log",
    time_field: str = "es_time",
    size: int = 300,
    error_keywords: Sequence[str] = ("ERROR", "FATAL", "CRITICAL", "Exception"),
) -> dict:
    """Pod 概览：一次聚合出每个 Pod 的日志量 / 错误数 / 归属应用·容器·节点 / 最后写入时间。

    之所以塞进**一次**请求：Console Proxy 单次固定开销约 4s，拆成多个聚合会把
    延迟线性放大（对齐项目「跨多层的远端操作必须合并或并发」的性能约定）。
    """
    p = (field_prefix or "").strip(".")
    def kf(name: str) -> str:
        return f"{p}.{name}.keyword" if p else f"{name}.keyword"

    should = [{"match_phrase": {msg_field: w}} for w in error_keywords]
    return build_terms_dsl(
        filters,
        {
            "pods": {
                "terms": {"field": kf("pod_name"), "size": size,
                          "order": {"_count": "desc"}},
                "aggs": {
                    "errors": {"filter": {"bool": {"should": should,
                                                   "minimum_should_match": 1}}},
                    "app": {"terms": {"field": kf("labels.app"), "size": 1}},
                    "container": {"terms": {"field": kf("container_name"), "size": 3}},
                    "namespace": {"terms": {"field": kf("namespace_name"), "size": 1}},
                    "host": {"terms": {"field": kf("host"), "size": 1}},
                    "last": {"max": {"field": time_field}},
                },
            }
        },
    )


def build_histogram_dsl(
    filters: Sequence[dict],
    *,
    time_field: str = "es_time",
    msg_field: str = "log",
    start: str = "",
    end: str = "",
    error_keywords: Sequence[str] = ("ERROR", "FATAL", "CRITICAL", "Exception"),
) -> dict:
    """时间直方图（总量 + 错误量两条曲线）。"""
    interval = pick_histogram_interval(start, end)
    should = [{"match_phrase": {msg_field: w}} for w in error_keywords]
    return build_terms_dsl(
        filters,
        {
            "over_time": {
                "date_histogram": {"field": time_field,
                                   "fixed_interval": interval,
                                   "min_doc_count": 0},
                "aggs": {
                    "errors": {"filter": {"bool": {"should": should,
                                                   "minimum_should_match": 1}}}
                },
            }
        },
    )


def build_context_dsl(
    filters: Sequence[dict],
    *,
    time_field: str = "es_time",
    anchor: str,
    size: int = 50,
    direction: str = "before",
) -> dict:
    """以某条日志为锚点取上下文。

    ``direction='before'`` → 取早于锚点的 ``size`` 条（按时间倒序，即「紧邻的上文」）；
    ``direction='after'``  → 取晚于锚点的 ``size`` 条（按时间正序，即「紧邻的下文」）。
    """
    size = max(1, min(int(size or 50), 500))
    fl = list(filters or [])
    op = "lt" if direction == "before" else "gt"
    fl = [f for f in fl if time_field not in _range_fields(f)]
    fl.append({"range": {time_field: {op: anchor}}})
    return {
        "size": size,
        "track_total_hits": False,
        "query": {"bool": {"filter": fl}} if fl else {"match_all": {}},
        "sort": [{time_field: {"order": "desc" if direction == "before" else "asc"}}],
    }


def _range_fields(node: dict) -> Iterable[str]:
    """取出 filter 节点里 range 涉及的字段名（用于剔除时间条件）。"""
    r = (node or {}).get("range")
    if isinstance(r, dict):
        return r.keys()
    return []


# --------------------------------------------------------------------------- #
#  响应解析
# --------------------------------------------------------------------------- #
def guess_level(text: str) -> str:
    """从日志正文前段猜级别；猜不出返回空串。

    只看前 200 字符：正文里业务内容可能包含 "error" 字样，越靠后越不可信。
    """
    head = (text or "")[:200]
    m = _LEVEL_RE.search(head)
    if not m:
        return ""
    lv = m.group(1).upper()
    return "WARN" if lv == "WARNING" else lv


def parse_hits(resp: dict, *, field_prefix: str = "kubernetes",
               msg_field: str = "log", time_field: str = "es_time") -> List[dict]:
    """ES ``_search`` 响应 → 归一化日志行列表。"""
    p = (field_prefix or "").strip(".")
    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    out: List[dict] = []
    for h in hits:
        src = h.get("_source") or {}
        k = src.get(p or "kubernetes") or {} if p else {}
        labels = (k.get("labels") or {})
        msg = src.get(msg_field) or ""
        ts = src.get(time_field) or ""
        out.append({
            "id": h.get("_id", ""),
            "index": h.get("_index", ""),
            "ts": ts,
            "ts_ms": _iso_to_ms(ts),
            "level": guess_level(msg),
            "msg": msg,
            "pod": k.get("pod_name", ""),
            "container": k.get("container_name", ""),
            "namespace": k.get("namespace_name", ""),
            "host": k.get("host", ""),
            "app": labels.get("app", ""),
        })
    return out


def _iso_to_ms(value: str) -> Optional[int]:
    """``2026-09-04T07:31:32.152Z`` → epoch 毫秒；不可解析返回 None。"""
    v = (value or "").strip()
    if not v:
        return None
    try:
        s = re.sub(r"\.(\d{6})\d+", r".\1", v.replace("Z", ""))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return None


def parse_buckets(resp: dict, agg: str) -> List[dict]:
    """取某个 terms 聚合的桶 → ``[{key, doc_count}]``。"""
    node = ((resp or {}).get("aggregations") or {}).get(agg) or {}
    return [{"key": b.get("key"), "count": b.get("doc_count", 0)}
            for b in (node.get("buckets") or [])]


def parse_pods_overview(resp: dict) -> List[dict]:
    """解析 :func:`build_pods_overview_dsl` 的结果。"""
    node = ((resp or {}).get("aggregations") or {}).get("pods") or {}
    out: List[dict] = []
    for b in (node.get("buckets") or []):
        sub = b or {}

        def _first(name: str) -> str:
            buckets = ((sub.get(name) or {}).get("buckets") or [])
            return buckets[0].get("key", "") if buckets else ""

        last = sub.get("last") or {}
        last_ms = last.get("value")
        out.append({
            "pod": b.get("key", ""),
            "count": b.get("doc_count", 0),
            "errors": (sub.get("errors") or {}).get("doc_count", 0),
            "app": _first("app"),
            "container": _first("container"),
            "namespace": _first("namespace"),
            "host": _first("host"),
            # ES 的 max 聚合返回秒级浮点（无值时为 -Infinity / None）
            "last_ms": int(last_ms * 1000) if isinstance(last_ms, (int, float))
                       and last_ms and last_ms > 0 else None,
        })
    return out


def parse_histogram(resp: dict, agg: str = "over_time") -> List[dict]:
    """解析时间直方图 → ``[{ts, count, errors}]``。"""
    node = ((resp or {}).get("aggregations") or {}).get(agg) or {}
    out = []
    for b in (node.get("buckets") or []):
        out.append({
            "ts": b.get("key_as_string") or "",
            "ts_ms": b.get("key"),
            "count": b.get("doc_count", 0),
            "errors": (b.get("errors") or {}).get("doc_count", 0),
        })
    return out


def total_hits(resp: dict) -> dict:
    """归一 ``hits.total``（ES7 可能是 int 或 {value, relation}）。"""
    t = ((resp or {}).get("hits") or {}).get("total") or 0
    if isinstance(t, dict):
        return {"value": t.get("value", 0), "relation": t.get("relation", "eq")}
    return {"value": t, "relation": "eq"}
