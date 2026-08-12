"""通用后台工作线程。

把任意阻塞调用（网络/克隆/下载）放到子线程执行，避免冻结 UI。
- finished(result) : 正常完成，携带返回值
- error(msg)       : 异常，携带错误信息
- log(msg)         : 进度日志（若目标函数签名含 on_log 参数，自动注入 self.log.emit）

用法：
    w = Worker(client.connect)
    w.finished.connect(self._on_done)
    w.error.connect(self._on_error)
    w.log.connect(self._log)
    w.start()
"""
import inspect

from PyQt6.QtCore import QThread, pyqtSignal


class Worker(QThread):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            # 若目标函数接受 on_log，自动注入日志回调
            try:
                sig = inspect.signature(self._fn)
                if "on_log" in sig.parameters:
                    self._kwargs.setdefault("on_log", self.log.emit)
            except (ValueError, TypeError):
                pass
            result = self._fn(*self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception as e:  # 子线程异常需捕获并回传主线程
            self.error.emit(str(e))
