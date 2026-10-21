"""Per-process authentication for the local HTTP and WebSocket API."""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

API_TOKEN = os.environ.get("JGG_API_TOKEN") or secrets.token_urlsafe(32)
COOKIE_NAME = "jgg_api_token"

router = APIRouter()


def _client_is_loopback(scope: dict) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        return ipaddress.ip_address(client[0].split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _host_is_loopback(host: str) -> bool:
    hostname = urlsplit(f"//{host}").hostname
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _same_origin(request: Request, origin: str, host: str) -> bool:
    parsed = urlsplit(origin)
    return (
        parsed.scheme.lower() == request.url.scheme.lower()
        and parsed.netloc.lower() == host.lower()
    )


def _trusted_local_origin(scope: dict) -> bool:
    if not _client_is_loopback(scope):
        return False
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    host = headers.get(b"host", b"").decode("latin-1")
    origin = headers.get(b"origin", b"").decode("latin-1")
    parsed = urlsplit(origin)
    expected_scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
    return (
        _host_is_loopback(host)
        and _host_is_loopback(parsed.netloc)
        and parsed.scheme.lower() == expected_scheme
    )


def _request_token(scope: dict) -> str:
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    direct = headers.get(b"x-api-token", b"").decode("latin-1")
    if direct:
        return direct
    authorization = headers.get(b"authorization", b"").decode("latin-1")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
    except Exception:
        return ""
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else ""


def _authorized(scope: dict) -> bool:
    return hmac.compare_digest(_request_token(scope), API_TOKEN)


def _authorized_or_local_origin(scope: dict) -> bool:
    return _authorized(scope) or _trusted_local_origin(scope)


def _login_page() -> str:
    return """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Jira Git GUI API 登录</title>
<body style="font:16px system-ui;max-width:32rem;margin:12vh auto;padding:0 1rem">
<h1>需要本机 API 令牌</h1>
<p>请向运行 Jira Git GUI 的本机用户获取当前启动令牌。</p>
<form id="login"><label>API 令牌 <input id="token" type="password" required autocomplete="current-password" style="width:100%;padding:.7rem"></label>
<button style="margin-top:1rem;padding:.6rem 1rem">连接</button></form>
<p id="error" role="alert" style="color:#b91c1c"></p>
<script>
document.querySelector('#login').addEventListener('submit', async (event) => {
  event.preventDefault();
  const result = await fetch('/api/auth/session', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token: document.querySelector('#token').value})
  });
  if (result.ok) location.reload();
  else document.querySelector('#error').textContent = '令牌无效或会话无法建立。';
});
</script></body></html>"""


class ApiTokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope_type = scope["type"]
        path = scope.get("path", "")
        if scope_type == "websocket":
            if not _authorized_or_local_origin(scope):
                await send({"type": "websocket.close", "code": 4401, "reason": "API token required"})
                return
            await self.app(scope, receive, send)
            return

        if scope_type != "http":
            await self.app(scope, receive, send)
            return

        is_api_path = path == "/api" or path.startswith("/api/")
        if is_api_path and not path.startswith("/api/auth/") and not _authorized_or_local_origin(scope):
            response = JSONResponse({"detail": "API token required"}, status_code=401)
            await response(scope, receive, send)
            return

        local = _client_is_loopback(scope)
        if path == "/" and not local and not _authorized(scope):
            response = HTMLResponse(_login_page())
            await response(scope, receive, send)
            return

        if local and not path.startswith("/api/"):
            async def add_local_cookie(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((
                        b"set-cookie",
                        f"{COOKIE_NAME}={API_TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400".encode(),
                    ))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, add_local_cookie)
            return

        await self.app(scope, receive, send)


class AuthSessionRequest(BaseModel):
    token: str


@router.get("/api/auth/token", response_class=PlainTextResponse)
async def get_local_api_token(request: Request) -> str:
    host = request.headers.get("host", "")
    if not _client_is_loopback(request.scope) or not _host_is_loopback(host):
        raise HTTPException(status_code=403, detail="Token can only be retrieved from localhost")
    origin = request.headers.get("origin")
    if origin and not _same_origin(request, origin, host):
        raise HTTPException(status_code=403, detail="Cross-origin token requests are not allowed")
    return API_TOKEN


@router.post("/api/auth/session")
async def create_auth_session(payload: AuthSessionRequest, request: Request):
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin and not _same_origin(request, origin, host):
        raise HTTPException(status_code=403, detail="Cross-origin authentication is not allowed")
    if not hmac.compare_digest(payload.token, API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid API token")
    from fastapi.responses import JSONResponse

    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        API_TOKEN,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=86400,
        path="/",
    )
    return response
