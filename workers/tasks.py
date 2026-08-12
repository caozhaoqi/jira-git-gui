"""通用后台工作线程。

把任意阻塞调用（网络/克隆/下载）放到子线程执行，避免冻结 UI。
- finished(result) : 正常完成，携带返回值
- error(msg)       : 异常，携带【完整 traceback 文本】（不再只是 str(e)，可追溯）
- log(msg)         : 进度日志（若目标函数签名含 on_log 参数，自动注入 self.log.emit）

用法：
    w = Worker(client.connect)
    w.finished.connect(self._on_done)
    w.error.connect(self._on_error)
    w.log.connect(self._log)
    w.start()
"""
import inspect
import logging
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from core.logger import get_logger


class Worker(QThread):
    # 注意：不要命名为 finished（会覆盖 QThread.finished），删除线程应挂到内置 finished 上
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        logger = get_logger()
        try:
            # 若目标函数接受 on_log，自动注入日志回调
            try:
                sig = inspect.signature(self._fn)
                if "on_log" in sig.parameters:
                    self._kwargs.setdefault("on_log", self.log.emit)
            except (ValueError, TypeError):
                pass
            result = self._fn(*self._args, **self._kwargs)
            self.result.emit(result)
        except Exception:
            # 关键：记录完整堆栈，并随 error 信号一并上抛，便于追溯「闪退」根因
            tb = traceback.format_exc()
            logger.error(
                "后台任务异常（%s）:\n%s",
                getattr(self._fn, "__qualname__", str(self._fn)), tb)
            self.error.emit(tb)
