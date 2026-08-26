# -*- coding: utf-8 -*-
"""SSE 事件流路由（日志 / 进度 / 任务完成推送）。"""
import asyncio
import json
import time

from fastapi import Request
from fastapi.responses import StreamingResponse

from fastapi import APIRouter
from api.common import app, subscribe, unsubscribe

router = APIRouter()


@router.get("/api/events")
async def api_events(request: Request):
    """SSE 端点：推送日志、进度、任务完成事件。"""
    q = subscribe()

    async def event_stream():
        try:
            yield f"event: ready\ndata: {json.dumps({'status': 'connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {json.dumps({'ts': time.time()})}\n\n"
        finally:
            unsubscribe(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
