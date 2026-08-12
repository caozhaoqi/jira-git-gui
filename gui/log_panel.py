"""日志面板。"""
from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("日志"))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(3000)
        self.text.setStyleSheet("font-family: Menlo, Monaco, 'Courier New', monospace; font-size: 11px;")
        layout.addWidget(self.text)

    def append(self, msg: str) -> None:
        self.text.appendPlainText(msg)

    def clear(self) -> None:
        self.text.clear()
