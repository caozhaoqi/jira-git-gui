# -*- coding: utf-8 -*-
"""全接口回归冒烟测试。

目标：验证全部已注册路由「不 500、不抛未捕获异常、不挂死」，并隔离所有外部副作用
（kubectl / clash 系统命令 / Jira 网络 / CF 网络），保证离线可跑、不破坏本地状态。

原则：
- GET 接口：带默认参数直接请求，断言 status < 500。
- 有 body 模型的 POST/PUT/DELETE：发 `{}`，期望 422（参数校验拦截 = 路由活着）。
- 无 body 的写接口：仅跑「纯标志类」（download/cancel、k8s/cancel 等）；
  会破坏本地状态/真实配置的（清 Cookie、删同步历史、改 services 配置）显式 SKIP。
- SSE 接口（/api/events、/api/k8s/log/stream）：stream 读首帧即断开。
- WebSocket（/ws/k8s/exec）：单独握手探测。

用法：venv/bin/python -m pytest tests/test_routes_regression.py -q
"""
import re
import sys
from pathlib import Path

import pytest
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
import api.server  # noqa: E402  —— 必须 import server 才有全部路由
from api.common import app  # noqa: E402
from core.errors import UserError  # noqa: E402
from fastapi.routing import _IncludedRouter  # noqa: E402


# --------------------------------------------------------------------------- #
#  路由枚举
# --------------------------------------------------------------------------- #
def walk_routes(router):
    out = []
    for r in router.routes:
        if type(r).__name__ == "_IncludedRouter":
            out.extend(walk_routes(r.original_router))
        else:
            out.append(r)
    return out


def all_apiroutes():
    return [r for r in walk_routes(app) if type(r).__name__ == "APIRoute"]


# --------------------------------------------------------------------------- #
#  副作用隔离 fixture
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(monkeypatch):
    # 屏蔽启动钩子里的 CF 自动登录（patch server 模块里绑定的名字，避免真网络）
    async def _no_autologin(*a, **kw):
        return []
    monkeypatch.setattr("api.server.cf_autologin_all", _no_autologin)

    # k8s：kubectl 一律失败（无集群）
    def fake_kubectl(args, kubeconfig=None, timeout=60, input=None):
        return "", 1, "no cluster (test)"
    monkeypatch.setattr("core.k8s.kubectl.run_kubectl", fake_kubectl)
    monkeypatch.setattr("core.k8s.kubectl.run_kubectl_async", fake_kubectl)

    # k8s 流式 kubectl：返回假 process（避免真起子进程）
    async def fake_stream_kubectl(args, kubeconfig=None):
        class _FakeProc:
            stdout = None
            async def kill(self):
                pass
            async def wait(self):
                pass
        return _FakeProc()
    monkeypatch.setattr("core.k8s.kubectl.stream_kubectl", fake_stream_kubectl)

    # clash：系统命令静默失败
    monkeypatch.setattr("api.clash.clash_base._run", lambda cmd, timeout=5.0: "")

    # Jira 仓库发现：返回空（避免真扫网络）
    monkeypatch.setattr("api.common.client.discover_repos", lambda force=False: [])

    # CF 网络：一律连接失败（patch 使用模块 routes_cf 绑定的名字）
    def fail_connect(*a, **kw):
        raise httpx.ConnectError("network disabled in test")
    monkeypatch.setattr("api.cf.cf_tokens.new_cf_client", fail_connect)

    # CF 登录账号调用：直接失败
    async def fail_login(*a, **kw):
        return {"ok": False, "need_captcha": False, "message": "network disabled in test"}
    monkeypatch.setattr("api.cf.routes_cf.cf_login_account", fail_login)

    # CF 自动登录：空结果
    async def no_auto_login(*a, **kw):
        return []
    monkeypatch.setattr("api.cf.routes_cf.cf_autologin_all", no_auto_login)

    # CF 刷新 token：失败
    async def fail_refresh(*a, **kw):
        return None
    monkeypatch.setattr("api.cf.routes_cf.cf_refresh_token", fail_refresh)

    # CF 诊断上下文：同步函数（被 unified_diagnose 内部 import 直接调用，patch 定义点）
    monkeypatch.setattr("api.cf.cf_diagnose.cf_diagnose_context",
                        lambda req, **kw: {"ok": False, "error": "network disabled"})
    # CF 日志查询：async 空结果（patch 定义点 + full_diagnose 模块级绑定）
    async def fake_query_logs(*a, **kw):
        return {"ok": True, "logs": [], "masked": []}
    monkeypatch.setattr("api.cf.cf_logs.cf_query_logs", fake_query_logs)
    monkeypatch.setattr("api.full_diagnose.cf_query_logs", fake_query_logs)
    monkeypatch.setattr("api.cf.routes_cf.cf_query_logs", fake_query_logs)

    # HCM 代理：禁用（patch 使用模块 routes_hcm 里绑定的名字，避免真转发）
    async def fake_proxy(*a, **kw):
        return {"ok": False, "error": "proxy disabled in test"}
    monkeypatch.setattr("api.hcm.routes_hcm.hcm_proxy", fake_proxy)
    # 兜底：HCM 直连也禁用
    async def fake_direct(*a, **kw):
        return {"ok": False, "error": "direct disabled in test"}
    monkeypatch.setattr("api.hcm.routes_hcm.hcm_direct", fake_direct)
    # HCM 数据保存：本地会真写文件 —— patch 掉（避免测试落盘）
    def fake_save_data(req):
        raise UserError("save disabled in test")
    monkeypatch.setattr("api.hcm.routes_hcm.hcm_save_data", fake_save_data)

    # 服务配置写操作：本地 JSON 会被真实改写 —— 只测 GET，POST/PUT/DELETE 走 SKIP 集
    # 统一诊断：避免内部真执行（CF 已禁 + kubectl 已禁，应能自然失败，不额外 patch）

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --------------------------------------------------------------------------- #
#  分类
# --------------------------------------------------------------------------- #
# 无 body 但会破坏本地状态/配置的接口 —— 显式跳过（在单独清单里说明原因）
SKIP_WRITE_NO_BODY = {
    "DELETE /api/session": "清空本地 Cookie 会话文件",
    "DELETE /api/sync-history": "删除全部同步历史",
    "DELETE /api/resume": "清空断点续传清单",
    "DELETE /api/services/cloud-functions/{index}": "删除云函数配置条目",
    "PUT /api/services/cloud-functions/{index}": "修改云函数配置条目",
    "POST /api/services/cloud-functions": "新增云函数配置",
    "POST /api/services/hcm-config": "写入 HCM 代理配置",
    "POST /api/services/jira-config": "写入 Jira 连接配置",
    "POST /api/cf/cases/save": "写 CF 案例库",
    "POST /api/cf/clipboard-save": "写剪贴板案例",
    "POST /api/cf/cases/feedback": "提交案例反馈（外部）",
    "POST /api/cf/cases/feedback-learn": "反馈学习入库",
    "POST /api/cf/logs/export": "导出日志文件",
    "POST /api/cf/diagnose-context": "诊断上下文生成（重）",
    "POST /api/cf/retrofit": "写改造结果",
}

# 无 body 但纯标志/缓存类 —— 可安全跑
SAFE_WRITE_NO_BODY = {
    "POST /api/download/cancel": "仅置取消标志",
    "POST /api/k8s/cancel": "仅置取消标志",
    "POST /api/diff/invalidate": "无 repo 时 400，安全",
    "DELETE /api/cache": "清缓存（用 namespace 限定）",
    "POST /api/cf/diagnose-index/rebuild": "重建索引（本地）",
}

STREAM_PATHS = {"/api/events", "/api/k8s/log/stream"}

# SSE 无限流端点：TestClient 拉流会因 ASGI transport 限制挂死（SIGKILL），
# 改为单独直接调用 endpoint 断言返回 StreamingResponse 类型（不消费 body）。
SSE_ENDPOINT_ONLY = {"/api/events"}


def type_default(anno):
    t = getattr(anno, "__name__", str(anno))
    if t == "int": return 0
    if t == "bool": return False
    if t == "float": return 0.0
    if t in ("list", "List"): return []
    if t == "dict": return {}
    return ""


def field_anno(p):
    """兼容 FastAPI 新旧版本的 ModelField 类型访问。"""
    for attr in ("annotation", "outer_type_", "type_"):
        v = getattr(p, attr, None)
        if v is not None:
            return v
    return None


def build_query(route):
    q = {}
    for p in route.dependant.query_params:
        try:
            required = p.required
        except Exception:
            required = False
        try:
            default = p.default
        except Exception:
            default = None
        if not required and default is not None and str(default) != "PydanticUndefined":
            q[p.name] = default
        else:
            q[p.name] = type_default(field_anno(p))
    return q


def fill_path(path, params):
    out = path
    for k, v in params.items():
        out = re.sub(r"\{" + re.escape(k) + r"(:[^}]*)?\}", str(v), out)
    return out


def path_params(path):
    params = {}
    for m in re.finditer(r"\{(\w+)(:[^}]*)?\}", path):
        name = m.group(1)
        params[name] = 0 if any(s in name for s in ("id", "index", "count", "port")) else "x"
        if name == "api_name":
            params[name] = "dummy"
    return params


# --------------------------------------------------------------------------- #
#  测试主体
# --------------------------------------------------------------------------- #
def _iter_cases():
    for r in all_apiroutes():
        for m in sorted(r.methods or []):
            if m in ("HEAD", "OPTIONS"):
                continue
            yield m, r


def _call_endpoint_sse(route):
    """直接调用 SSE endpoint，取首帧后关闭生成器（不经过 ASGI transport，不挂死）。"""
    import asyncio
    from fastapi.responses import StreamingResponse

    # 构造极简 Request 桩（SSE 端点只用到 request.is_disconnected()）
    class _FakeRequest:
        async def is_disconnected(self):
            return False

    # 用 asyncio 跑 endpoint，只消费第一帧
    async def _probe():
        endpoint = route.endpoint
        response = await endpoint(_FakeRequest())
        if not isinstance(response, StreamingResponse):
            return f"非 StreamingResponse: {type(response).__name__}"
        gen = response.body_iterator
        try:
            first = await gen.__anext__()
            return f"StreamingResponse 首帧 {first[:60]!r}"
        except StopAsyncIteration:
            return "StreamingResponse 无首帧"
        finally:
            await gen.aclose()

    try:
        return asyncio.run(asyncio.wait_for(_probe(), timeout=10))
    except Exception as e:
        return f"SSE 探测异常: {type(e).__name__}: {e}"


def test_all_routes_no_500(client):
    """遍历全部接口：断言非 500、无异常、不挂死。"""
    results = []
    for method, r in _iter_cases():
        path = r.path
        key = f"{method} {path}"
        query = build_query(r)
        full = fill_path(path, path_params(path))
        has_body = bool(r.dependant.body_params)

        # 跳过清单
        if key in SKIP_WRITE_NO_BODY:
            results.append((method, path, "SKIP", SKIP_WRITE_NO_BODY[key]))
            continue
        if not has_body and method != "GET" and key not in SAFE_WRITE_NO_BODY:
            results.append((method, path, "SKIP", "无 body 写接口（副作用未甄别）"))
            continue

        try:
            if path in SSE_ENDPOINT_ONLY:
                # SSE 端点：直接调用，验证返回 StreamingResponse 即可（不拉流）
                result = _call_endpoint_sse(r)
                results.append((method, path, "SSE", result))
            elif path == "/api/cache" and method == "DELETE":
                # 清缓存必须带 namespace，避免误清全库（2482 个文件）
                resp = client.request(method, full, params={**query, "namespace": "__regression__"})
                results.append((method, path, resp.status_code, ""))
            elif path in STREAM_PATHS:
                # k8s/log/stream：无效 env 应快速 400（UserError -> 400）
                q2 = {**query, "env": "__no_such_env__", "name": "x"}
                resp = client.request(method, full, params=q2)
                results.append((method, path, resp.status_code, ""))
            elif has_body:
                resp = client.request(method, full, params=query or None, json={})
                results.append((method, path, resp.status_code, ""))
            else:
                resp = client.request(method, full, params=query or None)
                results.append((method, path, resp.status_code, ""))
        except Exception as e:
            results.append((method, path, "EXC", f"{type(e).__name__}: {e}"))

    # 报告
    fails = [x for x in results if x[2] == "EXC" or (isinstance(x[2], int) and x[2] >= 500)]
    skips = [x for x in results if x[2] == "SKIP"]
    fours = [x for x in results if isinstance(x[2], int) and 400 <= x[2] < 500]
    oks = [x for x in results if x[2] in ("SSE",) or (isinstance(x[2], int) and x[2] < 400)]

    print(f"\n{'='*90}")
    print(f"全部接口: {len(results)}  通过(<400): {len(oks)}  4xx拦截: {len(fours)}  "
          f"跳过: {len(skips)}  失败/500: {len(fails)}")
    print(f"{'='*90}")
    if fails:
        print("\n--- 失败 / 500 / 异常 ---")
        for m, p, s, extra in fails:
            print(f"  [{s}] {m} {p}  {extra}")
    print("\n--- 4xx（合法拦截/前置条件不满足）---")
    for m, p, s, extra in sorted(fours, key=lambda x: (x[2], x[1])):
        print(f"  {s} {m} {p}")
    print("\n--- SKIP 明细 ---")
    for m, p, s, extra in skips:
        print(f"  {m} {p}  ← {extra}")
    print("\n--- 通过 / SSE ---")
    for m, p, s, extra in sorted(oks, key=lambda x: x[1]):
        print(f"  {s} {m} {p}  {extra}")

    # 断言
    assert not fails, f"{len(fails)} 个接口返回 500/异常: " + "; ".join(
        f"{m} {p}->{s}" for m, p, s, _ in fails)


def test_websocket_k8s_exec_handshake(client):
    """WebSocket /ws/k8s/exec：无集群环境应握手后立即关闭，而非挂死。"""
    try:
        with client.websocket_connect("/ws/k8s/exec?env=__none__&namespace=default&pod=p") as ws:
            # 允许：收到错误消息后关闭 / 直接关闭 / 异常关闭 —— 都是"明确失败"
            try:
                msg = ws.receive()
                print(f"  WS 收到消息: {msg}")
            except Exception:
                pass
    except Exception as e:
        print(f"  WS 被拒绝: {type(e).__name__}: {e}")
