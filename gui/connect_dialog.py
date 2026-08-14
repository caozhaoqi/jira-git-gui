"""连接设置对话框。"""
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QVBoxLayout,
)
from PyQt6.QtCore import Qt

from core.client import JiraGitClient
from core.config import save_session, clear_session
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
        self.resize(580, 480)

        # 外层垂直布局
        outer = QVBoxLayout(self)
        outer.setSpacing(12)
        outer.setContentsMargins(16, 16, 16, 16)

        # 基础字段表单
        form = QFormLayout()
        form.setSpacing(8)
        form.setContentsMargins(0, 0, 0, 0)

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://jira.example.com")
        self.user = QLineEdit()

        form.addRow("Jira 地址", self.url)
        form.addRow("用户名", self.user)
        outer.addLayout(form)

        # 模式选择分组
        self.mode_pat = QRadioButton("PAT 模式（git clone 全量，含嵌套文件）")
        self.mode_cookie = QRadioButton("Cookie 模式（Web 抓取，仅根目录文件）")
        self.mode_pat.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_pat, 1)
        self.mode_group.addButton(self.mode_cookie, 2)

        mode_box = QGroupBox("模式选择")
        mode_layout = QHBoxLayout(mode_box)
        mode_layout.addWidget(self.mode_pat)
        mode_layout.addWidget(self.mode_cookie)
        mode_layout.addStretch(1)
        outer.addWidget(mode_box)

        # PAT 分组
        self.pat = QLineEdit()
        self.pat.setEchoMode(QLineEdit.EchoMode.Password)
        pat_box = QGroupBox("PAT")
        pat_form = QFormLayout(pat_box)
        pat_form.addRow("Token", self.pat)
        outer.addWidget(pat_box)

        # Cookie 分组
        self.cookie = QPlainTextEdit()
        self.cookie.setMinimumHeight(60)
        self.cookie.setMaximumHeight(72)
        self.cookie.setPlaceholderText("JSESSIONID=...; atlassian.xsrf.token=...")
        cookie_box = QGroupBox("Cookie 会话")
        cookie_form = QFormLayout(cookie_box)
        cookie_form.addRow("Cookie", self.cookie)
        outer.addWidget(cookie_box)

        # 仓库设置分组
        self.repo_id = QLineEdit()
        self.branch = QLineEdit()
        self.repo_name = QLineEdit()
        self.repo_name.setPlaceholderText("PAT 克隆需要；可留空由 Cookie 探测")
        repo_box = QGroupBox("仓库设置")
        repo_form = QFormLayout(repo_box)
        repo_form.addRow("仓库 ID", self.repo_id)
        repo_form.addRow("分支", self.branch)
        repo_form.addRow("仓库名(repo_name)", self.repo_name)
        outer.addWidget(repo_box)

        # 状态标签
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #6b7280; font-size: 12px;")
        self.status.setContentsMargins(0, 8, 0, 0)

        # 底部按钮行
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setObjectName("primary")
        self.btn_test.clicked.connect(self._test)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.clicked.connect(self._ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_test)
        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)
        outer.addWidget(self.status)

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
        # Cookie 持久化反馈
        cfg = self.get_config()
        if cfg.mode == "cookie" and cfg.cookie:
            if res.get("cookieOk"):
                save_session(cfg.cookie, cfg.jira_url, cfg.username)
                parts.append("Cookie 已保存到本地，下次启动自动读取")
            else:
                parts.append("Cookie 验证失败，可能已过期，请重新获取")
                self.status.setStyleSheet("color: #dc2626; font-size: 12px;")
        else:
            self.status.setStyleSheet("color: #6b7280; font-size: 12px;")
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
        # 确定时也保存 Cookie（不要求先测试）
        cfg = self.client.config
        if cfg.mode == "cookie" and cfg.cookie:
            save_session(cfg.cookie, cfg.jira_url, cfg.username)
        self.accept()
