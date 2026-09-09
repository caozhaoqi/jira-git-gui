# -*- coding: utf-8 -*-
"""云函数日志查询：记录模型（record_model）UI 可配置 —— 透传单测。

验证 cf_query_logs 把 req.record_model 写进请求体 model 字段，
默认 dynamic_log 保持向后兼容，支持 SyncOuterRecord 等任意模型名。

项目 venv 未装 pytest-asyncio，沿用 regression 套件写法：同步函数内 asyncio.run。
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

import api.cf.cf_logs as cf_logs_mod


def _fake_payload():
    record = {"id_": 1, "content": "x"}
    return {"success": True, "result": {"list": [record], "total": 1}}


class _FakeResp:
    status_code = 200

    def json(self):
        return _fake_payload()


class _FakeClient:
    captured = {}

    def __init__(self, **kw):
        self.kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.captured = {"url": url, "json": json, "headers": headers}
        return _FakeResp()


def _make_req(**over):
    base = dict(
        server_url="https://cf.example.com",
        token="dummy-token",
        log_type="",
        record_model="dynamic_log",
        page_index=1,
        page_size=200,
        proxy="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _assert_ok(res):
    # 成功时返回内部结果（含 method/raw/data），无异常即代表查询成功
    assert res.get("method") in ("cookie", "bearer_hcminner", "header_token")


def test_type_field_mapping_per_model():
    """不同记录模型的「类型/描述」字段名不同（SyncOuterRecord 没有 log_type，用 name）。"""
    assert cf_logs_mod._model_type_field("dynamic_log") == "log_type"
    assert cf_logs_mod._model_type_field("SyncOuterRecord") == "name"
    assert cf_logs_mod._model_type_field("syncouterrecord") == "name"   # 大小写不敏感
    assert cf_logs_mod._model_type_field("") == "log_type"              # 兜底
    assert cf_logs_mod._model_type_field("unknown_model") == "log_type"


def test_filter_uses_model_specific_type_field(monkeypatch):
    """SyncOuterRecord 用 log_type 过滤会查不到，必须落到 name 字段。"""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    res = asyncio.run(cf_logs_mod.cf_query_logs(
        _make_req(record_model="SyncOuterRecord", log_type="员工同步")
    ))
    _assert_ok(res)
    assert _FakeClient.captured["json"]["model"] == "SyncOuterRecord"
    assert _FakeClient.captured["json"]["filter_dict"] == {"name": "员工同步"}


def test_filter_dynamic_log_uses_log_type(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    res = asyncio.run(cf_logs_mod.cf_query_logs(
        _make_req(record_model="dynamic_log", log_type="salary_x")
    ))
    _assert_ok(res)
    assert _FakeClient.captured["json"]["filter_dict"] == {"log_type": "salary_x"}


def test_record_model_default_dynamic_log(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    res = asyncio.run(cf_logs_mod.cf_query_logs(_make_req(record_model="")))
    _assert_ok(res)
    assert _FakeClient.captured["json"]["model"] == "dynamic_log"


def test_record_model_sync_outer_record(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    res = asyncio.run(cf_logs_mod.cf_query_logs(_make_req(record_model="SyncOuterRecord")))
    _assert_ok(res)
    assert _FakeClient.captured["json"]["model"] == "SyncOuterRecord"


def test_record_model_custom_passthrough(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    res = asyncio.run(cf_logs_mod.cf_query_logs(_make_req(record_model="  operation_log  ")))
    _assert_ok(res)
    # 前后空白应被 strip
    assert _FakeClient.captured["json"]["model"] == "operation_log"
