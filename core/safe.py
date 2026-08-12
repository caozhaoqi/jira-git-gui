"""槽函数 / 回调异常保护。

把 PyQt 信号槽里未捕获的异常拦下来，写入日志（含完整 traceback），
而不是让 PyQt 把异常抛回事件循环导致整个进程「闪退」。
"""
import functools
import inspect
import logging
import traceback

from core.logger import get_logger


def safe_slot(func=None, *, level: str = "error"):
    """装饰 PyQt 槽函数或任意回调。

    异常时记录完整 traceback 到日志，不向上抛出，避免界面闪退。
    ``level`` 可为 ``"error"`` / ``"warning"`` / ``"critical"``。

    用法：
        @safe_slot
        def _on_test_done(self, res): ...

        @safe_slot(level="warning")
        def _on_click(self): ...

    注意：PyQt 信号常会多传参数（例如 ``QPushButton.clicked`` 会带一个 ``bool``），
    而被装饰的方法可能只接收 ``self``。wrapper 会自动按原函数能接受的参数个数裁剪多余实参，
    避免 ``takes 1 positional argument but 2 were given`` 这类错误。
    """
    def decorator(fn):
        lvl = getattr(logging, level.upper(), logging.ERROR)

        # 预计算原函数能接受的「位置参数个数」，用于裁剪信号多余实参
        try:
            params = list(inspect.signature(fn).parameters.values())
            has_var_pos = any(
                p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            n_accept = None if has_var_pos else len(params)
        except (ValueError, TypeError):
            n_accept = None  # 无法解析时不做裁剪，原样透传

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                # 裁剪多余位置实参（信号可能比槽声明多传），避免 TypeError
                if n_accept is not None and len(args) > n_accept:
                    args = args[:n_accept]
                return fn(*args, **kwargs)
            except Exception:
                logger = get_logger()
                logger.log(
                    lvl,
                    "槽函数异常 [%s]:\n%s",
                    getattr(fn, "__qualname__", fn.__name__),
                    traceback.format_exc(),
                )
                return None

        return wrapper

    # 允许 @safe_slot 与 @safe_slot(level=...) 两种写法
    if func is not None and callable(func):
        return decorator(func)
    return decorator
