#!/usr/bin/env python3
"""Jira Git 通用拉取工具 —— PyQt6 桌面版入口。

运行：
    ./venv/bin/python main.py
或（已激活 venv）：
    python main.py
"""
import sys

from PyQt6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Jira Git 通用拉取工具")
    app.setOrganizationName("jira-git-gui")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
