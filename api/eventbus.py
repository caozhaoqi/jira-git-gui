"""全局 SSE 事件总线（跨模块、跨线程安全）。

为什么独立成模块：
    api.server 通过 ``python -m api.server`` 启动时，解释器会把它以
    ``__name__ == "__main__"`` 的形式加载，而其它模块（如 routes_k8s）
    以 ``import api.server as S`` 加载到的却是 ``__name__ == "api.server"``
    的**另一个模块实例**。两个实例各有独立的一份模块级全局变量，导致
    routes_k8s 里 ``S._broadcast`` 广播进了“空的事件总线”，SSE 订阅者
    （挂在 __main__ 实例上）永远收不到 k8s 进度 / 完成事件。

    把事件总线放到本模块后，无论以何种方式导入，``api.eventbus`` 都只会
    存在唯一一份实例，事件总线不再分叉。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from asyncio import QueueEmpty
from typing import Any, Optional

logger = logging.getLogger("api.eventbus")

# 所有 SSE 订阅者共享的队列列表
_subscribers: list[asyncio.Queue] = []
_lock = threading.Lock()
# 主事件循环引用：供工作线程通过 call_soon_threadsafe 安全唤醒循环
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def capture_loop() -> None:
    """在应用启动钩子中调用，捕获主事件循环。"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    logger.info("事件总线主循环已捕获")


def subscribe() -> asyncio.Queue:
    """注册一个 SSE 订阅者队列并返回该队列。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """移除一个 SSE 订阅者队列。"""
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


# 必达事件：这些是用户必须看到的告警（Cookie 失效 / 网络中断），
# 若被高频进度事件（scan_stage/merge_progress/k8s 快照进度…）以「丢最旧」
# 的方式挤出队列，用户会错过关键提示——比如 Cookie 已过期却还以为在正常扫描。
_CRITICAL_EVENTS = frozenset({"cookie_expired", "network_warning"})


def _is_critical(item: dict) -> bool:
    return item.get("event") in _CRITICAL_EVENTS


def _enqueue(q: asyncio.Queue, item: dict) -> None:
    """在事件循环线程中执行入队（唤醒 await q.get() 的消费者）。

    队列满时的丢弃策略按事件重要性区分：
    - 普通事件：丢最旧的一条腾位（维持原有「不阻塞生产者」语义）；
    - 关键事件（cookie_expired / network_warning）：优先挤出一条**最旧的普通
      事件**来腾位；队列里全是关键事件时才丢最旧的关键事件。
      这样高频进度风暴不会把告警挤掉。
    """
    try:
        q.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass

    if not _is_critical(item):
        try:
            q.get_nowait()
        except QueueEmpty:
            return
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass
        return

    # 关键事件：把队列整个倒出来，挤掉一条最旧的普通事件后原样放回。
    # call_soon_threadsafe 调度的回调在同一循环线程内串行执行，中途无并发竞争。
    drained: list[dict] = []
    spare: Optional[dict] = None
    while True:
        try:
            old = q.get_nowait()
        except QueueEmpty:
            break
        if spare is None and not _is_critical(old):
            spare = old
        else:
            drained.append(old)
    if spare is None and drained:
        drained.pop(0)  # 队列里全是关键事件：丢最旧的，保证新告警能入队
    for old in drained:
        q.put_nowait(old)
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass  # 理论不可达（已腾位），防御性保留


def broadcast(event: str, data: Any) -> None:
    """向所有 SSE 订阅者推送事件（跨线程安全）。

    长任务（clone/download/scan/k8s 快照）在工作线程中调用本函数。直接
    调用 asyncio.Queue.put_nowait 不会唤醒正阻塞在 select() 的事件循环，
    导致事件被延迟到 15s 心跳才送达。改用 call_soon_threadsafe 把入队操作
    调度回主循环线程，可立即唤醒消费者。
    """
    msg = json.dumps(data, ensure_ascii=False, default=str)
    item = {"event": event, "data": msg}
    with _lock:
        subs = list(_subscribers)
    loop = _main_loop
    for q in subs:
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(_enqueue, q, item)
            except RuntimeError:
                # 循环已关闭，忽略
                pass
        else:
            # 事件循环尚未就绪（理论上仅主循环线程内调用可达）；
            # 兜底路径同样按优先级丢弃，保证关键告警不被进度事件挤掉
            _enqueue(q, item)
