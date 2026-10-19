# -*- coding: utf-8 -*-
"""HCM 网关 WebSocket 调试通道（对齐平台调试页的「WS」按钮）。

协议来源
--------
从网关自带调试页的前端代码（``static/api/api.js`` 的 ``callHcmOpenWebSocket``）
逆向 + 对现网网关实测确认：

- URL：``ws(s)://<网关 host[:port]><网关路径前缀>/ws/<api_name>``
  （http → ws、https → wss；保留端口与路径前缀，实测 21qor 前缀为空）。
- 握手鉴权：``Cookie: token=<token>``，与 REST ``/api/<name>`` 同款登录态。
- 首帧：客户端 ``send(JSON.stringify(业务参数))``——**明文业务参数**，
  不像 REST 那样包 ``{hcm_transfer_strategy, hcm_param, s3h}`` 加密信封。
- 回包：``{"status": "notify" | "success" | "error", "data": ...}``
  - ``notify``：进度消息（``data == "pong"`` 是心跳），长任务可有多条；
  - ``success`` / ``error``：终态，随后网关主动关闭连接（一次性会话）。
- token 失效时首包即 ``{"status": "error", "data": "请先登录"}``（17003 的
  WS 形态）；bhcd 网关对无效/外来 token 的兜底文案是
  ``system_errors_msg_00000055``（现网实测）。两种形态都支持用 cf_accounts
  账密自动重登重试一次（ws_call）/ 重建连接重发首帧（ws_proxy）。
- 只有声明了 notify 回调的 handler 支持 WS（如 ``hcm.paas.object.list``、
  导出 / 批量审批类长任务）；不支持的接口回
  ``"xxx() got an unexpected keyword argument 'notify'"``。

依赖：``websockets``（已随 venv 安装；同时声明进 requirements.txt 与
PyInstaller spec 的 hiddenimports——v0.0.9 曾因漏声明冻结后端起不来）。
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit, urlunsplit

import websockets

from api.common import logger
from api.hcm.hcm_core import _HCM_PROXY_TARGET, _HCM_PRESET_TOKEN, _auto_relogin_token

# 终态 status（收到后网关即关闭连接）
_WS_TERMINAL = ("success", "error")
# 无 token 时的登录态错误文案（网关 WS 通道的 17003 等价物）
_WS_LOGIN_HINT = "请先登录"
# bhcd 网关对「无效/外来 token」的 WS 兜底码：REST 同场景回 errcode 17003，
# WS 通道无 errode 结构、直接回该系统错误文案 key（现网实测：外来 token 首包
# 即此码，带有效 token 同参数正常 success）。
_WS_LOGIN_SYS_ERR = "system_errors_msg_00000055"


def build_ws_url(target: str, api_name: str, extra_query: str = "") -> str:
    """由网关 base_url 推导 WS 调试通道 URL。

    ``https://..com:5206`` + ``hcm.paas.object.list``
    → ``wss://..com:5206/ws/hcm.paas.object.list``

    带路径前缀的目标（如 ``http://gw/hcm``）会保留前缀：``ws://gw/hcm/ws/<api>``。
    """
    t = (target or "").strip().rstrip("/")
    if not t:
        raise ValueError("WS 目标未配置（hcm_whitelist.local.json 的 proxy_target.base_url，或请求传入 target）")
    api = (api_name or "").strip()
    if not api:
        raise ValueError("WS 调用缺少 api_name")
    parts = urlsplit(t)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = (parts.path.rstrip("/") + "/ws/" + api) if parts.path else "/ws/" + api
    query = extra_query.lstrip("?&")
    return urlunsplit((scheme, parts.netloc, path, query, ""))


def _resolve_token(token: str) -> str:
    tok = (token or "").strip() or _HCM_PRESET_TOKEN
    if not tok:
        raise PermissionError("未提供 token 且预设 token 为空，请先配置 token 或传入 token")
    return tok


def _is_login_error(status: str, data) -> bool:
    """判定 WS 回包是否为登录态失效。"""
    if status != "error":
        return False
    if data == _WS_LOGIN_HINT:
        return True
    if isinstance(data, str) and _WS_LOGIN_HINT in data:
        return True
    if isinstance(data, str) and _WS_LOGIN_SYS_ERR in data:
        return True
    if isinstance(data, dict):
        code = data.get("errcode") or data.get("code")
        return code in (17003, 51006)
    return False


def _frame_is_login_error(raw: str) -> bool:
    """判定网关回包帧（原文）是否为登录态失效（ws_proxy 重登重建用）。"""
    try:
        msg = json.loads(raw)
    except Exception:
        return False
    if not isinstance(msg, dict):
        return False
    return _is_login_error(str(msg.get("status") or ""), msg.get("data"))


async def _ws_call_once(url: str, params, token: str, open_timeout: float,
                       recv_timeout: float) -> dict:
    """单次 WS 调用：连接 → 发参数 → 收包至终态。"""
    messages: list = []
    t0 = time.monotonic()
    ssl_ctx = None
    if url.startswith("wss://"):
        # 复用 CF 共享的 SSL 上下文（开启旧式重协商），与 hcm_direct 一致
        from api.cf.cf_tokens import _cf_ssl_context
        ssl_ctx = _cf_ssl_context()
    async with websockets.connect(
        url,
        additional_headers={"Cookie": f"token={token}"},
        open_timeout=open_timeout,
        ssl=ssl_ctx,
        proxy=None,  # 与 hcm_direct 一致：直连网关，绝不走环境代理
    ) as ws:
        await ws.send(json.dumps(params, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            try:
                msg = json.loads(raw)
            except Exception:
                # 非 JSON 帧（网关异常文本）：按 error 终态处理，保留原文
                return {"status": "error", "data": raw, "messages": messages,
                        "duration_ms": int((time.monotonic() - t0) * 1000)}
            status = str(msg.get("status") or "")
            data = msg.get("data")
            if status == "notify":
                if data != "pong":  # 心跳不入列表
                    messages.append(data)
                continue
            return {"status": status or "error", "data": data, "messages": messages,
                    "duration_ms": int((time.monotonic() - t0) * 1000)}


async def ws_call(target: str, api_name: str, params, token: str = "",
                  open_timeout: float = 15.0, recv_timeout: float = 300.0,
                  allow_relogin: bool = True) -> dict:
    """一次性 WS 调试调用（对齐调试页「WS」按钮的典型用法）。

    返回 ``{ok, status, data, messages, gateway, duration_ms}``；
    ``messages`` 为 notify 进度列表。token 失效自动重登重试一次。
    """
    effective_target = (target or "").strip() or _HCM_PROXY_TARGET
    if not effective_target:
        raise ValueError("WS 目标未配置（hcm_whitelist.local.json 的 proxy_target.base_url，或请求传入 target）")
    tok = _resolve_token(token)
    url = build_ws_url(effective_target, api_name)
    logger.info("[HCM-WS] %s → %s", api_name, url)

    try:
        r = await _ws_call_once(url, params, tok, open_timeout, recv_timeout)
    except PermissionError:
        raise
    except Exception as e:
        # 连接层失败（DNS / 拒绝 / 超时）：不重试，直接抛给路由层转 502/504
        logger.error("[HCM-WS] 连接失败 %s: %s", url, e)
        raise

    if allow_relogin and _is_login_error(r.get("status", ""), r.get("data")):
        new_tok = await _auto_relogin_token(effective_target)
        if new_tok and new_tok != tok:
            logger.info("[HCM-WS] 登录态失效，自动重登后重试 %s", api_name)
            r = await _ws_call_once(url, params, new_tok, open_timeout, recv_timeout)

    r.update({"ok": r.get("status") == "success", "gateway": effective_target})
    return r


async def _proxy_bridge(local_ws, url: str, tok: str, first: str, ssl_ctx):
    """单次流式桥接：连网关 → 发首帧 → 双向泵。

    返回 ``(outcome, login_frame)``：
    - ``("ok", None)``：正常结束（任一侧断开 / 终态后网关关闭）；
    - ``("login_error", frame)``：网关回包为登录态失效（该帧**未**转发给本地，
      由外层决定重登重建或透传）；
    - ``("conn_error", None)``：连接网关失败（已 close 本地 4404）。
    """
    login_frame = None
    try:
        async with websockets.connect(
            url,
            additional_headers={"Cookie": f"token={tok}"} if tok else {},
            open_timeout=15,
            ssl=ssl_ctx,
            proxy=None,  # 与 hcm_direct 一致：直连网关，绝不走环境代理
        ) as gw:
            # 首帧：本地客户端发来的业务参数（调试页 onopen 后 send 的同款数据）
            await gw.send(first)

            async def pump_gw_to_local():
                nonlocal login_frame
                try:
                    async for msg in gw:
                        # 登录态失效帧：先不转发，交外层重登重建（对齐 REST 的
                        # 自动重登体验：预设 token 属另一网关时用户无感知）。
                        # 真业务错误（如 notify 参数错误）形态不同，正常透传。
                        if _frame_is_login_error(msg):
                            login_frame = msg
                            return
                        await local_ws.send_text(msg)
                except Exception:
                    pass  # 网关关闭（终态后正常）→ 结束本地连接

            async def pump_local_to_gw():
                try:
                    while True:
                        await gw.send(await local_ws.receive_text())
                except Exception:
                    pass  # 本地断开 → 关闭网关连接

            tasks = [asyncio.create_task(pump_gw_to_local()),
                     asyncio.create_task(pump_local_to_gw())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
            if login_frame is not None:
                return "login_error", login_frame
            return "ok", None
    except Exception as e:
        logger.error("[HCM-WS][proxy] 桥接失败 %s: %s", url, e)
        try:
            await local_ws.close(code=4404, reason=f"gateway ws error: {type(e).__name__}")
        except Exception:
            pass
        return "conn_error", None


async def ws_proxy(local_ws, target: str, api_name: str, token: str = "",
                   allow_relogin: bool = True) -> None:
    """流式 WS 代理：本地客户端 ↔ 网关 WS 双向桥接。

    - 本地首帧（业务参数）原样转发给网关（调试页同款语义）；
    - 网关回包（notify 进度 / success / error 终态）实时推给本地；
    - 任一侧断开即整体关闭（网关终态后本就会关，属正常结束）；
    - 登录态失效（请先登录 / 17003 / 51006 / bhcd 的 system_errors_msg_00000055）
      自动用 cf_accounts 账密重登并**重建连接重发首帧**（首帧已从本地消费，
      缓存在后重用），本地客户端无感知；重登不可用（无账密/失败）时才把
      网关错误帧透传给本地——与 hcm_direct 的自动重登策略对齐。
    """
    effective_target = (target or "").strip() or _HCM_PROXY_TARGET
    if not effective_target:
        await local_ws.close(code=4400, reason="WS 目标未配置")
        return
    tok = (token or "").strip() or _HCM_PRESET_TOKEN
    url = build_ws_url(effective_target, api_name)
    logger.info("[HCM-WS][proxy] %s → %s", api_name, url)

    ssl_ctx = None
    if url.startswith("wss://"):
        from api.cf.cf_tokens import _cf_ssl_context
        ssl_ctx = _cf_ssl_context()

    # 本地首帧（业务参数）先缓存：自动重登重建时要原样重发
    try:
        first = await local_ws.receive_text()
    except Exception:
        return  # 本地已断开

    outcome, login_frame = await _proxy_bridge(local_ws, url, tok, first, ssl_ctx)
    if outcome == "login_error" and allow_relogin:
        new_tok = await _auto_relogin_token(effective_target)
        if new_tok and new_tok != tok:
            logger.info("[HCM-WS][proxy] 登录态失效，自动重登后重建 %s", api_name)
            outcome, login_frame = await _proxy_bridge(
                local_ws, url, new_tok, first, ssl_ctx)
        elif login_frame:
            # 无账密或重登失败：把网关的登录错误原样透传给本地后关闭
            try:
                await local_ws.send_text(login_frame)
                await local_ws.close()
            except Exception:
                pass
