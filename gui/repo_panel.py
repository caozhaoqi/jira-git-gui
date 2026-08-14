"""仓库面板：从 AllRepositories 页面发现仓库 / 手动指定仓库并加载文件树。"""
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QFormLayout, QGroupBox, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget, QHBoxLayout
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
        self._all_repos: list = []
        self._click_timer = None  # 防 单/双击重复触发

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel("仓库列表")
        title.setObjectName("section-title")
        layout.addWidget(title)

        hint = QLabel("已发现仓库（需 Cookie 模式；读取 AllRepositories 页面）")
        hint.setObjectName("hint")
        layout.addWidget(hint)

        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索仓库名 / ID / 分支…")
        self.search.textChanged.connect(self._on_search)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.setMinimumHeight(120)
        self.list.itemClicked.connect(self._on_clicked)
        self.list.itemDoubleClicked.connect(self._on_double)
        layout.addWidget(self.list)

        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(6)
        self.btn_discover = QPushButton("发现仓库")
        self.btn_discover.setObjectName("primary")
        self.btn_discover.clicked.connect(self._discover)
        self.btn_view = QPushButton("查看文件")
        self.btn_view.clicked.connect(self._on_view_clicked)
        br.addWidget(self.btn_discover)
        br.addWidget(self.btn_view)
        layout.addWidget(btn_row)

        grp = QGroupBox("手动指定仓库")
        gl = QFormLayout(grp)
        self.rid = QLineEdit()
        self.rid.setPlaceholderText("例如 12345")
        self.rname = QLineEdit()
        self.br = QLineEdit()
        self.rid.returnPressed.connect(self._apply_manual)
        self.rname.returnPressed.connect(self._apply_manual)
        self.br.returnPressed.connect(self._apply_manual)
        gl.addRow("仓库 ID", self.rid)
        gl.addRow("仓库名", self.rname)
        gl.addRow("分支", self.br)
        self.btn_apply = QPushButton("加载文件树")
        self.btn_apply.setObjectName("primary")
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
        self._all_repos = list(repos or [])
        self._apply_filter(self.search.text().strip())

    @safe_slot
    def _on_search(self, text: str):
        self._apply_filter(text.strip())

    def _apply_filter(self, keyword: str):
        self.list.clear()
        if not self._all_repos:
            QListWidgetItem("(未发现仓库，先点击「发现仓库」)", self.list)
            return
        keyword_l = keyword.lower()
        matched = []
        for r in self._all_repos:
            if not keyword_l:
                matched.append(r)
                continue
            if (keyword_l in (r.display_name or "").lower()
                    or keyword_l in str(r.repo_id).lower()
                    or keyword_l in (r.default_branch or "").lower()):
                matched.append(r)
        if not matched:
            QListWidgetItem(f"(无匹配项，关键字：{keyword})", self.list)
            return
        for r in matched:
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
    def _on_clicked(self, item):
        """单击列表项：延迟加载，若紧随双击到来则取消（避免重复）。"""
        r = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(r, RepoInfo):
            return
        # 取消前一个待定的单击回调
        if self._click_timer is not None:
            try:
                self._click_timer.stop()
            except Exception:
                pass
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(250)  # Qt 双击阈值通常 400ms，取一半足够
        self._click_timer.timeout.connect(lambda: self._delayed_emit(r))
        self._click_timer.start()

    def _delayed_emit(self, r: RepoInfo):
        """单击延迟 250ms 后真正触发（若中途双击会取消）。"""
        self._click_timer = None
        self._emit_repo(r)

    @safe_slot
    def _on_view_clicked(self):
        r = self._selected_repo()
        if not r:
            self._log.info("请先在列表中选择一个仓库，再点「查看文件」")
            return
        self._emit_repo(r)

    @safe_slot
    def _on_double(self, item):
        """双击：取消待触发的单击加载，然后立即加载。"""
        if self._click_timer is not None:
            try:
                self._click_timer.stop()
            except Exception:
                pass
            self._click_timer = None
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
