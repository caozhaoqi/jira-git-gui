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

存储：进程内存环形缓冲（每 tab 最多 _MAX_PER_TAB 条、单条 _MAX_ENTRY_BYTES
截断），重启即清空 —— 这些本来就是可重新拉取的展示数据，不值得落盘。
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
_RETENTION_SEC = 2 * 3600      # 2 小时未更新且超容量的条目顺带清理

# tab_key -> deque[{ts, text, size, truncated}]，最新的在队尾
_STORE: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=_MAX_PER_TAB))


class OutputEntry(BaseModel):
    """前端上报的面板输出。

    text: 供 AI 阅读的文本（面板可直接把展示内容序列化成 markdown/JSON 字符串）；
    data: 可选，结构化原始数据（AI 优先读 text，需要精确字段时读 data）。
    """
    text: str = ""
    data: Optional[dict] = None
    meta: Optional[dict] = None


def _now() -> float:
    return time.time()


def _clip(text: str) -> tuple[str, bool]:
    if len(text.encode("utf-8", "ignore")) <= _MAX_ENTRY_BYTES:
        return text, False
    # 按字符截到安全长度（UTF-8 每字符最多 4 字节，取 1/4 上限再留余量）
    limit = _MAX_ENTRY_BYTES // 4 - 100
    return text[:limit] + f"\n…[truncated, original {len(text)} chars]", True


@router.post("/output/{tab_key}")
async def post_output(tab_key: str, entry: OutputEntry) -> dict:
    if tab_key not in TAB_DESCRIPTIONS:
        raise HTTPException(404, f"unknown tab_key: {tab_key}")
    text, truncated = _clip(entry.text or "")
    if not text and entry.data is None:
        raise HTTPException(400, "empty output (need text or data)")
    _STORE[tab_key].append({
        "ts": _now(),
        "text": text,
        "data": entry.data,
        "meta": entry.meta or {},
        "size": len(text.encode("utf-8", "ignore")),
        "truncated": truncated,
    })
    return {"ok": True, "tab": tab_key, "kept": len(_STORE[tab_key])}


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
