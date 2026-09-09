# -*- coding: utf-8 -*-
"""
云函数调试：DAP 桥接（api/cfdebug/dap_bridge.py）
================================================================
浏览器无法直连 debugpy 的裸 TCP DAP 端口（WebSocket 握手指令不同），
因此后端在这里做「WebSocket ⇄ TCP」双向桥：

  - 浏览器  --ws-->  FastAPI WebSocket(/api/cf-debug/ws/{sid})  --tcp-->  debugpy DAP
  - debugpy DAP --tcp--> 后端  --ws(text JSON)-->  浏览器 DAP 客户端

DAP 在裸 TCP 上用 `Content-Length: N\r\n\r\n{json}` 分帧；在 WebSocket 上我们
直接以「一条文本帧 = 一个 JSON 消息」传输，省去帧头解析。
"""
import asyncio
import json
from typing import Any

import anyio  # FastAPI/Starlette 自带依赖


async def _read_dap_message(reader: asyncio.StreamReader) -> Any:
    """从裸 TCP 读取一个 DAP 分帧消息并解析为 dict。"""
    # 读头部，直到空行
    headers: bytes = b""
    while b"\r\n\r\n" not in headers:
        chunk = await reader.read(1)
        if not chunk:
            raise ConnectionError("DAP 连接已关闭（读头部）")
        headers += chunk
    header_text = headers.decode("utf-8", "replace")
    length = 0
    for line in header_text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    if length <= 0:
        raise ConnectionError("非法 DAP 头部")
    body = b""
    while len(body) < length:
        chunk = await reader.read(length - len(body))
        if not chunk:
            raise ConnectionError("DAP 连接已关闭（读 body）")
        body += chunk
    return json.loads(body.decode("utf-8", "replace"))


def _frame(msg: Any) -> bytes:
    body = json.dumps(msg, ensure_ascii=False, default=str).encode("utf-8")
    return (f"Content-Length: {len(body)}\r\n\r\n").encode("utf-8") + body


async def bridge_dap(websocket, dap_host: str, dap_port: int) -> None:
    """建立到 debugpy 的 TCP 连接，并在 WebSocket 与 TCP 之间双向搬运 DAP 消息。"""
    # 调试会话刚启动时 debugpy 可能尚未完成端口绑定，这里做有限次重试
    last_err: Exception = None
    for attempt in range(25):
        try:
            reader, writer = await asyncio.open_connection(dap_host, dap_port)
            last_err = None
            break
        except Exception as e:  # 连接被拒：等一会再试
            last_err = e
            await asyncio.sleep(0.2)
    if last_err is not None:
        # 无法连上 debugpy：关闭 WebSocket 并让前端提示
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return
    try:
        async with anyio.create_task_group() as tg:

            async def ws_to_tcp():
                # 浏览器 -> debugpy
                try:
                    while True:
                        data = await websocket.receive_text()
                        try:
                            msg = json.loads(data)
                        except Exception:
                            continue
                        writer.write(_frame(msg))
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            async def tcp_to_ws():
                # debugpy -> 浏览器
                try:
                    while True:
                        msg = await _read_dap_message(reader)
                        await websocket.send_text(
                            json.dumps(msg, ensure_ascii=False, default=str)
                        )
                except Exception:
                    pass
                finally:
                    try:
                        await websocket.close()
                    except Exception:
                        pass

            tg.start_soon(ws_to_tcp)
            tg.start_soon(tcp_to_ws)
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

