# -*- coding: utf-8 -*-
"""CfDebug 日志管理 tab：记录模型（record_model/model）UI 可配置 —— 透传单测。

验证 api/cfdebug/routes_cfdebug 的 list/delete 把 model 参数透传给 hcm.model.list/delete：
- list_dynamic_logs 默认 dynamic_log，可传 SyncOuterRecord 等
- delete_dynamic_logs 使用与列表一致的模型
"""
from types import SimpleNamespace

import api.cfdebug.routes_cfdebug as routes


class _FakeCU:
    """替换 RealCustomerUtil：记录 call_open_api 参数并返回可控结果。"""
    calls = []

    def __init__(self, server, token, dry_run=False, company_id=1):
        self.server = server
        self.token = token
        self.company_id = company_id

    def call_open_api(self, api, param):
        _FakeCU.calls.append({"api": api, "param": param})
        if api == "hcm.model.list":
            return {"list": [{"id_": 1, "content": "x"}], "total": 1}
        return {"ok": True}


def _patch_env(monkeypatch):
    monkeypatch.setattr(routes, "RealCustomerUtil", _FakeCU)
    monkeypatch.setattr(routes, "_resolve_dynlog_creds",
                        lambda env, server, token: {"server": "s", "token": "t", "env": ""})
    _FakeCU.calls = []


def test_list_default_model_is_dynamic_log(monkeypatch):
    _patch_env(monkeypatch)
    r = routes.list_dynamic_logs()
    assert r["ok"] is True
    assert r["model"] == "dynamic_log"
    assert _FakeCU.calls[0]["param"]["model"] == "dynamic_log"


def test_list_custom_model_sync_outer_record(monkeypatch):
    _patch_env(monkeypatch)
    r = routes.list_dynamic_logs(model="SyncOuterRecord")
    assert r["ok"] is True
    assert r["model"] == "SyncOuterRecord"
    assert _FakeCU.calls[0]["param"]["model"] == "SyncOuterRecord"


def test_delete_uses_same_model(monkeypatch):
    _patch_env(monkeypatch)
    req = SimpleNamespace(ids=[7], env=None, server=None, token=None, company_id=1, model="operation_log")
    r = routes.delete_dynamic_logs(req)
    assert r["deleted"] == 1
    assert _FakeCU.calls[0]["api"] == "hcm.model.delete"
    assert _FakeCU.calls[0]["param"] == {"model": "operation_log", "id_": 7}


def test_delete_default_model(monkeypatch):
    _patch_env(monkeypatch)
    req = SimpleNamespace(ids=[7], env=None, server=None, token=None, company_id=1, model=None)
    r = routes.delete_dynamic_logs(req)
    assert r["deleted"] == 1
    assert _FakeCU.calls[0]["param"]["model"] == "dynamic_log"
