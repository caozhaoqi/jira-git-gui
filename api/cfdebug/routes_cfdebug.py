# -*- coding: utf-8 -*-
"""
云函数调试：HTTP / WebSocket 路由（api/cfdebug/routes_cfdebug.py）
================================================================
端点：
  GET  /api/cf-debug/functions        列出云函数（扫描 functions_root）
  GET  /api/cf-debug/source?file=     读取云函数源码（供源码窗格 + 断点）
  GET  /api/cf-debug/environments     当前调试环境配置
  POST /api/cf-debug/environment      保存调试环境配置（持久化）
  POST /api/cf-debug/run              启动调试会话（返回 DAP ws_url）
  POST /api/cf-debug/stop             停止会话
  GET  /api/cf-debug/sessions         列出活动会话
  WS   /api/cf-debug/ws/{session_id}  DAP 桥接（浏览器 DAP 客户端连这里）

SSE 事件：cf_debug_log（运行/错误/debug 日志，带 session_id）、cf_debug_done（会话结束）。
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.common import logger, broadcast
from api.cfdebug import runner
from api.cfdebug.dap_bridge import bridge_dap
from core.config.cf import load_cf_accounts
from api.cf.cf_login import cf_login_account

router = APIRouter()


class RunReq(BaseModel):
    file: str
    root: Optional[str] = None
    kwargs: str = "{}"
    env: Optional[str] = None          # mock / test / custom
    server: Optional[str] = None
    token: Optional[str] = None
    server_account: Optional[str] = None  # 远程模式：cf_accounts 的 server_url 或 name，后端据此登录取 token
    debug_id: Optional[str] = None
    db_url: Optional[str] = None
    allow_ddl: bool = False
    db_save: bool = False
    write_real: bool = False
    entry: Optional[str] = None
    company_id: int = 1


class EnvReq(BaseModel):
    functions_root: Optional[str] = None
    current_env: Optional[str] = None
    test: Optional[Dict[str, str]] = None
    custom: Optional[Dict[str, str]] = None


def _err(status: int, msg: str):
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


@router.get("/api/cf-debug/functions")
def get_functions(root: Optional[str] = None):
    try:
        return runner.list_functions(root)
    except Exception as e:
        logger.warning("[cfdebug] list_functions 失败: %s", e)
        return _err(500, str(e))


@router.get("/api/cf-debug/source")
def get_source(file: str = ""):
    if not file:
        return _err(400, "缺少 file 参数")
    return runner.read_source(file)


@router.get("/api/cf-debug/environments")
def get_environments():
    return runner.get_env()


@router.post("/api/cf-debug/environment")
def post_environment(req: EnvReq):
    try:
        return runner.set_env(req.dict(exclude_unset=True))
    except Exception as e:
        return _err(500, str(e))


def _resolve_account(key: str) -> Optional[Dict[str, Any]]:
    """按 server_url 或 name 在 cf_accounts 中定位账号（远程模式选服务器用）。"""
    key = (key or "").strip()
    if not key:
        return None
    for acc in load_cf_accounts():
        if not isinstance(acc, dict):
            continue
        if (acc.get("server_url") or "").strip() == key or (acc.get("name") or "").strip() == key:
            return acc
    return None


@router.get("/api/cf-debug/accounts")
def get_accounts():
    """列出 cf_accounts 服务账号（供远程模式选服务器；密码不返回）。"""
    out = []
    for i, acc in enumerate(load_cf_accounts()):
        if not isinstance(acc, dict):
            continue
        out.append({
            "index": i,
            "name": acc.get("name", ""),
            "server_url": acc.get("server_url", ""),
            "type": acc.get("type", "云函数"),
            "has_password": bool((acc.get("password") or "").strip()),
        })
    return {"ok": True, "items": out}


@router.post("/api/cf-debug/run")
async def post_run(req: RunReq):
    # 远程模式：若给了 server_account 且未显式传 token，则按 cf_accounts 登录取 token
    # （密码不出后端，前端只传 server_url/name）。
    server = req.server
    token = req.token
    if req.server_account and req.env in ("test", "custom") and not token:
        acc = _resolve_account(req.server_account)
        if not acc:
            return _err(400, f"未找到服务器账号: {req.server_account}")
        try:
            r = await cf_login_account(acc)
        except Exception as e:
            return _err(400, f"登录远程服务器失败: {e}")
        if not r.get("ok"):
            return _err(400, f"登录远程服务器失败: {r.get('message', '')}")
        server = (acc.get("server_url") or "").strip()
        token = r.get("token") or ""
    try:
        res = runner.start_session({**req.dict(), "server": server, "token": token})
        broadcast("cf_debug_log", {
            "session_id": res["session_id"], "level": "info",
            "msg": f"[session] 已启动调试会话 -> {res['file']} (env={res['env']})",
        })
        return {"ok": True, **res}
    except Exception as e:
        logger.warning("[cfdebug] start_session 失败: %s", e)
        return _err(400, str(e))


@router.post("/api/cf-debug/stop")
def post_stop(payload: Dict[str, Any]):
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    if not sid:
        return _err(400, "缺少 session_id")
    return runner.stop_session(sid)


@router.get("/api/cf-debug/sessions")
def get_sessions():
    return runner.list_sessions()


@router.websocket("/api/cf-debug/ws/{session_id}")
async def ws_dap(session_id: str, websocket: WebSocket):
    info = runner.get_session(session_id)
    if not info:
        await websocket.close(code=1008)
        return
    # 必须先 accept 才能完成 WebSocket 握手；否则 Starlette 会以 403 拒绝连接
    # （浏览器/前端 DAP 客户端会因此握手失败）。
    await websocket.accept()
    logger.info("[cfdebug] DAP 桥接建立 session=%s dap=%s:%s",
                session_id, info["dap_host"], info["dap_port"])
    await bridge_dap(websocket, info["dap_host"], info["dap_port"])
    # 桥接结束（前端 WS 断开）。若会话仍在（说明不是走正常 stop），兜底终止子进程。
    runner.orphan_guard(session_id)
