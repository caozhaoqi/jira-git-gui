"""提交记录面板：按 Jira issue（或 best-effort 按仓库）查看关联提交与改动文件。

职责（纯视图）：
- queryRequested(issue_key) : 用户点「查询」时，把 issue 单号交给主窗口去后台拉取
- set_commits(commits)      : 主窗口把后台结果回传，渲染列表
- set_error(msg)            : 查询失败提示
数据获取由 MainWindow 通过 Worker 完成，再把结果回调到这里。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from core.models import Commit


class CommitPanel(QWidget):
    queryRequested = pyqtSignal(str)  # issue_key（空串表示按当前仓库 best-effort）

    def __init__(self, client=None, parent=None):
        super().__init__(parent)
        self.client = client
        self._commits: list = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # 头部说明
        hint = QLabel("查看提交记录：输入 Jira issue 单号（如 TST-234）查询其关联的全部提交与改动文件。"
                      "也可留空，尝试按当前仓库拉取（部分实例不支持）。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(hint)

        # 查询行
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(QLabel("Issue"))
        self.input = QLineEdit()
        self.input.setPlaceholderText("TST-234")
        self.input.returnPressed.connect(self._on_query)
        rl.addWidget(self.input, 1)
        self.btn = QPushButton("查询")
        self.btn.clicked.connect(self._on_query)
        rl.addWidget(self.btn)
        layout.addWidget(row)

        # 列表 + 详情
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._on_selected)
        split.addWidget(self.list)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        mono = self.detail.font()
        self.detail.setStyleSheet(
            "font-family: Menlo, Monaco, 'Courier New', monospace; font-size: 11px;")
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        layout.addWidget(split, 1)

    # ----------------------------------------------------------- 对外接口
    def set_commits(self, commits: list) -> None:
        self._commits = list(commits or [])
        self.list.clear()
        if not self._commits:
            self.detail.setPlainText("（没有查询到提交记录）")
            return
        for c in self._commits:
            it = QListWidgetItem(self._row_text(c))
            it.setData(Qt.ItemDataRole.UserRole, c)
            self.list.addItem(it)
        self.list.setCurrentRow(0)

    def set_error(self, msg: str) -> None:
        self._commits = []
        self.list.clear()
        self.detail.setPlainText("查询失败：\n" + msg)

    def set_querying(self, on: bool) -> None:
        self.btn.setEnabled(not on)
        self.btn.setText("查询中…" if on else "查询")

    # ----------------------------------------------------------- 内部
    @staticmethod
    def _row_text(c: Commit) -> str:
        msg = (c.message or "").splitlines()[0] if c.message else ""
        if len(msg) > 56:
            msg = msg[:55] + "…"
        meta = c.author or "?"
        if c.date:
            meta += f" · {c.date[:10]}"
        return f"{c.display_id}  {msg}\n    {meta}"

    def _on_query(self):
        self.queryRequested.emit(self.input.text().strip())

    def _on_selected(self):
        items = self.list.selectedItems()
        if not items:
            return
        c = items[0].data(Qt.ItemDataRole.UserRole)
        if isinstance(c, Commit):
            self.detail.setPlainText(self._detail_text(c))

    @staticmethod
    def _detail_text(c: Commit) -> str:
        lines = [f"commit  {c.commit_id}",
                 f"Author: {c.author}",
                 f"Date:   {c.date}",
                 f"Branch: {c.branch}" + (f"  (repo: {c.repository_name})" if c.repository_name else ""),
                 "",
                 c.message or "",
                 "",
                 f"变更文件（{len(c.files)}）:"]
        if not c.files:
            lines.append("  （该接口未返回文件清单；如需文件级变更，请确认 showFiles 支持）")
        for f in c.files:
            sign = {"ADDED": "+", "MODIFIED": "M", "DELETED": "D",
                    "RENAMED": "R", "COPIED": "C"}.get(f.change_type.upper(), f.change_type or "?")
            lines.append(f"  [{sign}] {f.path}   +{f.lines_added}/-{f.lines_removed}")
        return "\n".join(lines)
