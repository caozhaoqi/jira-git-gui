# -*- coding: utf-8 -*-
"""服务账号「域名别名」（domain 字段）贯通测试。

domain 是 cf_accounts 条目的可选展示别名：同一服务器的域名地址。
约定：仅用于下拉展示识别，连接 / 登录仍走 server_url。

覆盖链路：
1. core.config.cf.load_cf_accounts —— 从 JSON 读出 domain（缺省空串）
2. api.hcm.hcm_core.hcm_envs —— 环境列表透传 domain
3. api.cf.routes_cf.api_cf_accounts —— 账号列表透传 domain
4. api.cfdebug.routes_cfdebug.get_accounts —— 调试面板账号列表透传 domain
5. core.kibana.servers.list_servers —— 汇总服务器透传 domain
6. core.kibana.config._server_sites —— 派生站点 label 追加「 · 域名」
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture()
def accounts_root(tmp_path: Path) -> Path:
    """构造一份含 domain 的 cf_accounts.local.json。"""
    data = {
        "accounts": [
            {
                "name": "测试环境",
                "server_url": "http://10.1.38.184",
                "domain": "test.hcmcloud.cn",
                "username": "u1",
                "password": "p1",
                "type": "HCM",
            },
            {
                "name": "无域名环境",
                "server_url": "http://10.1.38.155",
                "username": "u2",
                "password": "p2",
            },
        ]
    }
    (tmp_path / "cf_accounts.local.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _fake_accounts():
    from core.config.cf import load_cf_accounts
    return load_cf_accounts(force=True)


def test_loader_reads_domain(accounts_root: Path):
    from core.config.cf import load_cf_accounts
    accs = load_cf_accounts(project_root=accounts_root, force=True)
    assert accs[0]["domain"] == "test.hcmcloud.cn"
    # 未配置 domain 的条目回退为空串（而非 KeyError / None）
    assert accs[1]["domain"] == ""
    # 连接地址不受影响
    assert accs[0]["server_url"] == "http://10.1.38.184"


def test_hcm_envs_passes_domain(monkeypatch):
    import api.hcm.hcm_core as hcm_core

    monkeypatch.setattr(
        hcm_core, "get_cf_accounts",
        lambda: [{"name": "A", "server_url": "http://10.0.0.1",
                  "domain": "a.hcmcloud.cn", "username": "", "password": "",
                  "default": False}])
    import asyncio
    d = asyncio.run(hcm_core.hcm_envs())
    env = [e for e in d["envs"] if e["key"] != "hcm_proxy"][0]
    assert env["domain"] == "a.hcmcloud.cn"


def test_cf_accounts_route_passes_domain(monkeypatch):
    import api.cf.routes_cf as routes_cf

    monkeypatch.setattr(routes_cf, "get_cf_accounts", lambda: [
        {"name": "A", "server_url": "http://10.0.0.1",
         "domain": "a.hcmcloud.cn", "username": "u", "password": "p"}])
    import asyncio
    out = asyncio.run(routes_cf.api_cf_accounts())
    assert out["accounts"][0]["domain"] == "a.hcmcloud.cn"


def test_cfdebug_accounts_passes_domain(monkeypatch):
    import api.cfdebug.routes_cfdebug as rc

    monkeypatch.setattr(rc, "load_cf_accounts", lambda: [
        {"name": "A", "server_url": "http://10.0.0.1",
         "domain": "a.hcmcloud.cn", "username": "u", "password": "p",
         "default": False}])
    out = rc.get_accounts()
    assert out["items"][0]["domain"] == "a.hcmcloud.cn"


def test_list_servers_passes_domain(monkeypatch):
    import core.kibana.servers as servers

    monkeypatch.setattr(
        "core.config.cf.load_cf_accounts",
        lambda *a, **k: [{"name": "A", "server_url": "http://10.0.0.1",
                          "domain": "a.hcmcloud.cn", "username": "u",
                          "password": "p", "default": False}])
    monkeypatch.setattr(
        "core.config.hcm.load_hcm_whitelist", lambda: {})
    out = servers.list_servers()
    cf_entries = [s for s in out if s["source"] == "cf_accounts"]
    assert cf_entries and cf_entries[0]["domain"] == "a.hcmcloud.cn"


def test_server_sites_label_appends_domain(monkeypatch):
    import core.kibana.config as kcfg

    # _server_sites 在函数体内 from core.kibana.servers import list_servers，
    # 因此 patch 源模块属性即可被取到。
    monkeypatch.setattr(
        "core.kibana.servers.list_servers",
        lambda: [{"key": "cf:http://10.0.0.1", "name": "A",
                  "server_url": "http://10.0.0.1",
                  "domain": "a.hcmcloud.cn", "source": "cf_accounts"}])
    sites = kcfg._server_sites({"label": "base", "base_url": "http://x"})
    item = next(iter(sites.values()))
    assert item["label"] == "A · a.hcmcloud.cn"
    # 无 domain 的服务器 label 不变
    monkeypatch.setattr(
        "core.kibana.servers.list_servers",
        lambda: [{"key": "cf:b", "name": "B",
                  "server_url": "http://10.0.0.2",
                  "domain": "", "source": "cf_accounts"}])
    sites = kcfg._server_sites({"label": "base", "base_url": "http://x"})
    assert next(iter(sites.values()))["label"] == "B"
