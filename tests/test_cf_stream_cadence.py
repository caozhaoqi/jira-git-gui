# -*- coding: utf-8 -*-
"""CF 实时刷新流的「周期调度」与「客户端基线播种」不变量（离线）。

背景：用户反馈 CF 日志「实时刷新」延时明显、更新不及时。根因有两条：

1. **周期被查询耗时撑大**：旧循环是 ``查询 → sleep(interval)`` 串行，
   实际周期 = 「查询耗时 + interval」（实测单次查询 0.3~2.2s，5s 间隔被撑到
   5.3~7.2s，且网关越慢漂移越明显）；
2. **首条新日志要等两个周期**：旧实现第一轮是「静默种子」（只记基线不推送），
   而前端开启流前已经手动查过一次基线 —— 那一轮纯属白等。

这里固化修复后的约定：

1. 轮询按**截止时间**调度（只睡剩余时间），周期稳定在 ``interval``；
2. 客户端把基线行 key 经 ``seen_keys`` 传入时，首轮轮询即可判定并推送新行；
3. 没有 ``seen_keys``（旧客户端 / 多页查询）时仍退回静默种子语义，不破坏兼容；
4. ``seen_keys`` 只透传给轮询任务，不进入 ``stream_status()`` 的对外字段。
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api.cf.cf_stream as st

_CFG = {
    "server_url": "http://stream-unit-test",
    "token": "",
    "proxy": "",
    "log_type": "want_fn",
    "record_model": "dynamic_log",
    "page_size": 100,
}


def _rows(*ids):
    return [
        {"id": i, "log_type": "want_fn",
         "create_time": f"2026-09-20 10:00:0{i}", "content": f"c{i}"}
        for i in ids
    ]


# --------------------------------------------------------------------------- #
#  1. 周期调度：interval 而不是「查询耗时 + interval」
# --------------------------------------------------------------------------- #
def test_cadence_is_interval_not_query_plus_interval():
    """慢网关下周期仍应稳定在 interval（旧实现会被查询耗时撑大近一倍）。"""
    stamps = []
    query_cost = 0.4
    interval = 0.5

    async def fake_query(req):
        stamps.append(asyncio.get_running_loop().time())
        await asyncio.sleep(query_cost)  # 模拟慢网关（实测 0.3~2.2s）
        return {"data": {"list": [], "count": 0}}

    async def run():
        st.cf_query_logs = fake_query
        st.broadcast = lambda ev, payload: None
        task = asyncio.create_task(st._stream_loop(dict(_CFG), interval))
        await asyncio.sleep(2.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(run())
    finally:
        pass

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # 旧实现：0 + (0.4 + 0.5) → 周期 0.9s，2.6s 窗口只有 3 次轮询
    # 新实现：周期 0.5s，2.6s 窗口约 6 次轮询
    assert len(stamps) >= 5, f"轮询次数过少（周期被查询耗时撑大？）: {stamps}"
    assert gaps, "至少应有两次轮询才能算周期"
    assert max(gaps) < 0.85, f"周期超过 interval+查询耗时的一半，疑似回退: {gaps}"
    assert sum(gaps) / len(gaps) < 0.7, f"平均周期偏离 interval: {gaps}"


# --------------------------------------------------------------------------- #
#  2. 客户端基线播种：首轮即推新行
# --------------------------------------------------------------------------- #
def test_client_baseline_seed_pushes_new_rows_on_first_poll():
    """传入 seen_keys：第一个事件就带新行，且不出静默种子事件。"""
    events = []
    calls = {"n": 0}

    async def fake_query(req):
        calls["n"] += 1
        # 第一轮起就有一条新日志（id=3）出现在第 1 页最上方
        rows = _rows(3, 1, 2)
        return {"data": {"list": rows, "count": len(rows)}}

    async def run():
        st.cf_query_logs = fake_query
        st.broadcast = lambda ev, payload: events.append(payload)
        task = asyncio.create_task(
            st._stream_loop(dict(_CFG), 30, "", ["id:1", "id:2"])
        )
        await asyncio.sleep(0.15)  # 只够跑第一轮
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    assert calls["n"] >= 1, "未触发轮询"
    assert events, "未收到任何推送"
    first = events[0]
    assert not first.get("seeded"), "传入基线后首轮不应再出静默种子（白等一个周期）"
    assert [r.get("id") for r in first["rows"]] == [3], first
    assert first.get("new_count") == 1, first


def test_baseline_seed_dedups_client_rows():
    """基线里的行不得被当成新行重复推送。"""
    events = []

    async def fake_query(req):
        return {"data": {"list": _rows(1, 2), "count": 2}}

    async def run():
        st.cf_query_logs = fake_query
        st.broadcast = lambda ev, payload: events.append(payload)
        task = asyncio.create_task(
            st._stream_loop(dict(_CFG), 30, "", ["id:1", "id:2"])
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    pushed = [r.get("id") for p in events for r in p.get("rows") or []]
    assert pushed == [], f"基线行被误判为新行: {pushed}"
    assert events and events[0].get("new_count") == 0, events


# --------------------------------------------------------------------------- #
#  3. 兼容：无 seen_keys 仍走静默种子
# --------------------------------------------------------------------------- #
def test_no_baseline_still_seeds_silently():
    """不传 seen_keys（旧客户端 / 多页查询）时保持静默种子语义。"""
    events = []
    calls = {"n": 0}

    async def fake_query(req):
        calls["n"] += 1
        rows = _rows(1, 2)
        if calls["n"] >= 2:
            rows = _rows(3, 1, 2)
        return {"data": {"list": rows, "count": len(rows)}}

    async def run():
        st.cf_query_logs = fake_query
        st.broadcast = lambda ev, payload: events.append(payload)
        task = asyncio.create_task(st._stream_loop(dict(_CFG), 30, ""))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    assert events and events[0].get("seeded") is True, events
    assert events[0]["rows"] == [], "静默种子不得推送任何行"


# --------------------------------------------------------------------------- #
#  4. start_stream：透传 + 过滤 seen_keys
# --------------------------------------------------------------------------- #
def test_start_stream_forwards_and_filters_seen_keys():
    """seen_keys 透传给轮询任务；非字符串条目被过滤；不进对外返回/status。"""
    captured = {}

    async def fake_loop(cfg, interval, key="", seen_keys=None):
        captured["seen_keys"] = seen_keys
        captured["cfg"] = cfg

    async def run():
        orig = st._stream_loop
        st._stream_loop = fake_loop
        try:
            res = await st.start_stream(
                "http://seed-forward", seen_keys=["id:1", "", None, 7, "id:2"],
            )
            await asyncio.sleep(0)  # 让 create_task 的任务跑一步
            await st.stop_stream("http://seed-forward")
            return res
        finally:
            st._stream_loop = orig

    res = asyncio.run(run())

    assert captured.get("seen_keys") == ["id:1", "id:2"], captured
    # 对外契约：seen_keys 是内部播种数据，不得出现在返回 / stream_status 里
    assert "seen_keys" not in res, res
    assert "seen_keys" not in st.stream_status(), st.stream_status()


def test_start_stream_defaults_to_silent_seed():
    """不传 seen_keys 时轮询任务收到 None（静默种子）。"""
    captured = {}

    async def fake_loop(cfg, interval, key="", seen_keys=None):
        captured["seen_keys"] = seen_keys

    async def run():
        orig = st._stream_loop
        st._stream_loop = fake_loop
        try:
            await st.start_stream("http://seed-default")
            await asyncio.sleep(0)
            await st.stop_stream("http://seed-default")
        finally:
            st._stream_loop = orig

    asyncio.run(run())
    assert captured.get("seen_keys") in (None, []), captured


# --------------------------------------------------------------------------- #
#  5. 路由层：POST /api/cf/logs/stream 把 seen_keys 带给 cf_log_stream_start
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from api.auth import API_TOKEN
    import api.server  # noqa: F401  —— 必须 import server 才有全部路由
    from api.common import app

    async def _no_autologin(*a, **kw):
        return []
    monkeypatch.setattr("api.server.cf_autologin_all", _no_autologin)

    with TestClient(
        app,
        headers={"X-API-Token": API_TOKEN},
        raise_server_exceptions=False,
    ) as c:
        yield c


def test_route_passes_seen_keys(client, monkeypatch):
    import api.cf.routes_cf as routes_cf

    captured = {}

    async def fake_start(**kw):
        captured.update(kw)
        return {"ok": True, "streaming": True}

    monkeypatch.setattr(routes_cf, "cf_log_stream_start", fake_start)

    r = client.post("/api/cf/logs/stream", json={
        "action": "start",
        "server_url": "https://sdsy.hcmcloud.cn",
        "interval": 7,
        "seen_keys": ["id:11", "id:12"],
    })
    assert r.status_code == 200, r.text
    assert captured.get("seen_keys") == ["id:11", "id:12"], captured
    assert captured.get("interval") == 7, captured
    assert captured.get("server_url") == "https://sdsy.hcmcloud.cn", captured


def test_route_seen_keys_optional(client, monkeypatch):
    """旧客户端不带 seen_keys 也不能 422（向后兼容）。"""
    import api.cf.routes_cf as routes_cf

    captured = {}

    async def fake_start(**kw):
        captured.update(kw)
        return {"ok": True, "streaming": True}

    monkeypatch.setattr(routes_cf, "cf_log_stream_start", fake_start)

    r = client.post("/api/cf/logs/stream", json={
        "action": "start",
        "server_url": "https://sdsy.hcmcloud.cn",
    })
    assert r.status_code == 200, r.text
    assert captured.get("seen_keys") == [], captured
