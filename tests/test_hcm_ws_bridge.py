# -*- coding: utf-8 -*-
"""HCM 网关 WS 调试通道测试（api/hcm/ws_bridge.py + 路由）。

协议基准（现网实测）：
- URL：ws(s)://<host><前缀>/ws/<api>；握手 Cookie: token=...
- 首帧发明文业务参数 JSON；回包 {"status": "notify"|"success"|"error", "data": ...}
- notify 可多条（data=="pong" 为心跳）；success/error 为终态
- token 失效首包即 {"status":"error","data":"请先登录"}

mock 网关用 websockets.serve 起本地 server，按上述协议回包。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest
import websockets
from api.auth import API_TOKEN

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# --------------------------------------------------------------------------- #
#  mock 网关 WS server
# --------------------------------------------------------------------------- #
class MockGateway:
    """按真实协议回包的 mock 网关。

    script：收到首帧后依次 send 的消息列表（dict 会 JSON 序列化，str 原文直发）。
    expect_token：若给出，断言握手 Cookie token 等于它（用于自动重登断言）。
    """

    def __init__(self, script, expect_token: str | None = None):
        self.script = script
        self.expect_token = expect_token
        self.received_params = None
        self.port = None
        self._loop = None
        self._thread = None
        self._server = None
        self.connections = 0

    async def _handler(self, ws):
        self.connections += 1
        if self.expect_token is not None:
            cookie = ws.request.headers.get("Cookie", "")
            assert f"token={self.expect_token}" in cookie, f"cookie mismatch: {cookie!r}"
        try:
            self.received_params = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        except Exception:
            self.received_params = None
        for item in self.script:
            payload = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            await ws.send(payload)
        await ws.close()

    def start(self):
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def serve():
                self._server = await websockets.serve(
                    self._handler, "127.0.0.1", 0, ping_interval=None)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()  # 常驻

            self._loop.run_until_complete(serve())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5), "mock gateway failed to start"
        return self

    def stop(self):
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def target(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture()
def gw_factory():
    made = []

    def make(script, expect_token=None):
        g = MockGateway(script, expect_token).start()
        made.append(g)
        return g

    yield make
    for g in made:
        g.stop()


# --------------------------------------------------------------------------- #
#  build_ws_url
# --------------------------------------------------------------------------- #
def test_build_ws_url_variants():
    from api.hcm.ws_bridge import build_ws_url
    assert build_ws_url("http://10.1.38.184", "hcm.model.list") == \
        "ws://10.1.38.184/ws/hcm.model.list"
    assert build_ws_url("https://bhcdhcm.tjbhcd.com:5206", "hcm.paas.object.list") == \
        "wss://bhcdhcm.tjbhcd.com:5206/ws/hcm.paas.object.list"
    # 带路径前缀的目标保留前缀（网关 hcm_wengen 机制）
    assert build_ws_url("https://gw.example.com/hcm/", "x.y") == \
        "wss://gw.example.com/hcm/ws/x.y"
    with pytest.raises(ValueError):
        build_ws_url("", "x.y")
    with pytest.raises(ValueError):
        build_ws_url("http://x", "")


# --------------------------------------------------------------------------- #
#  ws_call：成功 / 错误 / 非 JSON / 登录重登
# --------------------------------------------------------------------------- #
def test_ws_call_success_with_notify(gw_factory, monkeypatch):
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "tok-preset")
    gw = gw_factory([
        {"status": "notify", "data": "step 1/3"},
        {"status": "notify", "data": "pong"},          # 心跳应被过滤
        {"status": "notify", "data": "step 2/3"},
        {"status": "success", "data": {"list": [1, 2], "count": 2}},
    ])
    r = asyncio.run(ws_bridge.ws_call(
        gw.target, "hcm.paas.object.list", {"page_index": 1, "page_size": 2}, ""))
    assert r["ok"] is True
    assert r["status"] == "success"
    assert r["data"] == {"list": [1, 2], "count": 2}
    assert r["messages"] == ["step 1/3", "step 2/3"]   # pong 已过滤
    assert isinstance(r["duration_ms"], int)
    assert r["gateway"] == gw.target
    # 首帧 = 明文业务参数（无加密信封）
    assert gw.received_params == {"page_index": 1, "page_size": 2}


def test_ws_call_error_terminal(gw_factory, monkeypatch):
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "tok-preset")
    gw = gw_factory([{"status": "error", "data": "list() got an unexpected keyword argument 'notify'"}])
    r = asyncio.run(ws_bridge.ws_call(gw.target, "hcm.model.list", {}, ""))
    assert r["ok"] is False
    assert r["status"] == "error"
    assert "notify" in r["data"]


def test_ws_call_non_json_frame(gw_factory, monkeypatch):
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "tok-preset")
    gw = gw_factory(["<html>gateway 500</html>"])
    r = asyncio.run(ws_bridge.ws_call(gw.target, "x.y", {}, ""))
    assert r["status"] == "error"
    assert r["data"] == "<html>gateway 500</html>"


def test_ws_call_login_error_auto_relogin(gw_factory, monkeypatch):
    """token 失效（请先登录）→ 用 cf_accounts 自动重登后带新 token 重试一次。"""
    from api.hcm import ws_bridge

    calls = {"n": 0}

    class TwoPhase(MockGateway):
        async def _handler(self, ws):
            self.connections += 1
            cookie = ws.request.headers.get("Cookie", "")
            await ws.recv()
            calls["n"] += 1
            if calls["n"] == 1:
                assert "token=old-tok" in cookie
                await ws.send(json.dumps({"status": "error", "data": "请先登录"}, ensure_ascii=False))
                await ws.close()
            else:
                assert "token=new-tok" in cookie, cookie
                await ws.send(json.dumps({"status": "success", "data": {"ok": 1}}, ensure_ascii=False))
                await ws.close()

    gw = TwoPhase([]).start()
    try:
        monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "old-tok")
        async def fake_relogin(target_url):
            assert target_url == gw.target
            return "new-tok"
        monkeypatch.setattr(ws_bridge, "_auto_relogin_token", fake_relogin)
        r = asyncio.run(ws_bridge.ws_call(gw.target, "hcm.paas.object.list", {"a": 1}, ""))
        assert r["ok"] is True and r["data"] == {"ok": 1}
        assert calls["n"] == 2
    finally:
        gw.stop()


def test_ws_call_no_token(monkeypatch):
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "")
    with pytest.raises(PermissionError):
        asyncio.run(ws_bridge.ws_call("http://127.0.0.1:1", "x.y", {}, ""))


def test_ws_call_login_error_sys_err_auto_relogin(gw_factory, monkeypatch):
    """bhcd 兜底码 system_errors_msg_00000055 同样触发自动重登（现网实测：
    外来 token 首包即此码，带有效 token 同参数 success）。"""
    from api.hcm import ws_bridge

    calls = {"n": 0}

    class TwoPhase(MockGateway):
        async def _handler(self, ws):
            self.connections += 1
            cookie = ws.request.headers.get("Cookie", "")
            await ws.recv()
            calls["n"] += 1
            if calls["n"] == 1:
                assert "token=old-tok" in cookie
                await ws.send(json.dumps(
                    {"status": "error", "data": "system_errors_msg_00000055"},
                    ensure_ascii=False))
                await ws.close()
            else:
                assert "token=new-tok" in cookie, cookie
                await ws.send(json.dumps(
                    {"status": "success", "data": {"count": 3288}}, ensure_ascii=False))
                await ws.close()

    gw = TwoPhase([]).start()
    try:
        monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "old-tok")

        async def fake_relogin(target_url):
            return "new-tok"

        monkeypatch.setattr(ws_bridge, "_auto_relogin_token", fake_relogin)
        r = asyncio.run(ws_bridge.ws_call(gw.target, "hcm.paas.object.list", {"a": 1}, ""))
        assert r["ok"] is True and r["data"] == {"count": 3288}
        assert calls["n"] == 2
    finally:
        gw.stop()


# --------------------------------------------------------------------------- #
#  路由：POST /api/hcm/ws-call
# --------------------------------------------------------------------------- #
def test_route_ws_call(gw_factory, monkeypatch):
    from fastapi.testclient import TestClient
    import api.server  # noqa: F401  （注册全部路由）
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "tok-preset")
    gw = gw_factory([{"status": "success", "data": {"count": 7}}])
    with TestClient(api.server.app, headers={"X-API-Token": API_TOKEN}) as client:
        r = client.post("/api/hcm/ws-call", json={
            "api_name": "hcm.model.count",
            "params": {"model": "Employee"},
            "target": gw.target,
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["data"] == {"count": 7}


def test_route_ws_call_missing_api(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server  # noqa: F401
    with TestClient(api.server.app, headers={"X-API-Token": API_TOKEN}) as client:
        r = client.post("/api/hcm/ws-call", json={"api_name": ""})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "api_name" in r.json()["error"]


# --------------------------------------------------------------------------- #
#  路由：WS /api/hcm/ws 流式代理
# --------------------------------------------------------------------------- #
def test_route_ws_proxy(gw_factory, monkeypatch):
    from fastapi.testclient import TestClient
    import api.server  # noqa: F401
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "tok-preset")
    gw = gw_factory([
        {"status": "notify", "data": "working"},
        {"status": "success", "data": {"list": []}},
    ])
    with TestClient(api.server.app, headers={"X-API-Token": API_TOKEN}) as client:
        with client.websocket_connect(
                f"/api/hcm/ws?api=hcm.paas.object.list&target={gw.target}") as ws:
            ws.send_text(json.dumps({"page_index": 1}))
            m1 = json.loads(ws.receive_text())
            m2 = json.loads(ws.receive_text())
    assert m1["status"] == "notify" and m1["data"] == "working"
    assert m2["status"] == "success" and m2["data"] == {"list": []}
    assert gw.received_params == {"page_index": 1}


def test_route_ws_proxy_login_error_rebuild(gw_factory, monkeypatch):
    """流式代理：预设 token 失效（system_errors_msg_00000055）→ 自动重登
    重建连接重发首帧，本地客户端无感知（只见终态 success）。"""
    from fastapi.testclient import TestClient
    import api.server  # noqa: F401
    from api.hcm import ws_bridge

    calls = {"n": 0}

    class TwoPhase(MockGateway):
        async def _handler(self, ws):
            self.connections += 1
            cookie = ws.request.headers.get("Cookie", "")
            self.received_params = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            calls["n"] += 1
            if calls["n"] == 1:
                assert "token=old-tok" in cookie
                await ws.send(json.dumps(
                    {"status": "error", "data": "system_errors_msg_00000055"},
                    ensure_ascii=False))
                await ws.close()
            else:
                assert "token=new-tok" in cookie, cookie
                await ws.send(json.dumps(
                    {"status": "success", "data": {"count": 3288}}, ensure_ascii=False))
                await ws.close()

    gw = TwoPhase([]).start()
    try:
        monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "old-tok")

        async def fake_relogin(target_url):
            assert target_url == gw.target
            return "new-tok"

        monkeypatch.setattr(ws_bridge, "_auto_relogin_token", fake_relogin)
        with TestClient(api.server.app, headers={"X-API-Token": API_TOKEN}) as client:
            with client.websocket_connect(
                    f"/api/hcm/ws?api=hcm.paas.object.list&target={gw.target}") as ws:
                ws.send_text(json.dumps({"page_index": 1}))
                # 登录错误帧不透传：本地收到的第一条就是终态 success
                m = json.loads(ws.receive_text())
        assert m["status"] == "success" and m["data"] == {"count": 3288}
        assert calls["n"] == 2
        # 重建后首帧原样重发（业务参数不变）
        assert gw.received_params == {"page_index": 1}
    finally:
        gw.stop()


def test_route_ws_proxy_login_error_no_relogin(gw_factory, monkeypatch):
    """重登不可用（cf_accounts 无账密）→ 登录错误帧透传给本地后关闭。"""
    from fastapi.testclient import TestClient
    import api.server  # noqa: F401
    from api.hcm import ws_bridge
    monkeypatch.setattr(ws_bridge, "_HCM_PRESET_TOKEN", "old-tok")

    async def fake_relogin(target_url):
        return ""  # 无账密/重登失败

    monkeypatch.setattr(ws_bridge, "_auto_relogin_token", fake_relogin)
    gw = gw_factory([{"status": "error", "data": "请先登录"}])
    with TestClient(api.server.app, headers={"X-API-Token": API_TOKEN}) as client:
        with client.websocket_connect(
                f"/api/hcm/ws?api=x.y&target={gw.target}") as ws:
            ws.send_text(json.dumps({}))
            m = json.loads(ws.receive_text())
    assert m["status"] == "error" and m["data"] == "请先登录"
