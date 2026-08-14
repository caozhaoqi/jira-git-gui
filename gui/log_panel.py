"""日志面板。"""
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        title = QLabel("日志")
        title.setObjectName("section-title")
        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self.clear)
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.btn_clear)
        layout.addLayout(header)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(3000)
        self.text.setStyleSheet("font-family: Menlo, Monaco, 'Courier New', monospace; font-size: 12px;")
        layout.addWidget(self.text)

    def append(self, msg: str) -> None:
        self.text.appendPlainText(msg)

    def clear(self) -> None:
        self.text.clear()
