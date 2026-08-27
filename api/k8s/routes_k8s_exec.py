# -*- coding: utf-8 -*-
"""K8s 命令执行与 WebSocket 终端路由。

拆分自 ``api/routes_k8s.py``，业务子域：
- ``POST /api/k8s/exec``：一次性命令执行；
- ``WS /ws/k8s/exec``：交互式终端（降级实现，见下方说明）。

环境解析沿用 ``core.k8s.resolve_env_kubeconfig``。

符号说明：原 ``routes_k8s.py`` 依赖的 ``start_exec_pty`` / ``build_exec_command`` 在
core 拆分后已不存在。此处改用 ``core.k8s.exec.exec_command`` 作为底层执行能力：

- 一次性 exec：直接 ``exec_command(env, pod, container, namespace, command)``，
  返回 ``(clean_output, new_cwd)``；
- 交互式 WebSocket：core 当前无 PTY 常驻能力，降级为「行缓冲 + 单条命令一次性执行」。
  前端 ``K8sShell.tsx`` 逐键发送 ``{type:"input", data}``，后端回显、遇回车执行整行
  （每次 exec 以 ``cd "<cwd>" &&`` 前缀保持工作目录连续），再回 ``{type:"output"}`` /
  ``{type:"cwd"}`` / ``{type:"error"}``。协议与前端严格对齐，真正的 PTY 行编辑 / resize
  需在 core.k8s.exec 补充常驻 PTY 能力后再启用。
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

    # 一次性执行：core.k8s.exec.exec_command 返回 (clean_output, new_cwd)
    try:
        out, new_cwd = _k8s_exec_command(
            env, pod, container, namespace, command, timeout=60,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True, "stdout": out, "stderr": "", "rc": 0, "cwd": new_cwd}


async def _ws_k8s_exec_tty(websocket: WebSocket, env: str, namespace: str,
                           pod: str, container: str, env_vars: dict):
    """WebSocket 终端（降级版，但协议与前端 K8sShell 对齐）。

    前端 ``K8sShell.tsx`` 通过 ``term.onData`` 把每个按键以 ``{type:"input", data}`` 帧
    发来，并期望后端回 ``{type:"output"}`` / ``{type:"cwd"}`` / ``{type:"error"}``。

    core 当前无 PTY 常驻能力，因此做**行缓冲降级**：累积输入字符，遇到回车执行整行命令
    （每次 exec 以 ``cd "<cwd>" &&`` 前缀保持工作目录连续），把输出与新的 cwd 回传，
    从而让简单的命令行交互（含 ``cd`` / 相对路径）可用；并支持退格、Ctrl-C、忽略方向键等
    ANSI 转义序列。

    协议（务必与 frontend/web-react/src/components/k8s/K8sShell.tsx 保持一致）：
      - 前端→后端：``{type:"input", data}`` / ``{type:"resize"}``(忽略) / ``{type:"disconnect"}``
      - 后端→前端：``{type:"ready", cwd}`` / ``{type:"output", data}`` / ``{type:"cwd", cwd}`` /
                    ``{type:"error", msg}`` / ``{type:"exit", code}``
    """
    await websocket.accept()
    cwd_ref = ["/"]
    line: list = []
    skip_esc = False   # 跨帧保持，避免把方向键等转义序列误当作输入回显
    esc_len = 0
    await websocket.send_json({"type": "ready", "cwd": cwd_ref[0]})

    async def _emit_cmd(cmd: str):
        try:
            out, new_cwd = _k8s_exec_command(
                env, pod, container, namespace, cmd, cwd=cwd_ref[0], timeout=60,
            )
        except Exception as ex:
            msg = getattr(ex, "message", None) or str(ex)
            await websocket.send_json({"type": "error", "msg": msg})
            await websocket.send_json({"type": "output", "data": "\r\n"})
            await websocket.send_json({"type": "cwd", "cwd": cwd_ref[0]})
            return
        if new_cwd:
            cwd_ref[0] = new_cwd
        if out:
            await websocket.send_json({"type": "output", "data": out.rstrip("\r\n")})
        await websocket.send_json({"type": "cwd", "cwd": cwd_ref[0]})

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            ptype = payload.get("type")
            if ptype == "disconnect":
                break
            if ptype == "resize":
                continue  # 降级模式无 PTY，resize 忽略
            if ptype != "input":
                continue
            data = payload.get("data", "")
            for ch in data:
                if ch == "\x1b":
                    skip_esc = True
                    esc_len = 1
                    continue
                if skip_esc:
                    esc_len += 1
                    if ch.isalpha() or ch == "~":
                        skip_esc = False
                    elif esc_len > 16:  # 兜底：畸形转义序列，避免吞掉后续输入
                        skip_esc = False
                    continue
                if ch in ("\r", "\n"):
                    cmd = "".join(line)
                    line = []
                    await websocket.send_json({"type": "output", "data": "\r\n"})
                    if cmd.strip():
                        await _emit_cmd(cmd)
                    else:
                        await websocket.send_json({"type": "cwd", "cwd": cwd_ref[0]})
                    continue
                if ch in ("\x7f", "\b"):  # DEL / BS
                    if line:
                        line.pop()
                        await websocket.send_json({"type": "output", "data": "\b \b"})
                    continue
                if ch == "\x03":  # Ctrl-C：放弃当前行
                    line = []
                    await websocket.send_json({"type": "output", "data": "^C\r\n"})
                    await websocket.send_json({"type": "cwd", "cwd": cwd_ref[0]})
                    continue
                if ch.isprintable() or ch == "\t":
                    line.append(ch)
                    await websocket.send_json({"type": "output", "data": ch})
                # 其它控制字符忽略
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
