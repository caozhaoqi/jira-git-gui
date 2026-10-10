# -*- coding: utf-8 -*-
"""CF 手动登录「密码留空 → 后端就地补齐已存密码」回归测试。

背景（2026-10-09）：/api/cf/accounts 只回 has_password 布尔、永不下发明文密码
（密码不出网），前端切环境只能预填 server_url/username，密码框必然为空，
点「登录获取 Token」被前端校验拦下（"Please fill in server URL, mobile and password"）。
修复：前端放行空密码（选中环境 has_password 时），后端 api_cf_login 按
server_url(+username) 从配置账号就地补齐密码后再登录，明文全程不出后端。
"""
import asyncio

from api.cf import routes_cf


def _patch_accounts(monkeypatch, accounts):
    monkeypatch.setattr(routes_cf, "get_cf_accounts", lambda: accounts)


def test_resolve_stored_password_basic(monkeypatch):
    _patch_accounts(monkeypatch, [
        {"name": "a", "server_url": "http://x/", "username": "186", "password": "pw1"},
        {"name": "b", "server_url": "http://y", "username": "187", "password": "pw2"},
    ])
    # 尾斜杠归一化
    assert routes_cf._resolve_stored_password("http://x", "186") == "pw1"
    assert routes_cf._resolve_stored_password("http://x/", "186") == "pw1"
    # 不带用户名 → 该网关下第一个有密码的账号兜底
    assert routes_cf._resolve_stored_password("http://x") == "pw1"
    # 用户名不匹配但该网关只有一个账号 → 仍兜底
    assert routes_cf._resolve_stored_password("http://y", "999") == "pw2"
    # 未知网关 / 空参
    assert routes_cf._resolve_stored_password("http://z", "186") == ""
    assert routes_cf._resolve_stored_password("", "186") == ""


def test_resolve_stored_password_prefers_username_match(monkeypatch):
    _patch_accounts(monkeypatch, [
        {"name": "a", "server_url": "http://x", "username": "186", "password": "pw186"},
        {"name": "b", "server_url": "http://x", "username": "187", "password": "pw187"},
    ])
    assert routes_cf._resolve_stored_password("http://x", "187") == "pw187"
    assert routes_cf._resolve_stored_password("http://x", "186") == "pw186"


def test_resolve_skips_account_without_password(monkeypatch):
    _patch_accounts(monkeypatch, [
        {"name": "a", "server_url": "http://x", "username": "186", "password": ""},
        {"name": "b", "server_url": "http://x", "username": "187", "password": "pw187"},
    ])
    # 186 无密码 → 跳过，落到同网关 187 的密码（兜底语义：网关维度假定单账号）
    assert routes_cf._resolve_stored_password("http://x", "186") == "pw187"


def test_login_fills_blank_password_from_stored_account(monkeypatch):
    """密码留空的登录请求：后端用配置账号密码补齐后再登录，明文不出后端。"""
    captured = {}

    async def fake_login(account, proxy=""):
        captured.update(account)
        return {"ok": True, "token": "T", "cookie": "", "need_captcha": False, "message": ""}

    async def fake_broadcast(*a, **k):
        pass

    _patch_accounts(monkeypatch, [
        {"name": "a", "server_url": "http://x", "username": "186", "password": "pw1"},
    ])
    monkeypatch.setattr(routes_cf, "cf_login_account", fake_login)
    monkeypatch.setattr(routes_cf, "broadcast", fake_broadcast)
    # 登录成功分支会落盘 token 缓存，测试里拦掉
    monkeypatch.setattr("api.cf.cf_tokens._cf_tokens_save", lambda: None)

    req = routes_cf.CfLoginReq(server_url="http://x", mobile="186", password="")
    resp = asyncio.run(routes_cf.api_cf_login(req))
    assert resp["ok"] is True
    assert captured["password"] == "pw1"


def test_login_keeps_explicit_password(monkeypatch):
    """用户手填密码时必须原样使用，不得被配置账号密码覆盖。"""
    captured = {}

    async def fake_login(account, proxy=""):
        captured.update(account)
        return {"ok": False, "token": "", "need_captcha": False, "message": "账号或密码错误"}

    _patch_accounts(monkeypatch, [
        {"name": "a", "server_url": "http://x", "username": "186", "password": "pw1"},
    ])
    monkeypatch.setattr(routes_cf, "cf_login_account", fake_login)

    req = routes_cf.CfLoginReq(server_url="http://x", mobile="186", password="typed-pw")
    asyncio.run(routes_cf.api_cf_login(req))
    assert captured["password"] == "typed-pw"
