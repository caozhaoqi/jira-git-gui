"""统一日志子系统。

职责：
- 文件落盘：``logs/jira_git_gui.log``（带时间戳 / 级别 / 轮转，单文件 5MB × 3 备份）
- 控制台输出（开发期可见）
- 崩溃捕获：``install_global_hooks()`` 接管
  ``sys.excepthook`` / ``threading.excepthook``，
  任何未捕获异常都会写入日志文件（含完整 traceback），
  避免「点一下就闪退、还无任何日志」的情况。

2026-10-16：PyQt6 桌面实现已移至 ``pyqt-legacy`` 分支，本模块随之剥离
Qt 信号桥（LogBridge / QtLogHandler / Qt 消息处理器 / 崩溃弹窗）。
交付形态为 Electron + Tauri 桌面壳 + 无头后端（api.server），日志走文件。

使用方式：
    from core.logger import get_logger, install_global_hooks
    log = get_logger()          # 任意线程调用均安全
    log.info("xxx")             # -> 文件 + 控制台
"""
import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ----------------------------------------------------------------- 路径
from core.app_paths import get_data_root

LOG_DIR = get_data_root() / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "jira_git_gui.log"

FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
_ROOT_NAME = "jira-git-gui"

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
    return logger


# ----------------------------------------------------------------- 全局钩子
def install_global_hooks() -> None:
    """接管全局未捕获异常，全部落盘到日志文件。"""
    logger = get_logger()

    def _excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        logger.critical("未捕获异常（主线程）:\n%s", text)

    def _threading_excepthook(args):
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        logger.critical("未捕获异常（子线程 %s）:\n%s",
                        getattr(args.thread, "name", "?"), text)

    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook
