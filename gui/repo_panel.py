"""仓库面板：发现仓库 / 手动指定仓库并加载文件树。"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from core.client import JiraGitClient
from workers.tasks import Worker


class RepoPanel(QWidget):
    # repo_id, repo_name, branch
    repoSelected = pyqtSignal(str, str, str)

    def __init__(self, client: JiraGitClient, parent=None):
        super().__init__(parent)
        self.client = client

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("已发现仓库（需 Cookie 模式）"))

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_double)
        layout.addWidget(self.list)

        self.btn_discover = QPushButton("发现仓库")
        self.btn_discover.clicked.connect(self._discover)
        layout.addWidget(self.btn_discover)

        grp = QGroupBox("手动指定")
        gl = QFormLayout(grp)
        self.rid = QLineEdit()
        self.rname = QLineEdit()
        self.br = QLineEdit()
        gl.addRow("仓库 ID", self.rid)
        gl.addRow("仓库名", self.rname)
        gl.addRow("分支", self.br)
        self.btn_apply = QPushButton("加载文件树")
        self.btn_apply.clicked.connect(self._apply_manual)
        gl.addRow("", self.btn_apply)
        layout.addWidget(grp)

    # ----------------------------------------------------------- 行为
    def _discover(self):
        if not self.client.config.cookie:
            self.list.clear()
            QListWidgetItem("(未配置 Cookie，无法发现仓库)", self.list)
            return
        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("发现中…")
        w = Worker(self.client.discover_repos)
        w.finished.connect(self._on_discovered)
        w.error.connect(lambda m: self._restore_discover())
        w.finished.connect(lambda *_: self._restore_discover())
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        w.start()

    def _restore_discover(self):
        self.btn_discover.setEnabled(True)
        self.btn_discover.setText("发现仓库")

    def _on_discovered(self, repos):
        self.list.clear()
        if not repos:
            QListWidgetItem("(未发现仓库，或该账号无权限)", self.list)
            return
        for r in repos:
            it = QListWidgetItem(f"{r.display_name}  (id={r.repo_id})")
            it.setData(Qt.ItemDataRole.UserRole, r)
            self.list.addItem(it)

    def _on_double(self, item):
        r = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(r, object) and getattr(r, "repo_id", None):
            self.repoSelected.emit(r.repo_id, r.display_name, self.br.text().strip())

    def _apply_manual(self):
        rid = self.rid.text().strip()
        if not rid:
            return
        self.repoSelected.emit(rid, self.rname.text().strip(), self.br.text().strip())
