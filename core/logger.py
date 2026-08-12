"""统一日志子系统（完整项目建设标准）。

职责：
- 文件落盘：``logs/jira_git_gui.log``（带时间戳 / 级别 / 轮转，单文件 5MB × 3 备份）
- UI 转发：通过 ``LogBridge.message`` 信号把日志投递到主窗口日志面板（线程安全）
- 崩溃捕获：``install_global_hooks()`` 接管
  ``sys.excepthook`` / ``threading.excepthook`` / Qt 消息处理器，
  任何未捕获异常都会写入日志文件（含完整 traceback）并尽力弹出提示，
  避免「点一下就闪退、还无任何日志」的情况。

使用方式：
    from core.logger import get_logger, set_log_bridge, LogBridge, install_global_hooks
    log = get_logger()          # 任意线程调用均安全
    log.info("xxx")             # -> 文件 + 控制台 + UI 面板
"""
import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, qInstallMessageHandler, QtMsgType

# ----------------------------------------------------------------- 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "jira_git_gui.log"

FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
_ROOT_NAME = "jira-git-gui"


# ----------------------------------------------------------------- 桥
# 标记：当前是否正处于 Qt 消息处理器内部，避免从 Qt 内部回调里重入 emit 信号导致崩溃
_IN_QT_HANDLER = False


class LogBridge(QObject):
    """信号桥：把日志消息从任意线程投递到 UI 日志面板。单例由主窗口创建并注入。"""
    message = pyqtSignal(str)


_bridge: "LogBridge | None" = None


def set_log_bridge(bridge: "LogBridge | None") -> None:
    global _bridge
    _bridge = bridge


def get_log_bridge() -> "LogBridge | None":
    return _bridge


class QtLogHandler(logging.Handler):
    """把日志记录通过 LogBridge 转发到 UI。线程安全（Qt 信号自动排队到主线程）。

    注意：若当前正处于 Qt 消息处理器内部（_IN_QT_HANDLER=True），则跳过 UI 转发，
    避免从 Qt 内部回调里重入 Qt 渲染导致崩溃。
    """

    def emit(self, record: logging.LogRecord) -> None:
        global _IN_QT_HANDLER
        if _IN_QT_HANDLER:
            return
        try:
            msg = self.format(record)
            b = get_log_bridge()
            if b is not None:
                b.message.emit(msg)
        except Exception:
            # 绝不让日志系统自身导致崩溃
            pass


# ----------------------------------------------------------------- 构造 logger
_log_lock = threading.Lock()


def get_logger(name: str = _ROOT_NAME) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    with _log_lock:
        if logger.handlers:
            return logger
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        fmt = logging.Formatter(FMT, DATEFMT)

        # 1) 控制台（开发期可见）
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

        # 2) 文件（轮转，完整可追溯）
        try:
            fh = RotatingFileHandler(
                LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3,
                encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception as e:  # 文件不可写也不应阻断主程序
            sh.emit(logging.LogRecord(
                name, logging.WARNING, __file__, 0,
                "日志文件初始化失败：%s", (e,), None))

        # 3) UI 面板
        uh = QtLogHandler()
        uh.setLevel(logging.INFO)
        uh.setFormatter(fmt)
        logger.addHandler(uh)
    return logger


# ----------------------------------------------------------------- 全局钩子
def install_global_hooks() -> None:
    """接管全局未捕获异常与 Qt 消息，全部落盘到日志文件。"""
    logger = get_logger()

    def _excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        logger.critical("未捕获异常（主线程）:\n%s", text)
        _show_fatal(etype, value, tb)

    def _threading_excepthook(args):
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        logger.critical("未捕获异常（子线程 %s）:\n%s",
                        getattr(args.thread, "name", "?"), text)

    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook

    def _qt_msg_handler(msg_type: QtMsgType, context, msg: str) -> None:
        # 关键：本处理器由 Qt 从自身调用栈内部回调，绝不能抛异常、也不能重入 Qt 渲染。
        # 因此只写日志文件/控制台（QtLogHandler 在 _IN_QT_HANDLER 时自动跳过 UI 转发）。
        global _IN_QT_HANDLER
        try:
            level = {
                QtMsgType.QtDebugMsg: logging.DEBUG,
                QtMsgType.QtInfoMsg: logging.INFO,
                QtMsgType.QtWarningMsg: logging.WARNING,
                QtMsgType.QtCriticalMsg: logging.ERROR,
                QtMsgType.QtFatalMsg: logging.CRITICAL,
            }.get(msg_type, logging.WARNING)
            where = f" ({context.file or ''}:{context.line})" if context else ""
            _IN_QT_HANDLER = True
            try:
                logger.log(level, "[Qt:%s]%s %s", msg_type.name, where, msg)
            finally:
                _IN_QT_HANDLER = False
        except Exception:
            # 绝不让消息处理器自身导致 Qt 崩溃
            pass

    qInstallMessageHandler(_qt_msg_handler)


def _show_fatal(etype, value, tb) -> None:
    """崩溃时尽力弹出提示，告诉用户日志位置。best-effort，绝不二次崩溃。"""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance()
        if app is None:
            return
        text = "".join(traceback.format_exception(etype, value, tb))
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("程序发生未捕获异常")
        box.setText(
            "程序遇到未捕获的异常，已记录到日志。\n"
            f"日志文件：{LOG_PATH}\n\n"
            "请将该日志文件内容反馈，以便定位问题。")
        box.setDetailedText(text)
        box.exec()
    except Exception:
        pass
