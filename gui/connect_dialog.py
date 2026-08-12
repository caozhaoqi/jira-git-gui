"""连接设置对话框。"""
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
)
from PyQt6.QtCore import Qt

from core.client import JiraGitClient
from core.logger import get_logger
from core.models import ConnectConfig
from core.safe import safe_slot
from workers.tasks import Worker


class ConnectDialog(QDialog):
    def __init__(self, client: JiraGitClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._log = get_logger()
        self._workers: list = []
        self.setWindowTitle("连接设置")
        self.resize(540, 420)

        layout = QFormLayout(self)

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://jira.example.com")
        self.user = QLineEdit()

        self.mode_pat = QRadioButton("PAT 模式（git clone 全量，含嵌套文件）")
        self.mode_cookie = QRadioButton("Cookie 模式（Web 抓取，仅根目录文件）")
        self.mode_pat.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_pat, 1)
        self.mode_group.addButton(self.mode_cookie, 2)

        self.pat = QLineEdit()
        self.pat.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie = QPlainTextEdit()
        self.cookie.setMaximumHeight(72)
        self.cookie.setPlaceholderText("JSESSIONID=...; atlassian.xsrf.token=...")

        self.repo_id = QLineEdit()
        self.branch = QLineEdit()
        self.repo_name = QLineEdit()
        self.repo_name.setPlaceholderText("PAT 克隆需要；可留空由 Cookie 探测")

        layout.addRow("Jira 地址", self.url)
        layout.addRow("用户名", self.user)
        layout.addRow("模式", self.mode_pat)
        layout.addRow("", self.mode_cookie)
        layout.addRow("PAT", self.pat)
        layout.addRow("Cookie 会话", self.cookie)
        layout.addRow("仓库 ID", self.repo_id)
        layout.addRow("分支", self.branch)
        layout.addRow("仓库名(repo_name)", self.repo_name)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.btn_test = QPushButton("测试连接")
        self.btn_test.clicked.connect(self._test)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.clicked.connect(self._ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_test)
        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)

        layout.addRow("", btn_row)
        layout.addRow("", self.status)

        self.mode_group.buttonClicked.connect(self._on_mode)
        self._on_mode()

        # 回显已有配置
        self._fill_from_client()

    # ----------------------------------------------------------- UI 逻辑
    def _on_mode(self):
        pat = self.mode_pat.isChecked()
        self.pat.setEnabled(pat)
        self.cookie.setEnabled(not pat)

    def _fill_from_client(self):
        c = self.client.config
        self.url.setText(c.jira_url)
        self.user.setText(c.username)
        if c.mode == "cookie":
            self.mode_cookie.setChecked(True)
        else:
            self.mode_pat.setChecked(True)
        self.pat.setText(c.pat)
        self.cookie.setPlainText(c.cookie)
        self.repo_id.setText(self.client.repo_id)
        self.branch.setText(self.client.branch)
        self.repo_name.setText(self.client.repo_name)
        self._on_mode()

    def get_config(self) -> ConnectConfig:
        return ConnectConfig(
            jira_url=self.url.text().strip().rstrip("/"),
            username=self.user.text().strip(),
            mode="pat" if self.mode_pat.isChecked() else "cookie",
            pat=self.pat.text().strip(),
            cookie=self.cookie.toPlainText().strip(),
        )

    def _apply(self):
        self.client.set_config(self.get_config())
        self.client.set_repo(
            self.repo_id.text().strip(),
            self.repo_name.text().strip(),
            self.branch.text().strip(),
        )

    @safe_slot
    def _test(self):
        self._apply()
        self._log.info("【测试连接】开始：url=%s mode=%s repoId=%s",
                       self.client.config.jira_url, self.client.config.mode,
                       self.client.repo_id or "(空)")
        self.status.setText("测试中…（PAT 模式会触发真实克隆，可能耗时）")
        self.btn_test.setEnabled(False)
        w = Worker(self.client.connect)
        w.result.connect(self._on_test_done)
        w.error.connect(self._on_test_error)
        w.result.connect(lambda *_: self.btn_test.setEnabled(True))
        # 保留引用直到线程结束，避免局部变量被 GC 导致 QThread 运行中析构崩溃
        self._workers.append(w)
        w.finished.connect(lambda: self._workers.remove(w))
        w.finished.connect(w.deleteLater)
        w.start()

    @safe_slot
    def _on_test_done(self, res: dict):
        self._log.info("【测试连接】返回：%s", res)
        parts = []
        parts.append("Cookie ✓" if res.get("cookieOk") else "Cookie ✗")
        if res.get("patTest"):
            pt = res["patTest"]
            parts.append(f"PAT {'✓' if pt['ok'] else '✗'}: {pt['msg']}")
        if res.get("repoDefaults") and res["repoDefaults"].get("displayName"):
            self.repo_name.setText(res["repoDefaults"]["displayName"])
            parts.append(f"仓库名已探测: {res['repoDefaults']['displayName']}")
        if res.get("note"):
            parts.append(res["note"])
        self.status.setText(" | ".join(parts))

    @safe_slot
    def _on_test_error(self, tb_text: str):
        # error 信号携带的是完整 traceback；UI 只显示首行，全文已写入日志文件
        first_line = tb_text.strip().splitlines()[-1] if tb_text.strip() else "未知错误"
        self.status.setText(f"错误：{first_line}（完整堆栈见日志文件）")
        self._log.error("【测试连接】异常：\n%s", tb_text)

    @safe_slot
    def _ok(self):
        self._apply()
        self.accept()
