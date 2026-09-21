# -*- coding: utf-8 -*-
"""CF 实时刷新流的「类型过滤」不变量（离线）。

背景：前端「实时刷新」（SSE，cf_log_update）曾在开启时把过滤条件快照进后端流；
若开启时 log_type 为空/旧值，流会把其它类型日志推送并合并进列表，表现为
「自动刷新让日志类型过滤失效」。这里固化后端侧的根因约定：

1. 流每轮轮询都必须把 log_type 透传给 cf_query_logs（服务端据此 filter_dict 过滤）；
2. 类型字段按记录模型映射（dynamic_log→log_type，SyncOuterRecord→name）；
3. 首轮只做静默种子（不推送），增量轮只推送新行。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api.cf.cf_stream as st
from api.cf.cf_logs import _model_type_field


def test_model_type_field_mapping():
    assert _model_type_field("dynamic_log") == "log_type"
    assert _model_type_field("SyncOuterRecord") == "name"
    assert _model_type_field("") == "log_type"  # 未知模型兜底


def test_stream_passes_log_type_to_query():
    """流每轮轮询都要带上 log_type，保证服务端按类型过滤。"""
    captured = []

    async def fake_query(req):
        captured.append({
            "log_type": getattr(req, "log_type", "<MISSING>"),
            "record_model": getattr(req, "record_model", "<MISSING>"),
            "page_index": getattr(req, "page_index", "<MISSING>"),
        })
        rows = [
            {"id": 1, "log_type": "want_fn", "create_time": "2026-09-20 10:00:00", "content": "a"},
            {"id": 2, "log_type": "other_fn", "create_time": "2026-09-20 10:00:01", "content": "b"},
        ]
        return {"data": {"list": rows, "count": len(rows)}}

    async def run():
        st.cf_query_logs = fake_query
        task = asyncio.create_task(st._stream_loop(
            {"server_url": "http://x", "token": "", "proxy": "",
             "log_type": "want_fn", "record_model": "dynamic_log", "page_size": 100},
            3,
        ))
        await asyncio.sleep(0.05)  # 至少跑完首轮
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert captured, "流未触发任何轮询"
    assert all(c["log_type"] == "want_fn" for c in captured), captured
    assert all(c["page_index"] == 1 for c in captured), captured


def test_stream_seeds_silently_then_pushes_only_new():
    """首轮静默种子（不推送），增量轮只推新行（旧行不重复推送）。"""
    events = []
    orig_broadcast = st.broadcast
    st.broadcast = lambda ev, payload: events.append((ev, payload))
    calls = {"n": 0}

    async def fake_query(req):
        calls["n"] += 1
        # 第一轮给两行，第二轮追加一行（模拟新日志出现）
        rows = [
            {"id": 1, "log_type": "want_fn", "create_time": "2026-09-20 10:00:00", "content": "a"},
            {"id": 2, "log_type": "want_fn", "create_time": "2026-09-20 10:00:01", "content": "b"},
        ]
        if calls["n"] >= 2:
            rows = [{"id": 3, "log_type": "want_fn", "create_time": "2026-09-20 10:00:02", "content": "c"}] + rows
        return {"data": {"list": rows, "count": len(rows)}}

    async def run():
        st.cf_query_logs = fake_query
        task = asyncio.create_task(st._stream_loop(
            {"server_url": "http://x", "token": "", "proxy": "",
             "log_type": "want_fn", "record_model": "dynamic_log", "page_size": 100},
            1,
        ))
        await asyncio.sleep(1.2)  # 覆盖两轮（interval=1s）
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run())
    finally:
        st.broadcast = orig_broadcast

    seed = [p for _, p in events if p.get("seeded")]
    assert seed and seed[0]["rows"] == [], "首轮应为静默种子"
    increments = [p for _, p in events if not p.get("seeded") and "rows" in p]
    assert increments, "未收到增量推送"
    # 增量轮只应推送新行（id=3），旧行（1/2）已被首轮种子去重
    pushed_ids = [r.get("id") for p in increments for r in p["rows"]]
    assert 3 in pushed_ids
    assert 1 not in pushed_ids and 2 not in pushed_ids, pushed_ids
