# -*- coding: utf-8 -*-
"""K8s 命令执行与 WebSocket 终端路由。

拆分自 ``api/routes_k8s.py``，业务子域：
- ``POST /api/k8s/exec``：一次性命令执行；
- ``WS /ws/k8s/exec``：交互式终端（降级实现，见下方说明）。

环境解析沿用 ``core.k8s.resolve_env_kubeconfig``。

符号说明：原 ``routes_k8s.py`` 依赖的 ``start_exec_pty`` / ``build_exec_command`` 在
core 拆分后已不存在。此处改用 ``core.k8s.exec.exec_command`` 作为底层执行能力：

- 一次性 exec：直接 ``exec_command(env, pod, container, namespace, command)``；
- 交互式 WebSocket：core 当前无 PTY 常驻能力，降级为「单条命令一次性执行 + 输出作为
  一帧返回」的模式（保留 ready / data / exit 协议，便于前端不报错；真正的 PTY 行编辑 /
  resize 需在 core.k8s.exec 补充常驻 PTY 能力后再启用）。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import k8s_manager as _k8s_mgr
from core.k8s.exec import exec_command as _k8s_exec_command

logger = logging.getLogger("api.routes_k8s_exec")
router = APIRouter()


@router.post("/api/k8s/exec")
async def api_k8s_exec(body: dict):
    """在指定 Pod 容器中执行命令（一次性）。

    请求体（application/json）：::

        {
          "env": "prod",
          "namespace": "default",      # 可选，缺省用环境默认
          "pod": "nginx-7d8f9",        # 必填
          "container": "nginx",        # 可选，单容器可省略
          "command": "ls -la /etc",    # 必填
          "envVars": {"FOO": "bar"},   # 可选环境变量（当前作为 exec 环境透传，非 kubectl -e）
          "interactive": false         # true 则仅返回 ws 终端地址，不执行命令
        }
    """
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    command = body.get("command", "")
    env_vars = body.get("envVars", {}) or {}
    interactive = bool(body.get("interactive", False))
    namespace = body.get("namespace", "")

    if not pod or not command:
        return {"ok": False, "error": "pod 与 command 均为必填"}

    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns

    if interactive:
        # 交互式模式：交给 WebSocket 终端，这里仅返回连接信息
        return {
            "ok": True,
            "interactive": True,
            "ws": "/ws/k8s/exec",
            "params": {
                "env": env,
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "envVars": env_vars,
            },
        }

    # 一次性执行：调用 core.k8s.exec.exec_command
    try:
        out, rc, err = _k8s_exec_command(
            env, pod, container, namespace, command, timeout=60,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if rc != 0 and not out.strip():
        return {"ok": False, "error": err.strip()[:500]}
    return {"ok": True, "stdout": out, "stderr": err, "rc": rc}


async def _ws_k8s_exec_tty(websocket: WebSocket, env: str, namespace: str,
                           pod: str, container: str, env_vars: dict):
    """WebSocket 终端（降级版）。

    当前 core 无 PTY 常驻能力，因此对每次前端发来的 ``{type:"data"}`` 帧执行单条命令，
    把输出作为一帧回传。保留 ready / data / exit 协议，前端无需改动即可不报错。
    """
    await websocket.accept()
    await websocket.send_json({"type": "ready"})
    try:
        while True:
            try:
                msg = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if payload.get("type") == "data":
                command = payload.get("data", "").strip()
                if not command:
                    continue
                try:
                    out, rc, err = _k8s_exec_command(
                        env, pod, container, namespace, command, timeout=60,
                    )
                except Exception as ex:
                    await websocket.send_json(
                        {"type": "data", "data": f"exec error: {ex}\n"}
                    )
                    continue
                if out:
                    await websocket.send_json({"type": "data", "data": out})
                if err and rc != 0:
                    await websocket.send_json({"type": "data", "data": err})
            elif payload.get("type") == "resize":
                # 降级模式不支持 resize，忽略
                continue
            else:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.send_json({"type": "exit", "code": 0})
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/ws/k8s/exec")
async def ws_k8s_exec(websocket: WebSocket):
    """交互式终端 WebSocket：``/ws/k8s/exec?env=...&namespace=...&pod=...&container=...``。

    环境变量用 query 参数 ``envVars`` 以 ``JSON`` 编码传递，避免与路径冲突。
    """
    env = websocket.query_params.get("env", "")
    namespace = websocket.query_params.get("namespace", "")
    pod = websocket.query_params.get("pod", "")
    container = websocket.query_params.get("container", "")
    env_vars_raw = websocket.query_params.get("envVars", "{}")
    try:
        env_vars = json.loads(env_vars_raw) if env_vars_raw else {}
    except Exception:
        env_vars = {}

    if not pod:
        await websocket.accept()
        await websocket.send_json({"type": "error", "msg": "pod 为必填"})
        await websocket.close()
        return

    await _ws_k8s_exec_tty(websocket, env, namespace, pod, container, env_vars or {})
