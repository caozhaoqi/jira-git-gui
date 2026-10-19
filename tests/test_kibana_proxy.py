import asyncio

import httpx
import pytest

from api.kibana import routes_kibana_sites
from core.kibana import client as kibana_client


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("http://10.99.35.201:3128", "http://10.99.35.201:3128"),
        ("https://proxy.example:8443/", "https://proxy.example:8443"),
    ],
)
def test_normalize_http_proxy(value, expected):
    assert kibana_client.normalize_http_proxy(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "10.99.35.201:3128",
        "ftp://proxy.example:21",
        "http://user:password@proxy.example:3128",
        "http://proxy.example:3128/path",
    ],
)
def test_normalize_http_proxy_rejects_unsupported_urls(value):
    with pytest.raises(kibana_client.KibanaError):
        kibana_client.normalize_http_proxy(value)


def test_kibana_client_uses_configured_proxy(monkeypatch):
    captured = {}

    class FakeHttpClient:
        def close(self):
            pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeHttpClient()

    monkeypatch.setattr(httpx, "Client", fake_client)
    cli = kibana_client.KibanaClient({
        "base_url": "http://kibana.example/kibana",
        "http_proxy": "http://10.99.35.201:3128",
    })
    try:
        cli._http()
        assert captured["proxy"] == "http://10.99.35.201:3128"
    finally:
        cli.close()


def test_proxy_change_recreates_cached_site_client(monkeypatch):
    kibana_client.close_all_clients()
    site = {
        "base_url": "http://kibana.example/kibana",
        "username": "elastic",
        "http_proxy": "",
    }
    monkeypatch.setattr("core.kibana.config.get_site", lambda _name: ("test", site))
    try:
        first = kibana_client.get_client("test")
        site["http_proxy"] = "http://10.99.35.201:3128"
        second = kibana_client.get_client("test")
        assert second is not first
        assert second.http_proxy == "http://10.99.35.201:3128"
    finally:
        kibana_client.close_all_clients()


def test_site_save_accepts_proxy_url(monkeypatch):
    captured = {}

    def save(name, **fields):
        captured.update(name=name, **fields)
        return {"current": name}

    monkeypatch.setattr(routes_kibana_sites, "_add_or_update_site", save)
    result = asyncio.run(routes_kibana_sites.api_kibana_site_save(
        routes_kibana_sites.SiteSaveReq(
            name="test",
            base_url="http://kibana.example/kibana",
            http_proxy=" http://10.99.35.201:3128/ ",
        )
    ))
    assert result["ok"] is True
    assert captured["http_proxy"] == "http://10.99.35.201:3128"


def test_connection_test_reports_invalid_proxy():
    result = asyncio.run(routes_kibana_sites.api_kibana_test(
        routes_kibana_sites.TestReq(http_proxy="10.99.35.201:3128")
    ))
    assert result["ok"] is False
    assert "代理地址" in result["error"]


def test_connection_test_uses_unsaved_form_and_keeps_saved_password(monkeypatch):
    saved_site = {
        "base_url": "https://old.example/kibana",
        "username": "elastic",
        "password": "saved-secret",
        "index_pattern": "old-*",
        "time_field": "old_time",
        "msg_field": "log",
        "field_prefix": "kubernetes",
        "http_proxy": "",
        "verify_ssl": False,
        "timeout": 30,
    }
    captured = {}

    class FakeKibanaClient:
        def __init__(self, site):
            captured.update(site)
            self.time_field = site["time_field"]
            self.index_pattern = site["index_pattern"]

        def kibana_status(self):
            return {"http_status": 200, "kibana_version": "7.5.2", "overall": "green"}

        def cluster_health(self):
            return {"cluster_name": "test", "status": "yellow", "nodes": 1}

        def es_search(self, _dsl):
            return {"hits": {"total": {"value": 1}}}

        def close(self):
            pass

    monkeypatch.setattr(
        "core.kibana.config.get_site", lambda _name: ("jt-test", saved_site))
    monkeypatch.setattr(
        "core.kibana.client.KibanaClient", FakeKibanaClient)
    result = asyncio.run(routes_kibana_sites.api_kibana_test(
        routes_kibana_sites.TestReq(
            name="jt-test",
            base_url="http://10.22.78.121/kibana",
            username="elastic",
            password="",
            index_pattern="logstash-*",
            time_field="es_time",
            http_proxy="http://10.99.35.201:3128",
        )
    ))
    assert result["ok"] is True
    assert captured["base_url"] == "http://10.22.78.121/kibana"
    assert captured["http_proxy"] == "http://10.99.35.201:3128"
    assert captured["password"] == "saved-secret"
