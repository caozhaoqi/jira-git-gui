"""HCM 参数解密端点（/api/hcm/decrypt）回归测试。

核心语义：网关 /api/api_decrypt_param 的 DecryptParamHandler 自己解两层
（_asynchronous_service 一层 + execute 一层，且每层都会 pop 掉
hcm_transfer_strategy / hcm_param），因此**不能走 /api/hcm/direct 的信封
通道**。后端 hcm_decrypt_param 的正确姿势是：
  1. 把抓包信封 {hcm_transfer_strategy, hcm_param, s3h} 用 encrypt_param
     再加密一层作为外层 hcm_param；
  2. 明文 POST body {hcm_transfer_strategy:'ha', hcm_param:<外层>, s3h:<签名>}；
  3. 校验：外层信封解开后**必须仍含** strategy/hcm_param 键（execute 第二层
     的输入），即嵌套结构成立。
"""
import asyncio
import json
import unittest
from unittest import mock

import api.hcm.hcm_core as hc
from api.hcm.hcm_core import hcm_decrypt_param, _load_hcm_direct_core


def _hd():
    return _load_hcm_direct_core()


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.content = text.encode("utf-8")


class _FakeAsyncClient:
    """记录请求体的 httpx.AsyncClient 替身。"""

    bodies: list = []

    def __init__(self, responses=None):
        self._responses = responses or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        type(self).bodies.append(kwargs.get("content"))
        if self._responses:
            return self._responses.pop(0)
        return _FakeResp(200, json.dumps({"result": {"ok": 1}}))


def _req(hcm_param="CAPTURED", strategy="ha", s3h=""):
    class R:
        pass
    r = R()
    r.hcm_param = hcm_param
    r.hcm_transfer_strategy = strategy
    r.s3h = s3h
    r.target = "http://gw.test"
    r.token = "tok"
    return r


class TestHcmDecryptParam(unittest.TestCase):
    def setUp(self):
        _FakeAsyncClient.bodies = []
        hc._HCM_PROXY_TARGET_BACKUP = getattr(hc, "_HCM_PROXY_TARGET", "")

    def _run(self, req):
        return asyncio.run(hcm_decrypt_param(req))

    def test_body_is_nested_envelope_and_result_ok(self):
        """body 必须是 {strategy:'ha', hcm_param:<encrypt(信封)>}，且解开后含 strategy 键。"""
        hd = _hd()
        fake = _FakeAsyncClient([_FakeResp(200, json.dumps({"result": {"foo": "bar"}}))])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"], {"foo": "bar"})

        body = json.loads(_FakeAsyncClient.bodies[0])
        # 传输层形态
        self.assertEqual(body["hcm_transfer_strategy"], "ha")
        self.assertIn("hcm_param", body)
        # 解开外层 = 抓包信封，必须仍含 strategy/hcm_param（execute 第二层输入）
        opened = hd.decrypt_param(body["hcm_param"], "ha")
        self.assertEqual(opened["hcm_transfer_strategy"], "ha")
        self.assertEqual(opened["hcm_param"], "CAPTURED")

    def test_s3h_auto_signed_when_empty(self):
        """s3h 留空 → 后端按抓包密文自动计算签名，且随内层信封上送。"""
        hd = _hd()
        fake = _FakeAsyncClient([_FakeResp(200, json.dumps({"result": {}}))])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req(s3h=""))
        self.assertTrue(out["ok"])
        body = json.loads(_FakeAsyncClient.bodies[0])
        opened = hd.decrypt_param(body["hcm_param"], "ha")
        self.assertEqual(opened["s3h"], hd.sign_param("CAPTURED"))

    def test_s3h_passthrough(self):
        """显式 s3h 原样进内层信封。"""
        hd = _hd()
        fake = _FakeAsyncClient([_FakeResp(200, json.dumps({"result": {}}))])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            self._run(_req(s3h="SIG123"))
        body = json.loads(_FakeAsyncClient.bodies[0])
        opened = hd.decrypt_param(body["hcm_param"], "ha")
        self.assertEqual(opened["s3h"], "SIG123")

    def test_token_error_triggers_relogin_and_retry(self):
        """首响应 17003 → 自动重登 → 用新 token 重试成功。"""
        fake = _FakeAsyncClient([
            _FakeResp(200, json.dumps({"errcode": 17003, "errmsg": "token 过期"})),
            _FakeResp(200, json.dumps({"result": {"after": 1}})),
        ])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake), \
             mock.patch.object(hc, "_auto_relogin_token", mock.AsyncMock(return_value="NEW")):
            out = self._run(_req())
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"], {"after": 1})
        # 两次请求都发出
        self.assertEqual(len(_FakeAsyncClient.bodies), 2)

    def test_error_response_passthrough(self):
        """网关业务错误（HTTP 200 + ok:False 结构）→ ok:False 且带 errmsg。"""
        fake = _FakeAsyncClient([_FakeResp(200, json.dumps(
            {"code": 500, "message": "no permission"}))])
        with mock.patch.object(hc.httpx, "AsyncClient", lambda **kw: fake):
            out = self._run(_req())
        self.assertFalse(out["ok"])
        self.assertIn("no permission", out["error"]["errmsg"])

    def test_empty_param_rejected(self):
        with self.assertRaises(Exception):
            self._run(_req(hcm_param=""))


if __name__ == "__main__":
    unittest.main()
