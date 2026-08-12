"""限流（令牌桶）与 429/503 退避回归测试（纯逻辑，不联网）。"""
import time
import unittest
from unittest import mock

from core import throttle
from core.throttle import RateLimiter, set_global_rate_limit, get_rate_limiter
import core.client as client_mod


class _FakeResp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.request = None


class TestRateLimiter(unittest.TestCase):
    def test_pacing_respects_qps(self):
        # qps=4, burst=1：第 1 个令牌立即拿；之后每个需 0.25s
        rl = RateLimiter(max_qps=4, burst=1)
        t0 = time.monotonic()
        rl.acquire()
        rl.acquire()
        rl.acquire()
        elapsed = time.monotonic() - t0
        # 第 2、3 个各需 0.25s，合计 ≥ 0.5s；留出余量断言 ≥ 0.4s
        self.assertGreaterEqual(elapsed, 0.4)

    def test_burst_allows_short_spike(self):
        # burst=3：前 3 个令牌应几乎瞬时拿到
        rl = RateLimiter(max_qps=2, burst=3)
        t0 = time.monotonic()
        for _ in range(3):
            rl.acquire()
        self.assertLess(time.monotonic() - t0, 0.2)

    def test_set_qps_hot_update(self):
        rl = RateLimiter(max_qps=10, burst=1)
        rl.set_qps(3)
        self.assertAlmostEqual(rl.qps, 3.0)


class TestGlobalRateLimit(unittest.TestCase):
    def setUp(self):
        # 每个用例用干净的单例
        throttle._limiter = None

    def test_default_singleton(self):
        rl = get_rate_limiter()
        self.assertIs(rl, get_rate_limiter())
        self.assertGreater(rl.qps, 0)

    def test_set_global_rate_limit(self):
        set_global_rate_limit(12)
        self.assertAlmostEqual(get_rate_limiter().qps, 12.0)


class TestBackoff(unittest.TestCase):
    def test_should_backoff_statuses(self):
        self.assertTrue(client_mod._should_backoff(_FakeResp(429)))
        self.assertTrue(client_mod._should_backoff(_FakeResp(503)))
        self.assertFalse(client_mod._should_backoff(_FakeResp(200)))
        self.assertFalse(client_mod._should_backoff(_FakeResp(404)))

    def test_backoff_honors_retry_after_seconds(self):
        with mock.patch.object(client_mod.time, "sleep") as fake_sleep:
            client_mod._backoff_for(_FakeResp(429, {"Retry-After": "7"}), attempt=0)
            fake_sleep.assert_called_once_with(7.0)

    def test_backoff_exponential_when_no_header(self):
        with mock.patch.object(client_mod.time, "sleep") as fake_sleep:
            client_mod._backoff_for(_FakeResp(429), attempt=2)
            # 退避 = min(30, 2*(attempt+1)) = 6s
            fake_sleep.assert_called_once_with(6.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
