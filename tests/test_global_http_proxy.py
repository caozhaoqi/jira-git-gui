import asyncio
import json
import os

import pytest

from api import routes_services_config
from core import network_proxy
from core.errors import UserError


def test_global_proxy_is_persisted_and_applied_to_http_environment(tmp_path, monkeypatch):
    settings_file = tmp_path / "config" / "network.local.json"
    monkeypatch.setattr(network_proxy, "_SETTINGS_FILE", settings_file)
    for key in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")

    result = network_proxy.set_global_http_proxy(" http://proxy.example:3128/ ")

    assert result == "http://proxy.example:3128"
    assert json.loads(settings_file.read_text()) == {"http_proxy": result}
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        assert os.environ[key] == result
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"


@pytest.mark.parametrize(
    "value",
    [
        "proxy.example:3128",
        "socks5://proxy.example:1080",
        "http://user:password@proxy.example:3128",
        "http://proxy.example",
        "http://proxy.example:3128/path",
    ],
)
def test_global_proxy_rejects_unsupported_urls(value):
    with pytest.raises(UserError):
        network_proxy.normalize_http_proxy(value)


def test_service_proxy_overrides_global_fallback():
    assert network_proxy.resolve_http_proxy("http://service-proxy.example:3128") == (
        "http://service-proxy.example:3128"
    )
    assert network_proxy.resolve_http_proxy(
        "http://service-proxy.example:3128",
        "http://request-proxy.example:8080",
    ) == "http://request-proxy.example:8080"
    assert network_proxy.resolve_http_proxy(
        "http://service-proxy.example:3128", " "
    ) == "http://service-proxy.example:3128"


def test_cloud_function_server_proxy_is_saved_and_loaded(tmp_path, monkeypatch):
    from core.config import cf as cf_config

    monkeypatch.setattr(cf_config, "_CF_ACCOUNTS_CACHE", None)
    monkeypatch.setattr(cf_config, "_env_search_roots", lambda project_root=None: [tmp_path])

    monkeypatch.setattr(
        routes_services_config, "_cf_path",
        lambda: tmp_path / "cf_accounts.local.json",
    )
    result = asyncio.run(routes_services_config.add_cloud_function(
        routes_services_config.CloudFunctionReq(
            name="test", server_url="https://cf.example",
            http_proxy="http://proxy.example:3128",
        )
    ))

    assert result["ok"] is True
    assert cf_config.load_cf_accounts(force=True)[0]["http_proxy"] == (
        "http://proxy.example:3128"
    )
    assert asyncio.run(routes_services_config.list_cloud_functions())["items"][0][
        "http_proxy"
    ] == "http://proxy.example:3128"


def test_hcm_gateway_proxy_is_saved_and_returned(tmp_path, monkeypatch):
    import api.common as common
    import api.hcm.hcm_core as hcm_core

    path = tmp_path / "hcm_whitelist.local.json"
    monkeypatch.setattr(routes_services_config, "_hcm_path", lambda: path)
    for module, key in (
        (common, "HCM_PROXY_TARGET"),
        (common, "_HCM_PROXY_TARGET"),
        (common, "HCM_HTTP_PROXY"),
        (common, "_HCM_HTTP_PROXY"),
        (hcm_core, "_HCM_PROXY_TARGET"),
        (hcm_core, "_HCM_HTTP_PROXY"),
    ):
        monkeypatch.setattr(module, key, getattr(module, key))

    result = asyncio.run(routes_services_config.save_hcm_config(
        routes_services_config.HcmConfigReq(
            base_url="https://hcm.example",
            http_proxy="http://proxy.example:3128",
        )
    ))

    assert result["ok"] is True
    assert asyncio.run(routes_services_config.get_hcm_config())["http_proxy"] == (
        "http://proxy.example:3128"
    )


def test_jira_server_proxy_is_saved_to_its_config(tmp_path, monkeypatch):
    from core.config import connect
    import api.common as common

    class DummyJiraClient:
        config = None

        def set_config(self, config):
            self.config = config

        def _drop_http_client(self):
            pass

    monkeypatch.setattr(connect, "_BASE", tmp_path)
    monkeypatch.setattr(routes_services_config, "_ENV_BASE", tmp_path)
    monkeypatch.setattr(common, "client", DummyJiraClient())

    result = asyncio.run(routes_services_config.save_jira_config(
        routes_services_config.JiraConfigReq(
            jira_url="https://jira.example",
            http_proxy="http://proxy.example:3128",
        )
    ))

    assert result["ok"] is True
    assert connect.load_config()[0].http_proxy == "http://proxy.example:3128"
    assert asyncio.run(routes_services_config.get_jira_config())["http_proxy"] == (
        "http://proxy.example:3128"
    )


def test_network_settings_api_saves_and_returns_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        network_proxy, "_SETTINGS_FILE", tmp_path / "network.local.json")
    for key in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.setenv(key, "")

    result = asyncio.run(routes_services_config.save_network_config(
        routes_services_config.NetworkConfigReq(http_proxy="http://proxy.example:3128")
    ))

    assert result == {"ok": True, "http_proxy": "http://proxy.example:3128"}
    assert asyncio.run(routes_services_config.get_network_config()) == result


def test_network_settings_api_reports_invalid_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        network_proxy, "_SETTINGS_FILE", tmp_path / "network.local.json")

    result = asyncio.run(routes_services_config.save_network_config(
        routes_services_config.NetworkConfigReq(http_proxy="proxy.example:3128")
    ))

    assert result["ok"] is False
    assert "代理地址" in result["error"]
    assert not (tmp_path / "network.local.json").exists()
