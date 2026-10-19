# -*- coding: utf-8 -*-
"""AI 数据桥接口测试（api/ai_bridge.py）。

直接构造 FastAPI TestClient（httpx），不依赖运行中的 8787 服务。
"""
import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_client() -> TestClient:
    mod = importlib.import_module("api.ai_bridge")
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def test_post_and_get_roundtrip():
    c = make_client()
    r = c.post("/api/ai/output/hcmapi", json={"text": '{"result": 1}', "meta": {"api": "hcm.model.list"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = c.get("/api/ai/output/hcmapi")
    body = r.json()
    assert body["ok"] and body["text"] == '{"result": 1}'
    assert body["meta"] == {"api": "hcm.model.list"}


def test_text_format():
    c = make_client()
    c.post("/api/ai/output/cf", json={"text": "line1\nline2"})
    r = c.get("/api/ai/output/cf?format=text")
    assert r.json()["text"] == "line1\nline2"


def test_data_only_entry():
    c = make_client()
    c.post("/api/ai/output/hcmops", json={"data": {"keys": ["a", "b"]}})
    r = c.get("/api/ai/output/hcmops?format=text")
    assert '"keys"' in r.json()["text"]


def test_unknown_tab_rejected():
    c = make_client()
    assert c.post("/api/ai/output/nope", json={"text": "x"}).status_code == 404
    assert c.get("/api/ai/output/nope").status_code == 404


def test_empty_rejected():
    c = make_client()
    assert c.post("/api/ai/output/cf", json={"text": ""}).status_code == 400


def test_list_and_history():
    c = make_client()
    c.post("/api/ai/output/logs", json={"text": "m1"})
    c.post("/api/ai/output/logs", json={"text": "m2"})
    lst = c.get("/api/ai/output").json()
    panels = {p["tab"]: p for p in lst["panels"]}
    assert panels["logs"]["entries"] == 2
    hist = c.get("/api/ai/output/logs/history?limit=5").json()
    assert hist["total"] == 2
    assert hist["items"][-1]["preview"] == "m2"


def test_negative_index_and_range():
    c = make_client()
    c.post("/api/ai/output/cf", json={"text": "old"})
    c.post("/api/ai/output/cf", json={"text": "new"})
    assert c.get("/api/ai/output/cf?at=-2").json()["text"] == "old"
    assert c.get("/api/ai/output/cf?at=-9").status_code == 404


def test_truncation():
    c = make_client()
    big = "x" * (300 * 1024)
    r = c.post("/api/ai/output/cf", json={"text": big})
    assert r.json()["ok"]
    body = c.get("/api/ai/output/cf").json()
    assert body["truncated"] is True
    assert "truncated" in body["text"]


def test_ring_buffer_maxlen():
    c = make_client()
    for i in range(25):
        c.post("/api/ai/output/cf", json={"text": f"m{i}"})
    assert c.get("/api/ai/output/cf").json()["kept" if False else "text"] == "m24"
    hist = c.get("/api/ai/output/cf/history?limit=50").json()
    assert hist["total"] == 20  # deque maxlen
