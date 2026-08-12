"""仓库面板：从 AllRepositories 页面发现仓库 / 手动指定仓库并加载文件树。"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,QHBoxLayout
)

from core.client import JiraGitClient
from core.logger import get_logger
from core.models import RepoInfo
from core.safe import safe_slot
from workers.tasks import Worker


class RepoPanel(QWidget):
    # repo_id, repo_name, branch
    repoSelected = pyqtSignal(str, str, str)

    def __init__(self, client: JiraGitClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._log = get_logger()
        self._workers: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(QLabel("已发现仓库（需 Cookie 模式；读取 AllRepositories 页面）"))

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_double)
        layout.addWidget(self.list)

        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        self.btn_discover = QPushButton("发现仓库")
        self.btn_discover.clicked.connect(self._discover)
        self.btn_view = QPushButton("查看文件")
        self.btn_view.clicked.connect(self._on_view_clicked)
        br.addWidget(self.btn_discover)
        br.addWidget(self.btn_view)
        layout.addWidget(btn_row)

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
    @safe_slot
    def _discover(self):
        if not self.client.config.cookie:
            self.list.clear()
            QListWidgetItem("(未配置 Cookie，无法发现仓库)", self.list)
            return
        self._log.info("【发现仓库】开始（AllRepositories 页面）")
        self.btn_discover.setEnabled(False)
        self.btn_discover.setText("发现中…")
        w = Worker(self.client.discover_repos)
        w.result.connect(self._on_discovered)
        w.error.connect(self._on_discover_error)
        w.result.connect(lambda *_: self._restore_discover())
        self._workers.append(w)
        w.finished.connect(lambda: self._workers.remove(w))
        w.finished.connect(w.deleteLater)
        w.start()

    def _restore_discover(self):
        self.btn_discover.setEnabled(True)
        self.btn_discover.setText("发现仓库")

    @safe_slot
    def _on_discovered(self, repos):
        self._log.info("【发现仓库】返回 %d 个", len(repos) if repos else 0)
        self.list.clear()
        if not repos:
            QListWidgetItem("(未发现仓库，或该账号无权限)", self.list)
            return
        for r in repos:
            label = f"{r.display_name}  (id={r.repo_id})"
            if r.default_branch:
                label += f"  [branch={r.default_branch}]"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, r)
            self.list.addItem(it)

    @safe_slot
    def _on_discover_error(self, tb_text: str):
        self._log.error("【发现仓库】异常：\n%s", tb_text)
        self._restore_discover()

    def _selected_repo(self) -> RepoInfo | None:
        item = self.list.currentItem()
        if not item:
            return None
        r = item.data(Qt.ItemDataRole.UserRole)
        return r if isinstance(r, RepoInfo) else None

    @safe_slot
    def _on_view_clicked(self):
        r = self._selected_repo()
        if not r:
            self._log.info("请先在列表中选择一个仓库，再点「查看文件」")
            return
        self._emit_repo(r)

    @safe_slot
    def _on_double(self, item):
        r = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(r, RepoInfo):
            self._emit_repo(r)

    def _emit_repo(self, r: RepoInfo):
        branch = r.default_branch or self.br.text().strip()
        self.repoSelected.emit(r.repo_id, r.display_name, branch)

    @safe_slot
    def _apply_manual(self):
        rid = self.rid.text().strip()
        if not rid:
            return
        self.repoSelected.emit(rid, self.rname.text().strip(), self.br.text().strip())
