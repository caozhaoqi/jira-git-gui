# -*- coding: utf-8 -*-
"""CF 云函数日志 —— 路由层（薄 HTTP 适配）。

业务逻辑全部在 api.cf_core，这里只做：参数接收、异常→HTTP 状态码映射、
SSE 广播副作用（如登录成功后广播 token 更新）。
"""
from fastapi import HTTPException

from fastapi import APIRouter
from api.common import app, logger, broadcast, get_cf_accounts
from api.cf.cf_core import (
    cf_captcha_fetch, cf_login_account, cf_autologin_all,
    cf_refresh_token, cf_query_logs, cf_export_logs, cf_save_clipboard,
    cf_mask_tokens,
)
from api.schemas import (
    CfLogReq, CfLogExportReq, CfLoginReq, CfCaptchaReq, CfAutoLoginReq, ClipboardSaveReq,
)

router = APIRouter()


def _http_error(e: Exception) -> HTTPException:
    """把业务异常映射为合适的 HTTP 状态码。"""
    msg = str(e)
    if isinstance(e, (PermissionError,)):
        return HTTPException(401, msg)
    if isinstance(e, (ConnectionError,)):
        return HTTPException(502, msg)
    if isinstance(e, (TimeoutError,)):
        return HTTPException(504, msg)
    if isinstance(e, (ValueError,)):
        return HTTPException(400, msg)
    return HTTPException(500, msg)


@router.get("/api/cf/accounts")
async def api_cf_accounts():
    """返回配置的 CF 账号列表（不含凭证明文）。"""
    accounts = get_cf_accounts()
    return {
        "accounts": [
            {
                "name": a.get("name", ""),
                "server_url": a.get("server_url", ""),
                "username": a.get("username", a.get("mobile", "")),
                "has_password": bool(a.get("password")),
                "has_cookie": bool(a.get("cookie")),
                "source": "config",
            }
            for a in accounts
        ]
    }


@router.get("/api/cf/captcha")
async def api_cf_captcha(server_url: str = "", proxy: str = ""):
    """获取 CF 登录验证码图片（返回 base64 data URL）。"""
    try:
        return await cf_captcha_fetch(server_url, proxy=proxy)
    except (ValueError, ConnectionError, TimeoutError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/login")
async def api_cf_login(req: CfLoginReq):
    """手动登录单个 CF 账号（支持验证码），缓存并返回 token。"""
    try:
        r = await cf_login_account(
            {"server_url": req.server_url, "username": req.mobile, "password": req.password},
            proxy=req.proxy,
        )
    except Exception as e:
        raise _http_error(e)
    from api.cf.cf_core import _CF_TOKEN_CACHE, _cf_tokens_save
    su = req.server_url.rstrip("/")
    if r.get("ok"):
        _CF_TOKEN_CACHE[su] = {
            "token": r["token"], "cookie": r.get("cookie", ""),
            "name": req.mobile, "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "need_captcha": False, "last_error": "",
        }
        _cf_tokens_save()
        broadcast("cf_token_update", {"server_url": su, "name": req.mobile, "ok": True})
        return {"ok": True, "token": r["token"], "cookie": r.get("cookie", ""), "message": "登录成功"}
    _CF_TOKEN_CACHE.setdefault(su, {"token": "", "name": req.mobile})
    _CF_TOKEN_CACHE[su].update({"last_error": r.get("message", ""), "need_captcha": r.get("need_captcha", False)})
    return {"ok": False, "need_captcha": r.get("need_captcha", False), "message": r.get("message", "登录失败")}


@router.post("/api/cf/auto-login")
async def api_cf_auto_login(req: CfAutoLoginReq):
    """对配置的账号做自动登录（账号密码），返回结果列表。"""
    try:
        results = await cf_autologin_all(proxy=req.proxy)
    except Exception as e:
        raise _http_error(e)
    broadcast("cf_token_update", {"auto_login": True, "count": len(results)})
    return {"results": results, "count": len(results)}


@router.get("/api/cf/tokens")
async def api_cf_tokens():
    """返回已缓存 token 状态（掩码，不含明文）。"""
    return cf_mask_tokens()


@router.post("/api/cf/logs")
async def api_cf_logs(req: CfLogReq):
    """查询 CF 云函数日志。"""
    try:
        return await cf_query_logs(req)
    except (ValueError, PermissionError, ConnectionError, TimeoutError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/logs/export")
async def api_cf_export(req: CfLogExportReq):
    """导出 CF 日志到本地 JSON 文件。"""
    try:
        return cf_export_logs(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/clipboard-save")
async def api_cf_clipboard_save(req: ClipboardSaveReq):
    """保存剪贴板内容到本地文件。"""
    try:
        return cf_save_clipboard(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)
