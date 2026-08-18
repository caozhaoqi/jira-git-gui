"""连接设置对话框。"""
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton, QVBoxLayout,
    QWidget,
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
        self.resize(640, 520)
        self.setMinimumWidth(560)

        # 外层垂直布局
        outer = QVBoxLayout(self)
        outer.setSpacing(14)
        outer.setContentsMargins(20, 20, 20, 20)

        # ===== 1. 基础配置：两列并排 =====
        url_box = QGroupBox("基础配置")
        url_form = QFormLayout(url_box)
        url_form.setSpacing(10)
        url_form.setContentsMargins(12, 16, 12, 12)
        url_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.url = QLineEdit()
        self.url.setPlaceholderText("https://jira.example.com")
        self.user = QLineEdit()

        url_form.addRow("Jira 地址", self.url)
        url_form.addRow("用户名", self.user)
        outer.addWidget(url_box)

        # ===== 2. 认证方式 + 凭证（一体化） =====
        auth_box = QGroupBox("认证方式")
        auth_layout = QVBoxLayout(auth_box)
        auth_layout.setSpacing(10)
        auth_layout.setContentsMargins(12, 16, 12, 12)

        # 模式选择：水平排列
        mode_row = QHBoxLayout()
        self.mode_pat = QRadioButton("PAT 模式")
        self.mode_pat.setToolTip("git clone 全量，含嵌套文件")
        self.mode_cookie = QRadioButton("Cookie 模式")
        self.mode_cookie.setToolTip("Web 抓取，仅根目录文件")
        self.mode_pat.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_pat, 1)
        self.mode_group.addButton(self.mode_cookie, 2)

        mode_row.addWidget(self.mode_pat)
        mode_row.addWidget(self.mode_cookie)
        mode_row.addStretch(1)
        auth_layout.addLayout(mode_row)

        # 凭证区域
        cred_container = QWidget()
        cred_layout = QVBoxLayout(cred_container)
        cred_layout.setContentsMargins(0, 4, 0, 0)
        cred_layout.setSpacing(8)

        # PAT 凭证
        self.pat_widget = QWidget()
        pat_layout = QVBoxLayout(self.pat_widget)
        pat_layout.setContentsMargins(0, 0, 0, 0)
        pat_layout.setSpacing(6)
        pat_label = QLabel("Personal Access Token")
        pat_label.setStyleSheet("color: #6b7280; font-size: 12px;")
        self.pat = QLineEdit()
        self.pat.setEchoMode(QLineEdit.EchoMode.Password)
        self.pat.setPlaceholderText("粘贴你的 PAT")
        pat_layout.addWidget(pat_label)
        pat_layout.addWidget(self.pat)

        # Cookie 凭证
        self.cookie_widget = QWidget()
        cookie_layout = QVBoxLayout(self.cookie_widget)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.setSpacing(6)
        cookie_label = QLabel("Cookie")
        cookie_label.setStyleSheet("color: #6b7280; font-size: 12px;")
        self.cookie = QPlainTextEdit()
        self.cookie.setMinimumHeight(64)
        self.cookie.setMaximumHeight(80)
        self.cookie.setPlaceholderText("JSESSIONID=...; atlassian.xsrf.token=...")
        cookie_layout.addWidget(cookie_label)
        cookie_layout.addWidget(self.cookie)

        cred_layout.addWidget(self.pat_widget)
        cred_layout.addWidget(self.cookie_widget)
        self.cookie_widget.setVisible(False)

        auth_layout.addWidget(cred_container)
        outer.addWidget(auth_box)

        # ===== 3. 仓库设置（可折叠） =====
        self.repo_box = QGroupBox("仓库设置（可选）")
        self.repo_box.setCheckable(True)
        self.repo_box.setChecked(False)
        repo_form = QFormLayout()
        repo_form.setSpacing(10)
        repo_form.setContentsMargins(12, 16, 12, 12)
        repo_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.repo_id = QLineEdit()
        self.branch = QLineEdit()
        self.repo_name = QLineEdit()
        self.repo_name.setPlaceholderText("PAT 克隆需要；可留空由 Cookie 探测")

        repo_form.addRow("仓库 ID", self.repo_id)
        repo_form.addRow("分支", self.branch)
        repo_form.addRow("仓库名", self.repo_name)
        self.repo_box.setLayout(repo_form)
        outer.addWidget(self.repo_box)

        # ===== 4. 状态信息 =====
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #6b7280; font-size: 12px; padding: 0 4px;")
        self.status.setMinimumHeight(24)
        outer.addWidget(self.status)

        # ===== 5. 底部按钮行 =====
        btn_row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setObjectName("primary")
        self.btn_test.clicked.connect(self._test)
        btn_row.addWidget(self.btn_test)
        btn_row.addStretch(1)

        self.btn_ok = QPushButton("确定")
        self.btn_ok.clicked.connect(self._ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)
        outer.addLayout(btn_row)

        self.mode_group.buttonClicked.connect(self._on_mode)
        self._on_mode()

        # 回显已有配置
        self._fill_from_client()

    # ----------------------------------------------------------- UI 逻辑
    def _on_mode(self):
        pat = self.mode_pat.isChecked()
        self.pat_widget.setVisible(pat)
        self.cookie_widget.setVisible(not pat)

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
        # 有预设值时展开仓库设置
        if self.client.repo_id or self.client.branch or self.client.repo_name:
            self.repo_box.setChecked(True)
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
        self.status.setStyleSheet("color: #6b7280; font-size: 12px; padding: 0 4px;")
        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中…")
        w = Worker(self.client.connect)
        w.result.connect(self._on_test_done)
        w.error.connect(self._on_test_error)
        w.result.connect(lambda *_: (self.btn_test.setEnabled(True), self.btn_test.setText("测试连接")))
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
                self.status.setStyleSheet("color: #dc2626; font-size: 12px; padding: 0 4px;")
        else:
            self.status.setStyleSheet("color: #6b7280; font-size: 12px; padding: 0 4px;")
        self.status.setText(" | ".join(parts))

    @safe_slot
    def _on_test_error(self, tb_text: str):
        # error 信号携带的是完整 traceback；UI 只显示首行，全文已写入日志文件
        first_line = tb_text.strip().splitlines()[-1] if tb_text.strip() else "未知错误"
        self.status.setText(f"错误：{first_line}（完整堆栈见日志文件）")
        self.status.setStyleSheet("color: #dc2626; font-size: 12px; padding: 0 4px;")
        self._log.error("【测试连接】异常：\n%s", tb_text)

    @safe_slot
    def _ok(self):
        self._apply()
        # 确定时也保存 Cookie（不要求先测试）
        cfg = self.client.config
        if cfg.mode == "cookie" and cfg.cookie:
            save_session(cfg.cookie, cfg.jira_url, cfg.username)
        self.accept()
