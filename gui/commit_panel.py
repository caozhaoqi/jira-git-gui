"""提交记录面板：按 Jira issue（REST）或本地已克隆仓库（git log）查看提交与改动文件。

职责（纯视图）：
- queryRequested(issue_key, local_mode): 用户点「查询」时，把查询意图交给主窗口后台拉取
- fileViewRequested(commit_id, path)   : 用户在「变更文件」里点某文件，请求查看该历史版本
- set_commits(commits) / set_error(msg) / set_querying(bool)
数据获取由 MainWindow 通过 Worker 完成，再把结果回调到这里。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from core.models import Commit, CommitFile


class CommitPanel(QWidget):
    queryRequested = pyqtSignal(str, bool)  # (issue_key, local_mode)
    fileViewRequested = pyqtSignal(str, str)  # (commit_id, path)

    _CHANGE_SIGN = {
        "ADDED": "+", "MODIFIED": "M", "DELETED": "D", "RENAMED": "R",
        "COPIED": "C", "A": "+", "M": "M", "D": "D", "R": "R", "C": "C",
    }

    def __init__(self, client=None, parent=None):
        super().__init__(parent)
        self.client = client
        self._commits: list = []
        self._selected_commit = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        hint = QLabel("查看提交记录：选「按 Issue 查询」输入 Jira 单号（如 TST-234）；"
                      "或选「本地 Git 仓库」查看已克隆仓库的完整 git log。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(hint)

        # 查询行：模式选择 + issue 输入 + 查询按钮
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rl.addWidget(QLabel("模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["按 Issue 查询", "本地 Git 仓库"])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        rl.addWidget(self.mode_combo)
        self.label_issue = QLabel("Issue")
        rl.addWidget(self.label_issue)
        self.input = QLineEdit()
        self.input.setPlaceholderText("TST-234")
        self.input.returnPressed.connect(self._on_query)
        rl.addWidget(self.input, 1)
        self.btn = QPushButton("查询")
        self.btn.clicked.connect(self._on_query)
        rl.addWidget(self.btn)
        layout.addWidget(row)

        # 左：提交列表；右：详情(上) + 变更文件(下)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.itemSelectionChanged.connect(self._on_selected)
        split.addWidget(self.list)

        right = QSplitter(Qt.Orientation.Vertical)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setStyleSheet(
            "font-family: Menlo, Monaco, 'Courier New', monospace; font-size: 11px;")
        right.addWidget(self.detail)
        self.files = QListWidget()
        self.files.itemClicked.connect(self._on_file_clicked)
        right.addWidget(self.files)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        layout.addWidget(split, 1)

    # ----------------------------------------------------------- 对外接口
    def set_commits(self, commits: list) -> None:
        self._commits = list(commits or [])
        self.list.clear()
        self.files.clear()
        self._selected_commit = None
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
        self.files.clear()
        self.detail.setPlainText("查询失败：\n" + msg)

    def set_querying(self, on: bool) -> None:
        self.btn.setEnabled(not on)
        self.btn.setText("查询中…" if on else "查询")

    # ----------------------------------------------------------- 内部
    def _on_mode_changed(self, idx: int) -> None:
        local = idx == 1
        self.label_issue.setText("仓库" if local else "Issue")
        self.input.setPlaceholderText("(当前仓库)" if local else "TST-234")

    def _on_query(self) -> None:
        local_mode = self.mode_combo.currentIndex() == 1
        self.queryRequested.emit(self.input.text().strip(), local_mode)

    def _on_selected(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        c = items[0].data(Qt.ItemDataRole.UserRole)
        if isinstance(c, Commit):
            self._selected_commit = c
            self.detail.setPlainText(self._detail_text(c))
            self._render_files(c)

    def _render_files(self, c: Commit) -> None:
        self.files.clear()
        if not c.files:
            self.files.addItem("（该提交无文件清单 / 接口未返回）")
            return
        for f in c.files:
            sign = self._CHANGE_SIGN.get(f.change_type.upper(), f.change_type or "?")
            it = QListWidgetItem(f"[{sign}] {f.path}")
            it.setData(Qt.ItemDataRole.UserRole, f.path)
            self.files.addItem(it)

    def _on_file_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path or not isinstance(self._selected_commit, Commit):
            return
        self.fileViewRequested.emit(self._selected_commit.commit_id, path)

    @staticmethod
    def _row_text(c: Commit) -> str:
        msg = (c.message or "").splitlines()[0] if c.message else ""
        if len(msg) > 56:
            msg = msg[:55] + "…"
        meta = c.author or "?"
        if c.date:
            meta += f" · {c.date[:10]}"
        return f"{c.display_id}  {msg}\n    {meta}"

    @staticmethod
    def _detail_text(c: Commit) -> str:
        lines = [f"commit  {c.commit_id}",
                 f"Author: {c.author}",
                 f"Date:   {c.date}",
                 f"Branch: {c.branch}" + (f"  (repo: {c.repository_name})"
                                          if c.repository_name else ""),
                 "",
                 c.message or "",
                 "",
                 f"变更文件（{len(c.files)}）：单击右侧文件可查看该历史版本内容。"]
        return "\n".join(lines)
