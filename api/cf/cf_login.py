# -*- coding: utf-8 -*-
"""CF 云函数日志 —— 账号登录与 token 刷新。

依赖 ``api.cf_tokens`` 中的共享状态（``_CF_TOKEN_CACHE`` / ``_cf_tokens_save``）。
"""
import time

import httpx

from api.common import logger, get_cf_accounts
from api.cf.cf_tokens import _CF_TOKEN_CACHE, _cf_tokens_save


async def cf_login_account(account: "dict", proxy: str = "") -> "dict":
    """对单个 cf_accounts 账号用账号密码登录，返回 token。

    供首次启动 / 手动「自动获取」使用。自动流程无法人工输入验证码，
    需要验证码的账号返回 need_captcha=True，由前端引导手动登录。
    """
    server_url = (account.get("server_url") or "").strip()
    mobile = (account.get("username") or account.get("mobile") or "").strip()
    password = (account.get("password") or "").strip()
    if not server_url or not mobile or not password:
        return {"ok": False, "token": "", "need_captcha": False,
                "message": "缺少 server_url / 用户名 / 密码，跳过"}
    base = server_url.rstrip("/")
    url = f"{base}/login"
    kwargs = dict(timeout=15, follow_redirects=True)
    if proxy:
        kwargs["proxy"] = proxy
    else:
        kwargs["transport"] = httpx.AsyncHTTPTransport()
    form_data = {
        "mobile": mobile,
        "password": password,
        "pure_result": "true",
        "transfer_strategy": "no",
        "un_redirect": "true",
        "mode": "PWD",
    }
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(url, data=form_data)
            if resp.status_code >= 400:
                return {"ok": False, "token": "", "need_captcha": False,
                        "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            try:
                data = resp.json()
            except ValueError:
                return {"ok": False, "token": "", "need_captcha": False,
                        "message": f"非JSON响应(疑似登录页HTML): {resp.text[:200]}"}
            if isinstance(data, dict) and data.get("success") is False:
                msg = data.get("message", "登录失败")
                need = bool(data.get("need_img_valid"))
                return {"ok": False, "token": "", "need_captcha": need, "message": msg}
            token = ""
            if isinstance(data, dict):
                token = data.get("token", "")
                if not token and isinstance(data.get("result"), dict):
                    token = data["result"].get("token", "")
            if not token:
                for cookie in resp.cookies.jar:
                    if cookie.name == "token":
                        token = cookie.value
                        break
            try:
                raw_cookies = resp.headers.get_list("set-cookie")
            except Exception:
                raw_cookies = []
            cookie_header = "; ".join(s.split(";", 1)[0] for s in raw_cookies).strip()
            if not cookie_header:
                cookie_header = (f"token={token}" if token else "")
            if not token and not cookie_header:
                return {"ok": False, "token": "", "need_captcha": False,
                        "message": f"登录成功但未取到token/会话cookie: {str(data)[:200]}"}
            return {"ok": True, "token": token, "cookie": cookie_header,
                    "need_captcha": False, "message": ""}
    except httpx.ConnectError as e:
        return {"ok": False, "token": "", "need_captcha": False, "message": f"无法连接 {base}: {e}"}
    except httpx.TimeoutException:
        return {"ok": False, "token": "", "need_captcha": False, "message": "登录超时"}
    except Exception as e:
        return {"ok": False, "token": "", "need_captcha": False, "message": f"{type(e).__name__}: {e}"}


async def cf_autologin_all(proxy: str = "") -> "list":
    """遍历 cf_accounts 用账号密码登录，缓存 token 到内存 + 落盘。"""
    accounts = get_cf_accounts()
    results = []
    for acc in accounts:
        su = (acc.get("server_url") or "").strip()
        if not su:
            continue
        r = await cf_login_account(acc, proxy=proxy)
        entry = {"name": acc.get("name", su), "server_url": su,
                 "ok": r["ok"], "need_captcha": r["need_captcha"], "message": r["message"],
                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        if r["ok"]:
            _CF_TOKEN_CACHE[su] = {"token": r["token"], "cookie": r.get("cookie", ""),
                                   "name": acc.get("name", su),
                                   "ts": entry["ts"], "need_captcha": False, "last_error": ""}
            logger.info(f"[CF][auto-login] 成功: {acc.get('name')} ({su}) token长度={len(r['token'])}")
        else:
            prev = _CF_TOKEN_CACHE.get(su)
            if isinstance(prev, dict):
                prev["last_error"] = r["message"]
                prev["need_captcha"] = r["need_captcha"]
                prev["ts"] = entry["ts"]
            else:
                _CF_TOKEN_CACHE[su] = {"token": "", "name": acc.get("name", su),
                                       "ts": entry["ts"], "need_captcha": r["need_captcha"],
                                       "last_error": r["message"]}
            logger.warning(f"[CF][auto-login] 失败: {acc.get('name')} ({su}) -> {r['message']}")
        results.append(entry)
    _cf_tokens_save()
    return results


def cf_account_by_server(server_url: str) -> "dict | None":
    """按 server_url 在 cf_accounts 中找到对应账号（用于按需重登）。"""
    su = (server_url or "").strip().rstrip("/")
    if not su:
        return None
    for acc in get_cf_accounts():
        if (acc.get("server_url") or "").strip().rstrip("/") == su:
            return acc
    return None


async def cf_refresh_token(server_url: str, proxy: str = "") -> "dict | None":
    """按需对单个账号重新登录，刷新内存 + 落盘的 token/cookie 缓存。"""
    acc = cf_account_by_server(server_url)
    if not acc:
        logger.warning(f"[CF][refresh] 找不到账号: {server_url}")
        return None
    r = await cf_login_account(acc, proxy=proxy)
    su = (server_url or "").strip().rstrip("/")
    if r.get("ok"):
        entry = {"token": r["token"], "cookie": r.get("cookie", ""),
                 "name": acc.get("name", su), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "need_captcha": False, "last_error": ""}
        _CF_TOKEN_CACHE[su] = entry
        _cf_tokens_save()
        logger.info(f"[CF][refresh] 成功刷新: {acc.get('name')} ({su})")
        return entry
    logger.warning(f"[CF][refresh] 失败: {acc.get('name')} ({server_url}) -> {r.get('message')}")
    prev = _CF_TOKEN_CACHE.get(su)
    if isinstance(prev, dict):
        prev["last_error"] = r.get("message")
        prev["need_captcha"] = r.get("need_captcha")
        prev["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return None
