# -*- coding: utf-8 -*-
"""网络看门狗：监控连续失败，超过阈值后触发任务自动取消。

被 API 层与 core.client 共用，独立成模块以便复用且不污染 client 命名空间。
"""
import threading
import time


class NetworkWatchdog:
    """监控网络请求失败次数，在连续失败超过阈值后触发自动取消。

    设计目标：
    - 当网络长时间中断时（如断网 1 分钟以上），自动停止正在进行的
      扫描/下载/克隆任务，而不是让每个请求都超时重试（40s*5次）后放弃。
    - 任何一次成功的请求都会重置计数器，保证瞬时网络抖动不会误判。
    - 线程安全：所有方法通过 _lock 保护，可在任意工作线程中调用。

    使用方式：
        watchdog = NetworkWatchdog(threshold=5)
        # 将 watchdog.should_abort 嵌入 should_cancel 回调
        # 在 http_get 失败时调用 watchdog.notify_failure(err)
        # 在 http_get 成功时调用 watchdog.notify_success()
    """

    def __init__(self, threshold: int = 5, window: float = 60.0):
        self._threshold = threshold       # 连续失败多少次后判定网络不可用
        self._window = window             # 失败记录的有效期（秒），过期后自动重置
        self._failures = 0
        self._last_failure_time = 0.0
        self._down = False
        self._reason = ""
        self._lock = threading.Lock()

    def notify_failure(self, error_msg: str = "") -> None:
        """记录一次传输层失败（超时/连接拒绝/DNS 失败等）。"""
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self._threshold:
                self._down = True
                self._reason = f"网络连续失败 {self._failures} 次（最近错误：{error_msg[:120]}）"

    def notify_success(self) -> None:
        """请求成功后调用，重置失败计数。"""
        with self._lock:
            self._failures = 0
            self._down = False
            self._reason = ""

    def should_abort(self) -> bool:
        """检查网络是否已判定为不可用。

        若失败记录已超过 window 有效期（可能网络已恢复），自动重置。
        """
        with self._lock:
            if self._down and (time.time() - self._last_failure_time) > self._window:
                self._down = False
                self._failures = 0
                return False
            return self._down

    @property
    def down(self) -> bool:
        with self._lock:
            return self._down

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def reset(self) -> None:
        """手动重置（例如用户重新连接后）。"""
        with self._lock:
            self._failures = 0
            self._down = False
            self._reason = ""
