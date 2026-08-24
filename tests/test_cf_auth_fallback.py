"""CF 云函数日志查询：认证回退判定 + stale 判定 + 5xx 不误判 token 失效 的回归测试。

纯逻辑测试（不联网）。覆盖 P1-①（回退误判修复）与 P2 相关判定函数，确保：
  - 平台 5xx → 标记为「平台暂时错误」，绝不误判为 token 失效 / 不触发无效重登
  - 401/403/17003/未登录类 → 标记为会话失效（触发重登刷新）
  - 缓存凭证 stale 判定：last_error 非空 / 超 24h → stale
"""
import asyncio
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import api.server as srv


class _FakeResp:
    """模拟 httpx.Response（仅取测试所需字段）。"""
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self._text = text
        self.content = (text or "").encode("utf-8")

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("no json")

    @property
    def text(self):
        return self._text

    @property
    def headers(self):
        return {}


class _FakeAsyncClient:
    """可注入响应序列的 httpx.AsyncClient 替身。"""
    def __init__(self, responses, side_effect=None):
        self._responses = list(responses)
        self._side_effect = side_effect
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        if self._side_effect:
            raise self._side_effect
        if self._responses:
            resp = self._responses[self._calls]
            self._calls += 1
            return resp
        return _FakeResp(200, {"result": {"list": [], "total": 0}})


class TestCfTokenStale(unittest.TestCase):
    def test_stale_on_last_error(self):
        v = {"token": "x", "cookie": "token=x", "last_error": "login failed", "ts": "2020-01-01 00:00:00"}
        self.assertTrue(srv._cf_token_stale(v))

    def test_stale_on_old_ts(self):
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 25 * 3600))
        v = {"token": "x", "cookie": "token=x", "last_error": "", "ts": old}
        self.assertTrue(srv._cf_token_stale(v))

    def test_not_stale_recent(self):
        recent = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        v = {"token": "x", "cookie": "token=x", "last_error": "", "ts": recent}
        self.assertFalse(srv._cf_token_stale(v))

    def test_not_stale_no_token_no_error(self):
        # 尚未获取过凭证（无 last_error、有 ts 占位）→ 不算 stale（避免一启动就提示）
        v = {"token": "", "cookie": "", "last_error": "", "ts": ""}
        self.assertFalse(srv._cf_token_stale(v))


class TestIsSessionErr(unittest.TestCase):
    def test_401_is_session(self):
        self.assertTrue(srv._cf_is_session_err(401, None, ""))

    def test_403_is_session(self):
        self.assertTrue(srv._cf_is_session_err(403, None, ""))

    def test_17003_is_session(self):
        self.assertTrue(srv._cf_is_session_err(0, 17003, ""))

    def test_keyword_unauthorized(self):
        self.assertTrue(srv._cf_is_session_err(200, 0, "用户未登录，请先登录"))

    def test_5xx_not_session(self):
        # 平台 5xx 必须判为非会话类（不误判 token 失效）
        self.assertFalse(srv._cf_is_session_err(500, None, "Internal Server Error"))


def _client():
    return TestClient(srv.app)


class TestCfLogsFallback(unittest.TestCase):
    """用 TestClient 跑 api_cf_logs，验证 5xx 不误判 token 失效。"""

    def test_5xx_returns_platform_error_not_token_invalid(self):
        """缓存 cookie 凭证，方式1 返回 500 → 应报「平台暂时错误」，detail 不含 token 失效。"""
        server_url = "http://test-cf-5xx.invalid"
        srv._CF_TOKEN_CACHE[server_url] = {
            "token": "", "cookie": "token=abc", "name": "t",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "need_captcha": False, "last_error": "",
        }
        resp_500 = _FakeResp(500, text="<html>Internal Server Error</html>")
        with mock.patch.object(srv.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient([resp_500])):
            client = _client()
            r = client.post("/api/cf/logs", json={
                "server_url": server_url, "token": "", "log_type": "",
                "page_index": 1, "page_size": 200, "proxy": "",
            })
        self.assertEqual(r.status_code, 500)
        detail = r.json().get("detail", "")
        self.assertIn("平台暂时错误", detail)
        self.assertNotIn("token 可能已失效", detail)

    def test_401_session_triggers_token_invalid_hint(self):
        """缓存 cookie 凭证，方式1 返回 401 + 未登录 → 应提示 token 可能失效。"""
        server_url = "http://test-cf-401.invalid"
        srv._CF_TOKEN_CACHE[server_url] = {
            "token": "", "cookie": "token=abc", "name": "t",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "need_captcha": False, "last_error": "",
        }
        resp_401 = _FakeResp(401, {"errcode": 0, "errmsg": "未登录，请先登录"})
        with mock.patch.object(srv.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient([resp_401])):
            client = _client()
            r = client.post("/api/cf/logs", json={
                "server_url": server_url, "token": "", "log_type": "",
                "page_index": 1, "page_size": 200, "proxy": "",
            })
        self.assertEqual(r.status_code, 401)
        detail = r.json().get("detail", "")
        self.assertIn("token 可能已失效", detail)


class TestCfPageSizeClamp(unittest.TestCase):
    """page_size 上限保护：超限被钳制到 1000，非数字回退 200。"""

    def test_clamp_via_endpoint(self):
        server_url = "http://test-cf-clamp.invalid"
        srv._CF_TOKEN_CACHE[server_url] = {
            "token": "", "cookie": "token=abc", "name": "t",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "need_captcha": False, "last_error": "",
        }
        captured = {}

        async def _fake_post(self_url, **kwargs):
            captured["page_size"] = kwargs.get("json", {}).get("page_size")
            return _FakeResp(200, {"result": {"list": [], "total": 0}})

        with mock.patch.object(srv.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient([_FakeResp(200, {"result": {"list": [], "total": 0}})])):
            # 注入一个能捕获 payload 的 client：直接给 AsyncClient 的 post 加 wrapper
            def _wrap(**kw):
                inst = _FakeAsyncClient([_FakeResp(200, {"result": {"list": [], "total": 0}})])
                real_post = inst.post
                async def _post(url, **kk):
                    captured["page_size"] = (kk.get("json") or {}).get("page_size")
                    return await real_post(url, **kk)
                inst.post = _post
                return inst
            with mock.patch.object(srv.httpx, "AsyncClient", _wrap):
                client = _client()
                r = client.post("/api/cf/logs", json={
                    "server_url": server_url, "token": "", "log_type": "",
                    "page_index": 1, "page_size": 99999, "proxy": "",
                })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured.get("page_size"), 1000)


if __name__ == "__main__":
    unittest.main()
