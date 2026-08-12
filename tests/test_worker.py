"""Worker / _spawn 透传回归测试（不依赖网络，纯逻辑）。

守护的 bug：MainWindow._spawn 原先不接受 max_workers，
导致 download / download_repo 调用传入 max_workers 时抛
TypeError: got an unexpected keyword argument 'max_workers'。

另守护「错误分级」：用户可预期异常（UserError）只以消息文本上抛（不带 traceback），
真实异常仍带完整 traceback——避免友好提示被记成 ERROR + 完整堆栈的日志噪音。
"""
import unittest
from workers.tasks import Worker
from core.errors import UserError


class TestWorkerKwargs(unittest.TestCase):
    def test_forwards_max_workers_to_fn(self):
        captured = {}

        def fn(a, max_workers=None):
            captured["mw"] = max_workers
            return "ok"

        w = Worker(fn, 1, max_workers=4)
        w.start()
        w.wait()
        self.assertEqual(captured.get("mw"), 4)

    def test_forwards_arbitrary_kwargs(self):
        captured = {}

        def fn(x, extra=None):
            captured["extra"] = extra
            return None

        w = Worker(fn, "v", extra="hello")
        w.start()
        w.wait()
        self.assertEqual(captured.get("extra"), "hello")


class TestWorkerErrorClassification(unittest.TestCase):
    def test_user_error_emits_message_only(self):
        got = {}

        def fn():
            raise UserError("请先在仓库面板选择/指定仓库")

        w = Worker(fn)
        w.error.connect(lambda m: got.setdefault("m", m))
        w.run()  # 同步执行，同线程直连信号立即派发
        self.assertEqual(got.get("m"), "请先在仓库面板选择/指定仓库")
        self.assertNotIn("Traceback", got.get("m", ""))

    def test_unexpected_error_emits_traceback(self):
        got = {}

        def fn():
            raise ValueError("boom")

        w = Worker(fn)
        w.error.connect(lambda m: got.setdefault("m", m))
        w.run()
        self.assertIn("Traceback", got.get("m", ""))
        self.assertIn("boom", got.get("m", ""))


if __name__ == "__main__":
    unittest.main()
