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


def test_oversized_structured_data_is_rejected():
    c = make_client()
    r = c.post("/api/ai/output/cf", json={"data": {"blob": "x" * (300 * 1024)}})
    assert r.status_code == 413


def test_global_memory_cap_evicts_oldest_entry(monkeypatch):
    mod = importlib.import_module("api.ai_bridge")
    mod._STORE.clear()
    mod._TOTAL_BYTES = 0
    monkeypatch.setattr(mod, "_MAX_TOTAL_BYTES", 100)
    c = make_client()

    first = c.post("/api/ai/output/cf", json={"text": "a" * 40})
    second = c.post("/api/ai/output/logs", json={"text": "b" * 40})
    assert first.status_code == second.status_code == 200
    assert second.json()["total_bytes"] <= 100
    assert c.get("/api/ai/output/cf").status_code == 404
    assert c.get("/api/ai/output/logs").status_code == 200


def test_inbound_drafts_require_opt_in_and_user_acknowledgement(monkeypatch):
    mod = importlib.import_module("api.ai_bridge")
    monkeypatch.setattr(mod, "_INPUT_ENABLED", False)
    mod._INPUT_STORE.clear()
    c = make_client()

    payload = {"title": "yaml draft", "text": "apiVersion: v1\nkind: ConfigMap"}
    assert c.post("/api/ai/input/k8s_yaml", json=payload).status_code == 403
    assert c.post("/api/ai/input/settings", json={"enabled": True}).json()["enabled"] is True

    queued = c.post("/api/ai/input/k8s_yaml", json=payload)
    assert queued.status_code == 200
    draft_id = queued.json()["id"]
    pending = c.get("/api/ai/input/pending").json()
    assert pending["enabled"] is True
    assert pending["drafts"][0]["text"] == payload["text"]
    assert pending["drafts"][0]["target"] == "k8s_yaml"

    assert c.delete(f"/api/ai/input/k8s_yaml/{draft_id}").json()["ok"] is True
    assert c.get("/api/ai/input/pending").json()["drafts"] == []

    c.post("/api/ai/input/k8s_yaml", json=payload)
    assert c.post("/api/ai/input/settings", json={"enabled": False}).json()["enabled"] is False
    assert c.get("/api/ai/input/pending").json()["drafts"] == []


def test_inbound_drafts_validate_target_and_utf8_size(monkeypatch):
    mod = importlib.import_module("api.ai_bridge")
    monkeypatch.setattr(mod, "_INPUT_ENABLED", True)
    mod._INPUT_STORE.clear()
    c = make_client()
    assert c.post("/api/ai/input/unknown", json={"text": "x"}).status_code == 404
    assert c.post("/api/ai/input/k8s_yaml", json={"text": "中" * 90000}).status_code == 413
