# -*- coding: utf-8 -*-
"""HCM 网关临时文件下载（接口日志 / SQL Profile）代理回归测试（离线，无真实网关）。

守住「点击 Download interface log 报错」这个真实缺陷的根因：

1. **打到错误的 origin**
   网关的 ``/document/temp/download`` 挂在 **HCM 网关** 上，不在本机后端。
   前端若用 ``<a href="/document/temp/download?index=...">``，请求会落到本机
   origin（前端 dist 由本机后端托管）→ 404。

2. **跨 origin 不带 cookie**
   网关靠 cookie ``token`` 认登录态；即便把地址拼成绝对 URL，浏览器跳转
   到另一个 host 也不会带上该 cookie → 网关返回「请登录后操作」。

因此必须由后端带着 token 代取。本测试锁死：
- 请求 URL 指向传入的 ``target``（而不是本机）；
- token 以 **cookie** 形式传递；
- 二进制响应原样回传（带 attachment 文件名）；
- 网关以 JSON 报错时**不能**当成文件下发，要把错误信息透出；
- 登录失效时网关回 **HTTP 200 + text/html 错误页**（实测暗号
  ``system_errors_msg_00000062``）——同样不能当文件下发，且按疑似登录态
  失效**自动重登重试一次**（无效 preset token 跨网关下载日志的修复）。

运行：./venv/bin/python -m pytest tests/test_hcm_temp_download.py -s
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.hcm.hcm_core as _core  # noqa: E402
from api.hcm.routes_hcm import router as _router  # noqa: E402


class _Req:
    """最小请求替身：只需 index / name / token / target 四个字段。"""

    def __init__(self, index="", name="", token="", target=""):
        self.index = index
        self.name = name
        self.token = token
        self.target = target


class _FakeResp:
    def __init__(self, content: bytes, status_code: int = 200, content_type: str = "application/octet-stream"):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    """httpx.AsyncClient 替身：记录 URL 与 cookies，返回预置响应。"""

    last: dict = {}
    response: _FakeResp = _FakeResp(b"")

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, cookies=None):
        _FakeAsyncClient.last = {"url": url, "cookies": cookies}
        return _FakeAsyncClient.response


@pytest.fixture
def fake_httpx(monkeypatch):
    def _install(resp: _FakeResp):
        _FakeAsyncClient.response = resp
        _FakeAsyncClient.last = {}
        monkeypatch.setattr(_core.httpx, "AsyncClient", _FakeAsyncClient)
        return _FakeAsyncClient

    return _install


# ---- 0. 路由已注册 ---------------------------------------------------------- #
def test_route_registered():
    paths = {getattr(r, "path", None) for r in _router.routes}
    assert "/api/hcm/temp-download" in paths, "缺少 /api/hcm/temp-download 路由"


# ---- 1. 参数校验 ------------------------------------------------------------ #
def test_missing_index_rejected():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="")))
    assert ei.value.status_code == 400


@pytest.mark.parametrize("bad", ["a/b", "../etc/passwd", "log_.._x"])
def test_path_traversal_rejected(bad):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index=bad, target="http://gw")))
    assert ei.value.status_code == 400


def test_missing_target_rejected(monkeypatch):
    monkeypatch.setattr(_core, "_HCM_PROXY_TARGET", "")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="log_1")))
    assert ei.value.status_code == 500


# ---- 2. URL 指向网关 + token 走 cookie --------------------------------------- #
def test_url_targets_gateway_and_token_as_cookie(fake_httpx):
    fake_httpx(_FakeResp(b"hello-log", content_type="text/plain"))
    resp = asyncio.run(
        _core.hcm_temp_download(
            _Req(index="log_abc.txt", name="my log.txt", token="TK-123", target="http://10.22.78.121")
        )
    )
    sent = _FakeAsyncClient.last
    assert sent["url"].startswith("http://10.22.78.121/document/temp/download?"), sent["url"]
    assert "index=log_abc.txt" in sent["url"], sent["url"]
    assert "name=my%20log.txt" in sent["url"], sent["url"]
    # token 必须作为 cookie（网关就是这么认登录态的），不能塞在 query 里
    assert sent["cookies"] == {"token": "TK-123"}, sent["cookies"]
    assert "token=" not in sent["url"], "token 不应出现在 URL 里"

    # 二进制原样回传，并带上 attachment 文件名
    assert resp.body == b"hello-log"
    assert "attachment" in resp.headers["content-disposition"]
    assert "UTF-8''" in resp.headers["content-disposition"]


def test_preset_token_used_when_request_omits_it(fake_httpx, monkeypatch):
    monkeypatch.setattr(_core, "_HCM_PRESET_TOKEN", "PRESET-TK")
    fake_httpx(_FakeResp(b"x"))
    asyncio.run(_core.hcm_temp_download(_Req(index="log_1", target="http://gw")))
    assert _FakeAsyncClient.last["cookies"] == {"token": "PRESET-TK"}


# ---- 3. 网关 JSON 报错不能当文件下发 ----------------------------------------- #
def test_gateway_json_error_surfaces_message(fake_httpx):
    body = '{"errcode": 80002, "errmsg": "请登录后操作"}'.encode("utf-8")
    fake_httpx(_FakeResp(body, status_code=200, content_type="application/json; charset=utf-8"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="log_1", target="http://gw")))
    assert "请登录后操作" in str(ei.value.detail), ei.value.detail
    # 关键：不能把这段 JSON 当成日志文件返回
    assert ei.value.status_code == 400


def test_gateway_http_error_surfaces_status(fake_httpx):
    fake_httpx(_FakeResp(b"boom", status_code=500, content_type="text/html"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="log_1", target="http://gw")))
    assert ei.value.status_code == 500
    assert "HCM 下载失败" in str(ei.value.detail)


# ---- 4. HTML 错误页不当下发 + 登录失效自动重登重试 --------------------------- #
HTML_AUTH_ERR = "出错啦！\nerror\nsystem_errors_msg_00000062\n".encode("utf-8")


def test_html_error_page_not_downloaded_as_file(fake_httpx):
    """登录失效时网关回 200 + text/html 错误页（实测暗号 system_errors_msg_00000062）：
    不能当文件下发；cf_accounts 无 gw 账号 → 不重试，直接透出错误。"""
    fake_httpx(_FakeResp(HTML_AUTH_ERR, status_code=200, content_type="text/html; charset=utf-8"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="profile_x", target="http://gw")))
    assert ei.value.status_code == 400
    assert "system_errors_msg_00000062" in str(ei.value.detail), ei.value.detail


class _ScriptedClient:
    """按脚本依次返回响应并记录 cookies（验证重登后确带新 token 重试）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.cookies_seen = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, cookies=None):
        self.cookies_seen.append(cookies)
        return self._responses.pop(0)


def _install_scripted(monkeypatch, responses):
    fake = _ScriptedClient(responses)
    monkeypatch.setattr(_core.httpx, "AsyncClient", lambda **kw: fake)
    return fake


async def _fake_relogin_ok(target_url):
    return "NEW-TK"


async def _fake_relogin_fail(target_url):
    return ""


def test_html_auth_failure_triggers_relogin_retry(monkeypatch):
    """无效 token → HTML 错误页 → 自动重登 → 重试拿到文件（hr-test 实测场景）。"""
    fake = _install_scripted(monkeypatch, [
        _FakeResp(HTML_AUTH_ERR, status_code=200, content_type="text/html"),
        _FakeResp(b"profile-bytes", status_code=200, content_type="application/octet-stream"),
    ])
    monkeypatch.setattr(_core, "_auto_relogin_token", _fake_relogin_ok)
    resp = asyncio.run(
        _core.hcm_temp_download(_Req(index="profile_x", token="bad", target="http://gw"))
    )
    assert resp.body == b"profile-bytes"
    assert len(fake.cookies_seen) == 2
    assert fake.cookies_seen[0] == {"token": "bad"}
    assert fake.cookies_seen[1] == {"token": "NEW-TK"}


def test_html_auth_failure_relogin_retry_still_fails(monkeypatch):
    """重登成功但重试仍失败：透传重试后的错误，不把错误页当文件。"""
    _install_scripted(monkeypatch, [
        _FakeResp(HTML_AUTH_ERR, status_code=200, content_type="text/html"),
        _FakeResp(HTML_AUTH_ERR, status_code=200, content_type="text/html"),
    ])
    monkeypatch.setattr(_core, "_auto_relogin_token", _fake_relogin_ok)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="profile_x", token="bad", target="http://gw")))
    assert ei.value.status_code == 400
    assert "system_errors_msg_00000062" in str(ei.value.detail)


def test_missing_file_no_relogin(monkeypatch):
    """非登录类错误（文件不存在 JSON）：不触发重登，只调一次。"""
    body = '{"errcode": 80002, "errmsg": "文件不存在"}'.encode("utf-8")
    fake = _install_scripted(monkeypatch, [
        _FakeResp(body, status_code=200, content_type="application/json"),
    ])
    monkeypatch.setattr(_core, "_auto_relogin_token", _fake_relogin_ok)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_core.hcm_temp_download(_Req(index="log_1", token="TK", target="http://gw")))
    assert "文件不存在" in str(ei.value.detail)
    assert len(fake.cookies_seen) == 1
