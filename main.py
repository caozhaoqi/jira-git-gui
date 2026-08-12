#!/usr/bin/env python3
"""Jira Git 通用拉取工具 —— PyQt6 桌面版入口。

运行（任选其一）：
    ./venv/bin/python main.py        # 直接用项目 venv
    python3 main.py                  # 任意 python 亦可，main.py 会自动切到 venv
    ./run.sh                         # 一键启动脚本（macOS / Linux）
    open run.command                 # macOS 双击启动
"""
import os
import sys


def _ensure_venv_python():
    """若当前解释器缺少 PyQt6，自动 re-exec 到项目 venv 的解释器。

    这样无论用哪个 python 启动 main.py，都不会因缺依赖而启动失败
    （典型场景：用系统 python 直接跑，报 ModuleNotFoundError: No module named 'PyQt6'）。
    若连 venv 都没有，则保持原样启动，由后续 import 给出清晰报错。
    """
    try:
        import PyQt6  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    my_exe = os.path.abspath(sys.executable)
    for name in ("venv", ".venv"):
        for sub in ("bin/python", "Scripts/python.exe"):
            cand = os.path.join(here, name, sub)
            if os.path.exists(cand) and os.path.abspath(cand) != my_exe:
                os.execv(cand, [cand, __file__, *sys.argv[1:]])


_ensure_venv_python()

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
