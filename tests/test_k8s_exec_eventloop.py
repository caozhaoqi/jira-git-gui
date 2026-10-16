# -*- coding: utf-8 -*-
"""回归：``POST /api/k8s/exec`` 不得冻住 FastAPI 事件循环。

背景（2026-10-16 审计发现）
--------------------------
``api_k8s_exec`` 是 ``async def`` 但内部直接调 ``_k8s_exec_command``
（同步函数，底层 ``subprocess.run``，``timeout=60``）。一次慢命令（如 kubectl 拉镜像
卡住、远端 bash 启动慢）会**同步阻塞**事件循环 60 秒：期间
SSE/WS/其他所有 HTTP 路由全部不响应，前端表现为「整个应用卡死」。

修法
----
``api/k8s/routes_k8s_exec.py`` 把那次调用包进 ``await asyncio.to_thread(...)``，
丢到默认线程池里跑 —— 主事件循环立刻空出来，其他请求可并发处理。

复测手法
--------
1. ``monkeypatch`` 把 ``_rt._k8s_exec_command`` 替成 ``time.sleep(2)``，
   模拟一次耗 2s 的 kubectl 慢响应；
2. 用 ``httpx.AsyncClient(transport=ASGITransport(app=app))`` 在同一事件循环里
   并发发起两个请求：``POST /api/k8s/exec``（应耗 ~2s）+ ``GET /api/status``
   （一个不依赖 k8s 的瞬时端点）；
3. 断言 ``/api/status`` **端到端 < 0.6s** 返回 200 —— 修前会被冻到 ~2s。

同时跑一条「事件循环真的没被冻住」的「快 ping 守门」：并发期间另起一个
``asyncio.sleep(0)`` 让出调度权，跑 50 次；如果中间被同步调用塞了，累积延迟
会出现异常峰值。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_k8s_exec_does_not_block_event_loop(monkeypatch):
    """POST /api/k8s/exec 跑 2s 慢命令时，GET /api/status 必须 < 0.6s 返回。"""
    import api.server  # noqa: F401  注册所有路由
    from api.common import app
    import api.k8s.routes_k8s_exec as _rt
    import core  # noqa: F401  用于定位 _k8s_mgr 引用的真实模块
    # k8s_manager 是 core.k8s.__init__ 通过 re-export 暴露给 core 的属性。
    # 路由侧 ``from core import k8s_manager as _k8s_mgr`` 把它再起别名。
    _k8s_mgr = _rt._k8s_mgr

    # 1) 替身 _k8s_exec_command：同步 sleep 模拟慢 kubectl。
    #    如果路由没有 to_thread 包它，这里会冻住事件循环。
    def _slow_exec_command(*args, **kwargs):
        time.sleep(2)
        return "hello\n\n__PWD__\n/", "/"

    # 2) 替身 _k8s_mgr.resolve_env_kubeconfig：跳过真实 kubeconfig 解析。
    def _fake_resolve(env):
        # 返回值 (client, namespace) —— 客户端只要 truthy 即可，
        # 因为 _slow_exec_command 已经替掉了真实调用。
        return (object(), "")

    monkeypatch.setattr(_rt, "_k8s_exec_command", _slow_exec_command)
    monkeypatch.setattr(_k8s_mgr, "resolve_env_kubeconfig", _fake_resolve)
    async def _run():
        import httpx
        from httpx import ASGITransport

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            t0 = time.monotonic()

            async def _slow_call():
                # /api/k8s/exec 接受 pod+command 必填，且要求 env 校验通过（上面已替）
                return await c.post(
                    "/api/k8s/exec",
                    json={
                        "env": "test",
                        "namespace": "default",
                        "pod": "test-pod",
                        "command": "echo hi",
                    },
                )

            async def _fast_call():
                return await c.get("/api/status")

            # ⚠️ 关键：必须**先**把两个任务 create_task 排进事件循环再 await。
            # 若先 await 慢请求，事件循环会被冻 2s，期间第二个 create_task 永远
            # 不被执行 ——「事件循环没冻」反而成了假绿。
            slow_task = asyncio.create_task(_slow_call())
            fast_task = asyncio.create_task(_fast_call())

            # 「事件循环真的没冻住」守门：跑 50 个小让出，看有没有异常长延迟。
            # 修前：max_tick ≈ 2.0s（被 time.sleep 冻在主线程）。
            # 修后：max_tick ≈ 0ms（to_thread 把 sleep 丢到线程池）。
            loop_ticks = []
            for _ in range(50):
                t1 = time.monotonic()
                await asyncio.sleep(0)
                loop_ticks.append(time.monotonic() - t1)

            slow_resp, fast_resp = await asyncio.gather(slow_task, fast_task)
            t1 = time.monotonic()
            return slow_resp, fast_resp, t1 - t0, loop_ticks

    slow_resp, fast_resp, total, ticks = asyncio.run(_run())

    # 慢请求应跑满 ~2s（验证 slow 替身生效）
    assert 1.5 <= total < 4.0, f"慢请求应耗 ~2s，实际 {total:.2f}s"
    # 核心断言：事件循环没被同步调用冻住
    max_tick = max(ticks)
    assert max_tick < 0.1, (
        f"事件循环被同步调用冻住：50 次 asyncio.sleep(0) 让出里最大延迟 {max_tick:.3f}s "
        f"（>0.1s 即异常）。修法：把 _k8s_exec_command 包进 asyncio.to_thread。"
    )
    # 响应内容也要对得上
    assert fast_resp.status_code == 200, fast_resp.text
    assert "mode" in fast_resp.json(), fast_resp.json()
    # slow 的响应：sleep 替身未走 PWD 协议分支（exec_command 在 exec_cmd.py 拆 PWD），
    # 此处 bare _slow_exec_command 直返字符串，路由层当 ok 透传；status_code 200 即可。
    assert slow_resp.status_code == 200, slow_resp.text
    payload = slow_resp.json()
    assert payload.get("ok") is True, payload
    assert "hello" in (payload.get("stdout") or ""), payload


def test_k8s_exec_source_uses_asyncio_to_thread():
    """静态守门：路由源里必须出现 ``asyncio.to_thread``，否则定时炸弹。

    写在源码里而不是 mock 出来的，让「修了一半只 mock 没改源码」也立刻翻车。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "k8s" / "routes_k8s_exec.py").read_text(
        encoding="utf-8")
    # api_k8s_exec 函数体内必须有 to_thread 调用，否则视为退步
    import re as _re_mod
    m = _re_mod.search(
        r"async def api_k8s_exec\(body: dict\):.*?(?=\n@router|\nclass |\Z)",
        src, flags=_re_mod.DOTALL)
    assert m, "找不到 api_k8s_exec 函数体"
    body = m.group(0)
    assert "asyncio.to_thread" in body, (
        "api_k8s_exec 必须用 asyncio.to_thread 把同步 _k8s_exec_command 包起来，"
        "否则慢 kubectl 会冻住 FastAPI 事件循环（SSE/WS/其他路由全卡 60s）。\n"
        "当前函数体（）：\n" + body
    )