#!/usr/bin/env python3
"""Jira Git 通用拉取工具 —— PyQt6 桌面版入口。

运行：
    ./venv/bin/python main.py
或（已激活 venv）：
    python main.py
"""
import sys

# 在任何 import / Qt 初始化之前就打开 faulthandler，
# 万一发生 C 级段错误（segmentation fault），能把回溯写到日志文件，便于定位。
import faulthandler

from core.logger import LOG_DIR

_fault_fd = open(str(LOG_DIR / "faulthandler.log"), "a", buffering=1)
faulthandler.enable(_fault_fd)

from PyQt6.QtWidgets import QApplication

from core.logger import LOG_PATH, get_logger, install_global_hooks
from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    # 全局异常 / Qt 消息接管：任何未捕获异常都写入 logs/jira_git_gui.log（完整 traceback）
    install_global_hooks()
    app.setApplicationName("Jira Git 通用拉取工具")
    app.setOrganizationName("jira-git-gui")

    win = MainWindow()
    win.show()

    log = get_logger()
    log.info("主窗口已显示；完整日志见 %s", LOG_PATH)
    try:
        sys.exit(app.exec())
    except Exception:
        log.exception("事件循环异常退出")
        raise


if __name__ == "__main__":
    main()
