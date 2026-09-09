# -*- coding: utf-8 -*-
"""Kibana 面板功能冒烟（pytest 版，真实打 Kibana）。

与 ``tools/kibana_smoke.py``（ASGI 直连）/ ``tools/kibana_smoke_http.py``（真端口）
同源，但落地为 pytest 用例，便于 ``pytest`` 一把梭：

- 用 ``fastapi.testclient.TestClient`` 同步打 ASGI app（进程内直连，
  **不受本机 HTTP_PROXY 影响**；与 test_routes_regression.py 同款）。
- 启动钩子里的 CF 自动登录被 monkeypatch 置空，避免后台真联网噪声。
- 需要真实 Kibana（网络 + 已配置站点）。若 ``/api/kibana/sites`` 不可用，
  整模块 pytest.skip，离线 CI 也能一键跑而不报错。

用法::

    ./venv/bin/python -m pytest tests/test_kibana_smoke.py -q
"""
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api.server  # noqa: E402  —— 必须 import 才注册全部路由
from api.common import app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    # 屏蔽启动钩子里的 CF 自动登录（避免后台真联网 + 噪声），与 test_routes_regression 一致
    async def _no_autologin(*a, **kw):
        return []
    monkeypatch.setattr("api.server.cf_autologin_all", _no_autologin)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _sites(client):
    r = client.get("/api/kibana/sites")
    if r.status_code != 200:
        return None
    d = r.json()
    if not d.get("ok") or not d.get("sites"):
        return None
    return d


@pytest.fixture(autouse=True)
def _require_kibana(client):
    """Kibana 不可用（无配置 / 网络不可达）时，跳过本测试。"""
    if not _sites(client):
        pytest.skip("Kibana 站点不可用（无配置或网络不可达），跳过 Kibana 功能冒烟")


def test_sites(client):
    d = _sites(client)
    assert d["ok"] is True
    assert isinstance(d["sites"], list) and d["sites"]
    assert d.get("current"), "sites 响应缺少 current 标记"


def test_test_endpoint(client):
    d0 = _sites(client)
    site = d0.get("current") or d0["sites"][0]["name"]
    r = client.post("/api/kibana/test", json={"name": site})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    bad = [s for s in (d.get("steps") or []) if not s.get("ok")]
    assert not bad, f"连通性探测失败步骤: {[(s['name'], s.get('detail')) for s in bad]}"


def test_fields(client):
    r = client.get("/api/kibana/fields", params={"start": "now-24h"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    for k in ("namespaces", "containers", "apps", "hosts", "pods"):
        assert k in d, f"fields 缺候选字段 {k}"


def test_pods(client):
    r = client.get("/api/kibana/pods", params={"start": "now-1h"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("pods"), "pods 概览为空"
    assert (d.get("total") or 0) > 0


def test_logs_by_level(client):
    r = client.post("/api/kibana/logs",
                    json={"start": "now-30m", "size": 5, "levels": ["ERROR"]})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True


def test_logs_by_pod_and_context(client):
    d0 = client.get("/api/kibana/pods", params={"start": "now-1h"}).json()
    pod = d0["pods"][0]["pod"]
    r = client.post("/api/kibana/logs",
                    json={"start": "now-2h", "pod": pod, "size": 3})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("rows"), "指定 pod 无日志行"
    anchor = d["rows"][0]["ts"]
    rc = client.post("/api/kibana/context",
                     json={"anchor": anchor, "before": 5, "after": 5})
    assert rc.status_code == 200
    dc = rc.json()
    assert dc.get("ok") is True and dc.get("rows"), "context 上下文为空"


def test_histogram(client):
    r = client.post("/api/kibana/histogram", json={"start": "now-6h"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("buckets"), "histogram 无 buckets"


def test_export(client):
    r = client.post("/api/kibana/export", json={"start": "now-30m", "size": 50})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("content"), "export 内容为空"
    assert d.get("filename")


def test_cache_clear(client):
    r = client.post("/api/kibana/cache/clear", json={"site": "current"})
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True


def test_web_spa_serves_panel(client):
    """SPA 静态服务 + 产物内含 kibana 面板代码。"""
    r = client.get("/web/")
    assert r.status_code == 200, "/web/ 未返回 SPA"
    m = re.search(r'src="(/web/assets/[^"]+\.js)"', r.text)
    assert m, "index.html 未引用 JS bundle"
    js_url = m.group(1)
    js = client.get(js_url).text
    assert "kibana" in js, "bundle 不含 kibana 面板代码"
