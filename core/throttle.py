"""请求速率限制（令牌桶）。

目的：批量下载 / 整库递归抓取时，避免对 Jira 服务器发起过高并发与请求速率，
从而防止把对方打崩（HTTP 429 / 503，或被运维限流）。

设计要点：
- 令牌桶是**模块级单例**，所有经由 ``http_get`` / ``_request_with`` 的对外请求
  都会经过它，因此无论线程池开多大，稳态请求速率都被 ``max_qps`` 钳住。
- 线程安全：用 ``threading.Lock`` 串行化取令牌，``acquire()`` 在令牌不足时阻塞。
- 速率可调：``set_global_rate_limit(qps)`` 运行时热更新（UI 旋钮透传）。
- 纯标准库，无第三方依赖，避免循环导入。
"""

import threading
import time
from typing import Optional

_DEFAULT_QPS = 6.0      # 默认稳态上限：每秒最多 ~6 个请求
_DEFAULT_BURST = 4      # 桶容量：允许短促突发（如首次列目录）


class RateLimiter:
    """线程安全令牌桶限流器。

    ``max_qps``：稳态下每秒允许的最大请求数（平均速率）。
    ``burst``：桶容量，允许在空闲后一次性消耗的突发令牌数。
    """

    def __init__(self, max_qps: float = _DEFAULT_QPS, burst: int = _DEFAULT_BURST):
        self._rate = max(0.1, float(max_qps))
        self._burst = max(1, int(burst))
        self._tokens = float(self._burst)
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._qps = float(max_qps)

    @property
    def qps(self) -> float:
        return self._qps

    def set_qps(self, qps: float) -> None:
        """运行时调整稳态速率（线程安全）。"""
        qps = max(0.1, float(qps))
        with self._lock:
            # 充值当前令牌，使速率切换不会因旧余额而突变
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last = now
            self._rate = qps
            self._qps = qps

    def acquire(self, tokens: int = 1) -> None:
        """阻塞直到能取走 ``tokens`` 个令牌。``tokens`` 不超过桶容量。"""
        tokens = max(1, min(int(tokens), self._burst))
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # 需要等待的时长（秒）
                wait = (tokens - self._tokens) / self._rate
            time.sleep(max(0.0, wait))


# ----------------------------------------------------------------- 模块级单例
_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """获取（惰性创建）全局限流器单例。"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter(_DEFAULT_QPS, _DEFAULT_BURST)
    return _limiter


def set_global_rate_limit(qps: float) -> None:
    """热更新全局限流速率（qps）。UI 旋钮调用。"""
    get_rate_limiter().set_qps(qps)


def acquire() -> None:
    """便捷封装：向全局限流器取一个令牌。"""
    get_rate_limiter().acquire(1)
