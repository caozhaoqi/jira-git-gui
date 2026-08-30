# -*- coding: utf-8 -*-
"""K8s 命令执行与 WebSocket 终端路由。

拆分自 ``api/routes_k8s.py``，业务子域：
- ``POST /api/k8s/exec``：一次性命令执行（**无 TTY**，用于脚本化取数，可断言退出码）；
- ``WS /ws/k8s/exec``：交互式终端（**真 PTY 常驻会话**，失败时降级为行缓冲）。

环境解析沿用 ``core.k8s.resolve_env_kubeconfig``。

交互式终端的两种模式
--------------------
1. **PTY 模式（默认）**：``core.k8s.exec_pty.spawn_kubectl_pty`` 用本地
   ``pty.fork()`` + ``kubectl exec -it`` 起一个常驻会话，键盘字节原样透传、
   窗口 resize 通过 ``TIOCSWINSZ`` → SIGWINCH 同步到远端，因此
   ``vim`` / ``top`` / ``htop`` / ``less`` 等全屏程序可正常渲染与交互。
   就绪时后端回 ``{"type":"ready","tty":true,"cwd":...}``。

2. **行缓冲模式（降级）**：PTY 不可用（无 kubectl / fork 失败 / 客户端 ``tty=0``）
   时自动回退。累积按键、遇回车执行一次 ``kubectl exec``（以 ``cd "<cwd>" &&``
   前缀保持工作目录连续），回 ``{"type":"ready"}``（**不带 tty**）。
   此模式**不支持**全屏程序，前端会显示本地假提示符。

前后端协议（务必与 ``frontend/web-react/src/components/k8s/K8sShell.tsx`` 保持一致）：
  - 前端→后端：``{type:"input", data}`` / ``{type:"resize", cols, rows}`` /
                ``{type:"disconnect"}``
  - 后端→前端：``{type:"ready", cwd, tty?}`` / ``{type:"output", data}`` /
                ``{type:"cwd", cwd}`` / ``{type:"error", msg}`` / ``{type:"exit", code}``
"""
import asyncio
import codecs
import json
import logging
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import k8s_manager as _k8s_mgr
from core.k8s.exec import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    READY_MARKER_RE,
    exec_command as _k8s_exec_command,
    interactive_command_hint,
    kubectl_available,
    spawn_kubectl_pty,
)

logger = logging.getLogger("api.routes_k8s_exec")
router = APIRouter()

#: 等待 PTY 会话就绪（kubectl 建连 + 远端 shell 启动 + 打印 READY 标记）的上限秒数。
#: 集群网络慢 / 镜像拉取慢时会偏大，但过长会让用户对着黑屏干等。
READY_TIMEOUT = 20.0

#: 从 PTY 早期输出里剥掉 ANSI 转义，便于把 kubectl 的报错原样呈现给用户
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[()][A-Z0-9]"
    r"|\x1b[=>]"
    r"|\r"
)


@router.post("/api/k8s/exec")
async def api_k8s_exec(body: dict):
    """在指定 Pod 容器中执行命令（一次性）。

    请求体（application/json）::

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

    # 全屏/交互式程序走一次性通道必然「无反馈」，提前拦截并给出可读指引，
    # 免得用户等满 60s 只拿到一句 TimeoutExpired。
    hint = interactive_command_hint(command)
    if hint:
        return {"ok": False, "error": hint, "hint": True}

    # 一次性执行：core.k8s.exec.exec_command 返回 (clean_output, new_cwd)
    try:
        out, new_cwd = _k8s_exec_command(
            env, pod, container, namespace, command, timeout=60,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True, "stdout": out, "stderr": "", "rc": 0, "cwd": new_cwd}


# --------------------------------------------------------------------------- #
# PTY 模式
# --------------------------------------------------------------------------- #
def _pty_wanted(websocket: WebSocket) -> bool:
    """客户端是否要真 PTY。``?tty=0`` 可强制降级为行缓冲（排障用）。"""
    raw = (websocket.query_params.get("tty") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _initial_size(websocket: WebSocket):
    """客户端首屏窗口尺寸（``?cols=&rows=``）。

    前端连接时就把 xterm 的列数/行数带上，让远端启动脚本的 ``stty`` 直接用真实
    尺寸，避免「先按 80x24 起来、再被 resize 追着改」导致的首屏错位。
    缺省或越界（异常客户端/手改 URL）时回退默认值。
    """
    def _int(key, default, lo, hi):
        try:
            val = int(str(websocket.query_params.get(key) or "").strip())
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    return (_int("cols", DEFAULT_COLS, 20, 500),
            _int("rows", DEFAULT_ROWS, 5, 200))


def _pty_error_detail(buf: str, limit: int = 400) -> str:
    """把 PTY 早期输出整理成一句人类可读的失败原因。"""
    text = _ANSI_RE.sub(" ", buf or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ("kubectl exec 会话立即退出且未输出任何内容。"
                "请检查 Pod/容器是否存在、kubectl 能否连通集群。")
    tail = "\n".join(lines[-6:])
    if len(tail) > limit:
        tail = "…" + tail[-limit:]
    return "无法建立终端会话：\n" + tail


async def _pty_await_ready(sess, timeout: float = READY_TIMEOUT):
    """等待远端 shell 打印 READY 标记。

    返回 ``(ok, cwd, pending_text, reason)``：
      * ``ok=True`` 时 ``cwd`` 为远端真实起始目录，``pending_text`` 是**已解码的
        str**，即 READY 标记之后残留在同一批输出里的首屏内容（须在 ``ready``
        之后补发给前端，否则会丢首屏）；
      * ``ok=False`` 时 ``reason`` 是给用户看的失败原因。

    .. note::
       ``pending_text`` 必须是 **str** 而不是 bytes。``_pty_pump`` 里的增量解码器
       只吃 bytes；早期实现在这里返回了已解码的 str，``decoder.decode(str)`` 抛出
       ``TypeError``，导致 pump 起不来 —— 表现为「终端显示已连接，但敲任何命令
       都没有反馈」。只要 READY 标记与首屏输出落在同一批读取里就必现。
    """
    loop = asyncio.get_running_loop()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    deadline = loop.time() + timeout
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            return False, "/", "", "等待终端会话就绪超时（%.0fs）" % timeout
        chunk = await sess.read(timeout=remain)
        if chunk is None:
            # EOF：kubectl 已退出，多半是 Pod/容器不存在或连不上集群
            return False, "/", "", _pty_error_detail(buf)
        if not chunk:
            continue
        buf += decoder.decode(chunk)
        m = READY_MARKER_RE.search(buf)
        if m:
            cwd = (m.group(1) or "").strip() or "/"
            return True, cwd, buf[m.end():], ""


async def _pty_pump(websocket: WebSocket, sess, initial: str = ""):
    """PTY 会话的主循环：双向搬运字节，直到任一端断开。

    ``initial`` 是 READY 标记之后残留的首屏输出，必须是**已解码的 str**
    （与 ``_pty_await_ready`` 的返回保持一致）。
    """
    loop = asyncio.get_running_loop()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    async def _out():
        while True:
            chunk = await sess.read()
            if chunk is None:      # EOF：远端 shell / kubectl 退出
                # 给一句明确的收尾，否则用户只看到光标卡住、以为又「没反馈」
                await websocket.send_json(
                    {"type": "output", "data": "\r\n— 会话已结束 —\r\n"})
                return
            if not chunk:
                continue
            await websocket.send_json({"type": "output", "data": decoder.decode(chunk)})

    async def _in():
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            ptype = payload.get("type")
            if ptype == "input":
                # 原样透传：不过滤控制字符，vim 的转义序列依赖这些字节
                sess.write(payload.get("data", ""))
            elif ptype == "resize":
                sess.resize(payload.get("cols"), payload.get("rows"))
            elif ptype == "disconnect":
                return

    out_task = asyncio.ensure_future(_out())
    in_task = asyncio.ensure_future(_in())
    try:
        if initial:
            # READY 标记之后残留的首屏输出（远端 prompt 等），先补发给前端
            await websocket.send_json({"type": "output", "data": initial})
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (out_task, in_task):
            if not t.done():
                t.cancel()
        for t in (out_task, in_task):
            try:
                await t
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception as ex:  # 断开后的收尾异常不该冒泡
                logger.debug("PTY 泵协程结束: %s", ex)
        # close() 里有 join(1.0)，丢线程池避免阻塞事件循环
        try:
            await loop.run_in_executor(None, sess.close)
        except Exception:
            pass
        try:
            await websocket.send_json({"type": "exit", "code": 0})
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 行缓冲降级模式
# --------------------------------------------------------------------------- #
async def _ws_k8s_exec_line(websocket: WebSocket, env: str, namespace: str,
                            pod: str, container: str, notice: str = ""):
    """降级终端：无 PTY，逐行执行一次 ``kubectl exec``。

    不支持 vim / top 等全屏程序，仅保证 ``cd``、相对路径、简单命令可用。
    ``notice`` 不为空时，在 ``ready`` 之后回一条 ``error``，向用户解释为何降级。
    """
    cwd_ref = ["/"]
    line: list = []
    skip_esc = False   # 跨帧保持，避免把方向键等转义序列误当作输入回显
    esc_len = 0
    await websocket.send_json({"type": "ready", "cwd": cwd_ref[0]})
    if notice:
        await websocket.send_json({"type": "error", "msg": notice})

    async def _emit_cmd(cmd: str):
        # 降级模式没有 TTY：vim/top 这类全屏程序执行下去要么立刻退出、要么挂到
        # 60s 超时，输出还被丢弃 —— 用户看到的就是「敲了没反应」。这里直接拦下并
        # 说明原因，比静默失败好排查得多。
        if interactive_command_hint(cmd):
            await websocket.send_json({
                "type": "error",
                "msg": "「%s」是全屏/交互式程序，当前行缓冲降级模式没有 TTY，"
                       "无法渲染也不会转发键盘输入。请安装 kubectl 后重连，"
                       "即可启用真正的 PTY 终端。" % cmd.strip().split()[0],
            })
            await websocket.send_json({"type": "output", "data": "\r\n"})
            await websocket.send_json({"type": "cwd", "cwd": cwd_ref[0]})
            return
        try:
            out, new_cwd = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _k8s_exec_command(
                    env, pod, container, namespace, cmd, cwd=cwd_ref[0], timeout=60,
                ),
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


# --------------------------------------------------------------------------- #
# 入口：先 PTY，不行再降级
# --------------------------------------------------------------------------- #
async def _ws_k8s_exec_tty(websocket: WebSocket, env: str, namespace: str,
                           pod: str, container: str, env_vars: dict):
    """WebSocket 终端：优先真 PTY（支持 vim/top），不可用时降级行缓冲。"""
    await websocket.accept()

    sess = None
    reason = ""
    if _pty_wanted(websocket):
        if not kubectl_available():
            reason = ("未找到 kubectl 可执行文件，已降级为行缓冲模式"
                      "（不支持 vim / top 等全屏程序）。")
        else:
            cols, rows = _initial_size(websocket)
            try:
                sess = spawn_kubectl_pty(
                    env, pod, container or None, namespace or None,
                    cwd="/", cols=cols, rows=rows,
                    loop=asyncio.get_running_loop(),
                )
            except Exception as ex:
                sess = None
                reason = "启动 PTY 会话失败（%s），已降级为行缓冲模式。" % ex
    else:
        reason = "客户端指定 tty=0，使用行缓冲模式。"

    if sess is not None:
        try:
            ok, cwd, pending, reason = await _pty_await_ready(sess)
        except Exception as ex:
            ok, cwd, pending, reason = False, "/", "", "建立终端会话异常：%s" % ex
            logger.warning("PTY 会话就绪等待异常 pod=%s: %s", pod, ex)
        if ok:
            await websocket.send_json({"type": "ready", "tty": True, "cwd": cwd})
            await _pty_pump(websocket, sess, pending)
            return
        # 就绪失败：收掉半死的会话，把原因带给降级模式
        try:
            await asyncio.get_running_loop().run_in_executor(None, sess.close)
        except Exception:
            pass
        sess = None
        reason = reason or "终端会话未就绪，已降级为行缓冲模式。"

    await _ws_k8s_exec_line(websocket, env, namespace, pod, container, notice=reason)


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
