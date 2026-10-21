# -*- coding: utf-8 -*-
"""AI 数据桥（AI Bridge）——把应用各功能窗口的输出暴露为本地 HTTP 接口。

设计动机：AI 助手（Claude Code / Cursor 等）跑在同一台机器上，需要读取
应用各面板「已经拿到的数据」而不必重复调用底层 API（部分接口需要 token、
部分查询很慢）。前端各面板在数据更新时把**展示用的输出**上报到这里，
AI 侧按面板 key 拉取。

接口一览（全部只读除上报外，仅监听 127.0.0.1）：
  POST /api/ai/output/{tab_key}      面板上报输出（前端调用）
  GET  /api/ai/output                列出全部面板摘要（key/标题/更新时间/大小）
  GET  /api/ai/output/{tab_key}      取某面板最新输出（?format=text 拼纯文本）
  GET  /api/ai/output/{tab_key}/history  历史（最近 N 条，默认 10）
  POST /api/ai/input/{target}        （默认关闭）排队待用户确认插入的草稿

存储：进程内存环形缓冲（每 tab 最多 _MAX_PER_TAB 条、单条 _MAX_ENTRY_BYTES
截断），重启即清空 —— 这些本来就是可重新拉取的展示数据，不值得落盘。
"""
from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/ai")

# 面板 key → 中文/英文标题的权威表在前端 i18n；这里只放「AI 可读」的英文说明，
# 供 GET /api/ai/output 的消费者（AI）理解每个面板是干嘛的。
TAB_DESCRIPTIONS: Dict[str, str] = {
    "repo": "Repository list / selected repo",
    "diff": "Git diff output",
    "logs": "App activity log (pushLog stream)",
    "k8s": "K8s snapshot / resource output",
    "kibana": "Kibana search results",
    "cf": "Cloud-function log stream / query results",
    "cfsync": "CF consistency check results",
    "cfdebug": "CF debug (stack / variables / errors)",
    "clash": "Clash rules / probe output",
    "hcm": "HCM object browse / model detail",
    "hcmapi": "HCM API tester request+response",
    "hcmops": "HCM ops (redis keys / error list / decrypt)",
    "diagnose": "Unified diagnosis (CF + K8s)",
    "aibridge": "AI bridge self-description (docs)",
}

_MAX_PER_TAB = 20
_MAX_ENTRY_BYTES = 256 * 1024  # 单条 256KB 上限，超长截断（日志流防 OOM）
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_RETENTION_SEC = 2 * 3600      # 2 小时未更新且超容量的条目顺带清理

# tab_key -> deque[{ts, text, size, truncated}]，最新的在队尾
_STORE: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=_MAX_PER_TAB))
_TOTAL_BYTES = 0
_INPUT_TARGETS = {"k8s_yaml", "hcm_params", "kibana_query"}
_INPUT_MAX_PER_TARGET = 5
_INPUT_MAX_BYTES = 256 * 1024
_INPUT_ENABLED = False
_INPUT_STORE: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=_INPUT_MAX_PER_TARGET))


class OutputEntry(BaseModel):
    """前端上报的面板输出。

    text: 供 AI 阅读的文本（面板可直接把展示内容序列化成 markdown/JSON 字符串）；
    data: 可选，结构化原始数据（AI 优先读 text，需要精确字段时读 data）。
    """
    text: str = ""
    data: Optional[dict] = None
    meta: Optional[dict] = None


class InputDraft(BaseModel):
    text: str = Field(min_length=1, max_length=_INPUT_MAX_BYTES)
    title: str = Field(default="AI draft", max_length=120)


class InputSettings(BaseModel):
    enabled: bool


def _now() -> float:
    return time.time()


@router.post("/input/settings")
async def set_input_enabled(settings: InputSettings) -> dict:
    global _INPUT_ENABLED
    _INPUT_ENABLED = settings.enabled
    if not _INPUT_ENABLED:
        _INPUT_STORE.clear()
    return {"ok": True, "enabled": _INPUT_ENABLED}


@router.get("/input/pending")
async def get_pending_inputs() -> dict:
    drafts = [
        {**draft, "target": target}
        for target, entries in _INPUT_STORE.items()
        for draft in entries
    ]
    return {"ok": True, "enabled": _INPUT_ENABLED, "drafts": drafts}


@router.post("/input/{target}")
async def post_input(target: str, draft: InputDraft) -> dict:
    if target not in _INPUT_TARGETS:
        raise HTTPException(404, f"unknown input target: {target}")
    if not _INPUT_ENABLED:
        raise HTTPException(403, "AI input is disabled in the application")
    size = len(draft.text.encode("utf-8"))
    if size > _INPUT_MAX_BYTES:
        raise HTTPException(413, "draft exceeds the 256 KB limit")
    entries = _INPUT_STORE[target]
    draft_id = uuid.uuid4().hex
    if len(entries) >= _INPUT_MAX_PER_TARGET:
        entries.popleft()
    entries.append({
        "id": draft_id,
        "title": draft.title,
        "text": draft.text,
        "created_at": _now(),
        "size": size,
    })
    return {"ok": True, "id": draft_id, "target": target, "queued": len(entries)}


@router.delete("/input/{target}/{draft_id}")
async def remove_pending_input(target: str, draft_id: str) -> dict:
    if target not in _INPUT_TARGETS:
        raise HTTPException(404, f"unknown input target: {target}")
    entries = _INPUT_STORE.get(target)
    if not entries:
        raise HTTPException(404, "draft not found")
    retained = [draft for draft in entries if draft["id"] != draft_id]
    if len(retained) == len(entries):
        raise HTTPException(404, "draft not found")
    entries.clear()
    entries.extend(retained)
    return {"ok": True}


def _entry_size(text: str, data: Optional[dict], meta: dict) -> int:
    encoded = json.dumps(
        {"text": text, "data": data, "meta": meta},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded)


def _evict_expired(now: float) -> None:
    global _TOTAL_BYTES
    cutoff = now - _RETENTION_SEC
    for entries in _STORE.values():
        while entries and entries[0]["ts"] < cutoff:
            _TOTAL_BYTES -= entries.popleft()["size"]


def _evict_oldest() -> bool:
    global _TOTAL_BYTES
    candidates = [(entries[0]["ts"], key) for key, entries in _STORE.items() if entries]
    if not candidates:
        return False
    _, key = min(candidates)
    _TOTAL_BYTES -= _STORE[key].popleft()["size"]
    return True


@router.post("/output/{tab_key}")
async def post_output(tab_key: str, entry: OutputEntry) -> dict:
    global _TOTAL_BYTES
    if tab_key not in TAB_DESCRIPTIONS:
        raise HTTPException(404, f"unknown tab_key: {tab_key}")
    text = entry.text or ""
    meta = entry.meta or {}
    if not text and entry.data is None:
        raise HTTPException(400, "empty output (need text or data)")

    size = _entry_size(text, entry.data, meta)
    truncated = False
    if size > _MAX_ENTRY_BYTES:
        if not text:
            raise HTTPException(413, "structured output exceeds the 256 KB per-entry limit")
        suffix = f"\n…[truncated, original {len(text)} chars]"
        low, high = 0, len(text)
        fitted = None
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid] + suffix
            if _entry_size(candidate, entry.data, meta) <= _MAX_ENTRY_BYTES:
                fitted = candidate
                low = mid + 1
            else:
                high = mid - 1
        if fitted is None:
            raise HTTPException(
                413,
                "structured output leaves no room for text within the 256 KB per-entry limit",
            )
        text = fitted
        size = _entry_size(text, entry.data, meta)
        truncated = True

    now = _now()
    _evict_expired(now)
    entries = _STORE[tab_key]
    if len(entries) >= _MAX_PER_TAB:
        _TOTAL_BYTES -= entries.popleft()["size"]
    while _TOTAL_BYTES + size > _MAX_TOTAL_BYTES:
        if not _evict_oldest():
            break
    entries.append({
        "ts": now,
        "text": text,
        "data": entry.data,
        "meta": meta,
        "size": size,
        "truncated": truncated,
    })
    _TOTAL_BYTES += size
    return {
        "ok": True,
        "tab": tab_key,
        "kept": len(entries),
        "total_bytes": _TOTAL_BYTES,
        "truncated": truncated,
    }


@router.get("/output")
async def list_outputs() -> dict:
    out = []
    for key, desc in TAB_DESCRIPTIONS.items():
        dq = _STORE.get(key)
        out.append({
            "tab": key,
            "description": desc,
            "entries": len(dq) if dq else 0,
            "updated_at": dq[-1]["ts"] if dq else None,
            "age_sec": round(_now() - dq[-1]["ts"], 1) if dq else None,
            "latest_size": dq[-1]["size"] if dq else 0,
            "truncated": dq[-1]["truncated"] if dq else False,
        })
    return {"ok": True, "panels": out, "hint": "GET /api/ai/output/{tab} 读取面板输出"}


@router.get("/output/{tab_key}")
async def get_output(tab_key: str, format: str = "json", at: int = -1) -> dict:
    """at=-1 取最新；at=-2 取上一条……（负索引，与 Python deque 语义一致）"""
    if tab_key not in TAB_DESCRIPTIONS:
        raise HTTPException(404, f"unknown tab_key: {tab_key}")
    dq = _STORE.get(tab_key)
    if not dq:
        raise HTTPException(404, f"no output yet for tab: {tab_key}")
    try:
        entry = dq[at]
    except IndexError:
        raise HTTPException(404, f"history index out of range (have {len(dq)})")
    if format == "text":
        # 纯文本：方便 AI 直接 curl 读完
        txt = entry["text"]
        if entry["data"] is not None and not txt:
            txt = json.dumps(entry["data"], ensure_ascii=False, indent=2)
        return {"ok": True, "tab": tab_key, "ts": entry["ts"], "text": txt}
    return {"ok": True, "tab": tab_key, "ts": entry["ts"], "meta": entry["meta"],
            "size": entry["size"], "truncated": entry["truncated"],
            "text": entry["text"], "data": entry["data"]}


@router.get("/output/{tab_key}/history")
async def get_history(tab_key: str, limit: int = 10) -> dict:
    if tab_key not in TAB_DESCRIPTIONS:
        raise HTTPException(404, f"unknown tab_key: {tab_key}")
    dq = _STORE.get(tab_key)
    items = list(dq)[-max(1, min(limit, _MAX_PER_TAB)):]
    return {
        "ok": True, "tab": tab_key, "total": len(dq),
        "items": [{"ts": e["ts"], "size": e["size"], "truncated": e["truncated"],
                   "meta": e["meta"],
                   "preview": e["text"][:300] if e["text"] else json.dumps(
                       e["data"], ensure_ascii=False)[:300] if e["data"] else ""}
                  for e in items],
    }
