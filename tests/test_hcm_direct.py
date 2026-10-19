# -*- coding: utf-8 -*-
"""HCM 直连通道（/api/hcm/direct → hcm_direct）回归测试。

核心语义（对齐网关自带调试页 api.js 的行为）：
- 网关 HTTP >= 400 且响应为 JSON（errcode 结构）→ ok:false + error 结构化（红色结果块）
- 网关 HTTP >= 400 但响应是 **HTML 错误页**（500/nginx/SSO 跳转页）→ 不再抛
  HTTPException 把整页 HTML 塞进 message，而是 ok:false + error.raw 携带原文，
  由前端 Text/Code 视图展示（树视图对 HTML 无意义）
- 200 但非 JSON（网关异常页）→ 同样原文进结果块
"""
import asyncio
import json
import unittest
from unittest import mock

import api.hcm.hcm_core as hc
from api.hcm.hcm_core import hcm_direct

HTML_500 = (
    "<!DOCTYPE html>\n<html>\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<title>500 Internal Server Error</title>\n"
    "</head>\n<body>\n<h1>500</h1>\n</body>\n</html>\n"
)


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.content = text.encode("utf-8")

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.text)


class _FakeAsyncClient:
    """按脚本返回响应的 httpx.AsyncClient 替身（记录调用次数）。

    ⚠️ patch 的是共享 httpx 模块：TestClient 启动时的 cf_accounts auto-login
    等无关消费者也会走这里。因此只对目标 URL（gw.test）弹脚本响应，
    其他 URL 返回最小可用响应，避免脚本被无关调用消耗（全量跑时前面测试
    会污染 cf_accounts 缓存，触发 startup auto-login）。
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls += 1
        if "gw.test" not in str(url):
            return _FakeResp(200, json.dumps({"ok": False}))
        return self._responses.pop(0) if self._responses else _FakeResp(200, "{}")


def _req(api_name="hcm.paas.object.list", params=None, target="http://gw.test"):
    class R:
        pass
    r = R()
    r.api_name = api_name
    r.params = params or {"model": "Employee"}
    r.model = ""
    r.token = "tok"
    r.target = target
    r.sql_debug = False
    r.profile_debug = False
    return r


class TestHcmDirect(unittest.TestCase):
    def setUp(self):
        hc._HCM_PROXY_TARGET_BACKUP = getattr(hc, "_HCM_PROXY_TARGET", "")

    def _run(self, req):
        return asyncio.run(hcm_direct(req))

    def test_success_json_result(self):
        fake = _FakeAsyncClient([_FakeResp(200, json.dumps(
            {"result": {"list": [1, 2], "count": 2}, "srv_begin": 100, "srv_end": 150}))])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"], {"list": [1, 2], "count": 2})
        self.assertEqual(out["meta"]["duration_ms"], 50)
        self.assertEqual(out["gateway"], "http://gw.test")

    def test_business_error_json_passthrough(self):
        """200 + errcode 业务错误：ok:false + 结构化 error（既有语义不回归）。"""
        fake = _FakeAsyncClient([_FakeResp(200, json.dumps(
            {"errcode": 17003, "errmsg": "执行api错误", "traceback": "Traceback...",
             "log_index": "log_x.log"}))])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["errcode"], 17003)
        self.assertEqual(out["meta"]["log_index"], "log_x.log")

    def test_http500_html_error_page_structured(self):
        """HTTP 500 + HTML 错误页：原文进 error.raw / data，不抛 HTTPException。"""
        fake = _FakeAsyncClient([_FakeResp(500, HTML_500)])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["http_status"], 500)
        self.assertIn("非 JSON", out["error"]["errmsg"])
        self.assertEqual(out["error"]["raw"], HTML_500)
        self.assertEqual(out["data"], HTML_500)
        self.assertEqual(out["gateway"], "http://gw.test")
        # 非 token 错误：不触发自动重登（只调一次网关）
        self.assertEqual(fake.calls, 1)

    def test_http200_non_json_structured(self):
        """200 + HTML（网关异常页）：同样结构化，不再 502。"""
        fake = _FakeAsyncClient([_FakeResp(200, "<html>gateway boom</html>")])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertFalse(out["ok"])
        self.assertIn("非 JSON", out["error"]["errmsg"])
        self.assertEqual(out["error"]["raw"], "<html>gateway boom</html>")

    def test_http502_upstream_error_page(self):
        fake = _FakeAsyncClient([_FakeResp(502, "<html><body>502 Bad Gateway</body></html>")])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["http_status"], 502)
        self.assertIn("502 Bad Gateway", out["error"]["raw"])

    def test_route_direct_returns_structured_html_error(self):
        """路由层：非 JSON 错误页走 HTTP 200 + {ok:false}（前端红色结果块），非 5xx。"""
        from fastapi.testclient import TestClient
        import api.server  # noqa: F401  （注册全部路由）
        fake = _FakeAsyncClient([_FakeResp(500, HTML_500)])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            with TestClient(api.server.app) as client:
                r = client.post("/api/hcm/direct", json={
                    "api_name": "hcm.paas.object.list",
                    "params": {"model": "Employee"},
                    "target": "http://gw.test",
                    "token": "tok",
                })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("500", body["error"]["errmsg"])
        self.assertIn("<!DOCTYPE html>", body["error"]["raw"])


if __name__ == "__main__":
    unittest.main()
