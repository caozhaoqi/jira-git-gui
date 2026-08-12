"""代码预览面板。"""
import os

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget


class PreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.title = QLabel("预览")
        layout.addWidget(self.title)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Menlo, Monaco, Consolas, monospace", 11))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.text)

    def set_content(self, text: str, path: str = "") -> None:
        self.title.setText(f"预览 · {path}" if path else "预览")
        self.text.setPlainText(text or "")

    def set_loading(self, path: str = "") -> None:
        self.title.setText(f"加载中 · {path}" if path else "加载中…")
        self.text.setPlainText("")

    def set_error(self, msg: str) -> None:
        self.title.setText("错误")
        self.text.setPlainText(msg)

    @staticmethod
    def size_fmt(n) -> str:
        if n is None:
            return ""
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} K"
        return f"{n / 1024 / 1024:.1f} M"
