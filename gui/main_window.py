"""主窗口：布局 + 信号绑定 + 异步任务编排。

关键点（防闪退 / 可追溯）：
- 启动时建立 LogBridge 把日志系统与主窗口日志面板打通（文件 + UI 双写）。
- 所有信号槽用 @safe_slot 包裹，任一槽内异常都会被记录到日志而非让进程崩溃。
- 后台任务的完整 traceback 已由 Worker 写入日志文件。
"""
import sys

from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR, Qt, QSettings
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QToolBar, QLabel, QProgressBar, QPushButton,
    QTabWidget, QStatusBar, QSpinBox, QWidget, QHBoxLayout, QApplication,
)

from core.client import JiraGitClient, DEFAULT_DOWNLOAD_WORKERS
from core.config import load_config, load_session, save_session, clear_session
from core.constants import DEFAULT_REQUEST_QPS
from core.constants import PROXY_URL, DOWNLOAD_DIR
from core.logger import LogBridge, get_logger, set_log_bridge
from core.safe import safe_slot
from gui.styles import apply_global_style
from gui.connect_dialog import ConnectDialog
from gui.log_panel import LogPanel
from gui.preview_panel import PreviewPanel
from gui.repo_panel import RepoPanel
from gui.tree_panel import TreePanel
from gui.commit_panel import CommitPanel
from gui.k8s_panel import K8sPanel, yaml_get_task, yaml_apply_task, net_task
from workers.tasks import Worker
from core.k8s import run_snapshot
from core import k8s_manager as k8s_mgr


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.client = JiraGitClient()
        # 启动即尝试从项目根 .env 载入默认连接配置（jira_url / PAT / Cookie）
        self._env_loaded = False
        self._env_path = ""
        _cfg, self._env_loaded, self._env_path = load_config()
        if self._env_loaded:
            self.client.set_config(_cfg)
        # 从 session.json 读取上次保存的 Cookie（优先级高于 .env）
        _sess = load_session()
        if _sess.get("cookie"):
            self.client.config.cookie = _sess["cookie"]
            if _sess.get("jira_url") and not self.client.config.jira_url:
                self.client.config.jira_url = _sess["jira_url"]
            if _sess.get("username") and not self.client.config.username:
                self.client.config.username = _sess["username"]
        self.setWindowTitle("Jira Git 通用拉取工具")
        self.resize(1280, 800)
        self.setMinimumSize(900, 600)

        # 持有活动 worker 引用，避免局部变量被 GC 导致 QThread 在运行中析构而崩（SIGABRT）
        self._workers: list = []

        # 组件
        self.log_panel = LogPanel()
        self.connect_dialog = ConnectDialog(self.client, self)
        self.repo_panel = RepoPanel(self.client, self)
        self.tree_panel = TreePanel()
        self.preview_panel = PreviewPanel()
        self.commit_panel = CommitPanel(self.client, self)
        self.k8s_panel = K8sPanel(self)

        # —— 日志桥：把核心日志转发到 UI 面板（必须在最早完成）——
        self._bridge = LogBridge()
        set_log_bridge(self._bridge)
        self._bridge.message.connect(self.log_panel.append)
        self._logger = get_logger()
        if self._env_loaded:
            self._log(f"{self._env_path} 有配置文件默认用配置文件")

        # 布局：左(仓库+树) | 右(预览/提交/日志)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.repo_panel)
        left.addWidget(self.tree_panel)
        left.setStretchFactor(0, 0)
        left.setStretchFactor(1, 1)
        left.setSizes([260, 500])

        right = QSplitter(Qt.Orientation.Vertical)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.preview_panel, "文件预览")
        self.tabs.addTab(self.commit_panel, "提交记录")
        self.tabs.addTab(self.k8s_panel, "K8s 快照")
        self.tabs.addTab(self.log_panel, "日志")
        right.addWidget(self.tabs)
        right.setStretchFactor(0, 1)

        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(left)
        main.addWidget(right)
        main.setStretchFactor(0, 2)
        main.setStretchFactor(1, 3)
        main.setSizes([480, 800])
        self.setCentralWidget(main)

        # 工具栏
        tb = QToolBar("主操作", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        # 连接设置
        self.act_connect = tb.addAction("连接设置")
        self.act_connect.triggered.connect(self._open_connect)
        tb.addSeparator()

        # 克隆 / 下载
        self.act_clone = tb.addAction("克隆仓库 (PAT)")
        self.act_clone.triggered.connect(self._clone)
        self.act_download = tb.addAction("下载选中 (Cookie)")
        self.act_download.triggered.connect(self._download)
        self.act_download_all = tb.addAction("下载整个仓库 (Cookie)")
        self.act_download_all.triggered.connect(self._download_all)
        tb.addSeparator()

        # 维护操作
        self.act_clear_resume = tb.addAction("清空断点")
        self.act_clear_resume.triggered.connect(self._clear_resume)
        self.act_clearlog = tb.addAction("清空日志")
        self.act_clearlog.triggered.connect(self.log_panel.clear)
        tb.addSeparator()

        # —— 下载并发数（UI 可调）——
        tb.addWidget(QLabel("并发"))
        self._max_workers = DEFAULT_DOWNLOAD_WORKERS
        self._concurrency = QSpinBox()
        self._concurrency.setRange(1, 16)
        self._concurrency.setValue(self._max_workers)
        self._concurrency.setSuffix(" 线程")
        self._concurrency.setFixedWidth(86)
        self._concurrency.valueChanged.connect(self._on_concurrency_changed)
        tb.addWidget(self._concurrency)

        # —— 请求速率（QPS，UI 可调，保护服务器）——
        tb.addWidget(QLabel("速率"))
        self._qps = DEFAULT_REQUEST_QPS
        self._rate = QSpinBox()
        self._rate.setRange(1, 50)
        self._rate.setValue(self._qps)
        self._rate.setSuffix(" 请求/秒")
        self._rate.setFixedWidth(104)
        self._rate.setToolTip("对 Jira 服务器的稳态请求速率上限；调小更温和，调大更快。"
                              "批量下载的并发线程再多，总速率也被它钳住，避免打崩服务器。")
        self._rate.valueChanged.connect(self._on_rate_changed)
        tb.addWidget(self._rate)
        tb.addSeparator()

        # 主题切换（浅色 / 深色）
        _cur_theme = (QApplication.instance().property("theme") or "light")
        self._btn_theme = tb.addAction("☀ 主题" if _cur_theme == "dark" else "🌓 主题")
        self._btn_theme.triggered.connect(self._toggle_theme)

        # 弹性间隔
        spacer = QWidget()
        spacer.setFixedWidth(12)
        tb.addWidget(spacer)

        # —— 下载进度区（进度条 + 百分比标签 + 取消按钮）——
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #6b7280; font-size: 12px;")
        self._progress = QProgressBar()
        self._progress.setMinimum(0)
        self._progress.setMaximum(0)        # 先以“不确定”模式显示
        self._progress.setFixedWidth(220)
        self._progress.setTextVisible(True)
        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.setFixedWidth(56)
        self._btn_cancel.setVisible(False)
        self._btn_cancel.clicked.connect(self._cancel_download)
        tb.addWidget(self._progress_label)
        tb.addWidget(self._progress)
        tb.addWidget(self._btn_cancel)
        self._dl_worker = None

        # 信号
        self.repo_panel.repoSelected.connect(self._on_repo_selected)
        self.tree_panel.requestRoot.connect(self._load_root)
        self.tree_panel.requestChildren.connect(self._load_children)
        self.tree_panel.fileActivated.connect(self._open_file)
        self.commit_panel.queryRequested.connect(self._on_query_commits)
        self.commit_panel.fileViewRequested.connect(self._on_file_at_commit)

        # K8s 快照：面板发出抓取/取消意图，由主窗口在后台线程执行
        self.k8s_panel.snapshotRequested.connect(self._on_k8s_snapshot)
        self.k8s_panel.cancelRequested.connect(self._on_k8s_cancel)
        self.k8s_panel.yamlGetRequested.connect(self._on_k8s_yaml_get)
        self.k8s_panel.yamlApplyRequested.connect(self._on_k8s_yaml_apply)
        self.k8s_panel.netRequested.connect(self._on_k8s_net)
        self._k8s_worker = None

        self._log_startup_banner()
        self._log("就绪。先点「连接设置」配置 Jira 地址 / 账号 / 模式，再在仓库面板选择或指定仓库。")
        self._log(f"当前模式：{self.client.config.mode.upper()}；代理："
                  f"{'已探测 ' + PROXY_URL if PROXY_URL else '无'}")

        # 状态栏：实时反映 模式 / 当前仓库 / 分支 / 连接状态
        self.setStatusBar(QStatusBar())
        self._update_status()

    # ----------------------------------------------------------- 工具
    def _log(self, msg: str) -> None:
        self._logger.info(msg)

    def _log_startup_banner(self) -> None:
        import os
        self._logger.info("=" * 60)
        self._logger.info("应用启动")
        self._logger.info("Python     : %s", sys.version.replace("\n", " "))
        self._logger.info("PyQt       : %s (Qt %s)", PYQT_VERSION_STR, QT_VERSION_STR)
        self._logger.info("工作目录   : %s", os.getcwd())
        self._logger.info("代理       : %s", PROXY_URL or "无")
        self._logger.info("日志文件   : 见 logs/jira_git_gui.log")
        self._logger.info("=" * 60)

    def _spawn(self, fn, *args, on_finished=None, on_error=None, on_log=None,
               on_progress=None, **kwargs):
        w = Worker(fn, *args, **kwargs)
        if on_finished:
            w.result.connect(on_finished)
        if on_error:
            w.error.connect(on_error)
        if on_log:
            w.log.connect(on_log)
        if on_progress:
            w.progress.connect(on_progress)
        # 保留引用直到线程彻底结束；内置 finished 触发后删除，避免“Destroyed while running”
        self._workers.append(w)
        w.finished.connect(lambda: self._workers.remove(w))
        w.finished.connect(w.deleteLater)
        w.start()
        return w

    @safe_slot
    def _open_connect(self):
        self.connect_dialog.exec()
        self._update_status()

    # ----------------------------------------------------------- 仓库 / 树
    @safe_slot
    def _on_repo_selected(self, rid, rname, branch):
        self.client.set_repo(rid, rname, branch)
        self._update_status()
        self._log(f"已选择仓库 id={rid} name={rname or '(待探测)'} branch={branch or '(默认)'}")
        self._load_root()

    @safe_slot
    def _load_root(self):
        if not self.client.repo_id:
            self._log("尚未指定仓库（repoId）。请在连接设置或仓库面板中填写。")
            return
        self.tree_panel.clear()
        self._log("加载文件树根目录…")
        self._spawn(
            self.client.list_level, "",
            on_finished=lambda entries: self._on_root_loaded(entries),
            on_error=lambda m: self._log(f"加载树失败：{m}"),
        )

    @safe_slot
    def _on_root_loaded(self, entries):
        self.tree_panel.set_root_entries(entries)
        # 分支可能在 list_level 内被自动探测确定，同步状态栏（避免仍显示「(默认)」）
        self._update_status()
        if self.client.branch:
            self._log(f"已用分支「{self.client.branch}」加载文件树，共 {len(entries)} 项。")
        else:
            self._log("文件树为空（未能确定可用分支或该仓库无可见文件）。")

    @safe_slot
    def _set_children(self, path, entries):
        # 回调时再按 path 重新解析「活的」节点引用。
        # 若在请求期间切换了仓库 / 重新加载了根目录（tree.clear() 已销毁旧节点），
        # 这里查不到节点，直接丢弃过期结果，避免访问已销毁的 QTreeWidgetItem 崩溃。
        item = self.tree_panel.find_item_by_path(path)
        if item is None:
            return
        self.tree_panel.set_children(item, entries)

    def _load_children(self, path):
        self._spawn(
            self.client.list_level, path,
            on_finished=lambda entries: self._set_children(path, entries),
            on_error=lambda m: self._log(f"加载子目录失败 {path}：{m}"),
        )

    @safe_slot
    def _open_file(self, path):
        self.preview_panel.set_loading(path)
        self._spawn(
            self.client.get_file, path,
            # Worker 的 result 信号只回传一个返回值(get_file 的元组)，
            # 用闭包把 path 一并带入 _on_file，避免 “missing 1 required positional argument: 'path'”
            on_finished=lambda res: self._on_file(res, path),
            on_error=lambda m: self.preview_panel.set_error(m),
        )

    @safe_slot
    def _on_file(self, res, path):
        content, err = res
        if err:
            self.preview_panel.set_error(err)
            self._log(f"读取文件失败 {path}: {err}")
        else:
            self.preview_panel.set_content(content, path)
        # get_file 内部可能自动探测分支，刷新状态栏
        self._update_status()

    # ----------------------------------------------------------- 提交记录
    @safe_slot
    def _on_query_commits(self, issue_key: str, local_mode: bool):
        self.commit_panel.set_querying(True)
        if local_mode:
            if not self.client.repo_id:
                self.commit_panel.set_querying(False)
                self.commit_panel.set_error(
                    "本地 Git 模式需要先在仓库面板选择/指定一个仓库，"
                    "且该仓库已通过 PAT 模式克隆到本地。")
                self._log("本地 Git 查询：未指定仓库，已取消。")
                return
            self._log(f"查询本地 Git 提交：repo={self.client.repo_id}"
                      f" branch={self.client.branch or '(默认)'}")
            self._spawn(
                self.client.get_local_commits, self.client.repo_id, self.client.branch,
                on_finished=lambda commits: self._on_commits_loaded(commits, "(本地 git log)"),
                on_error=lambda m: self._on_commits_error(m),
            )
        else:
            if not issue_key and not self.client.repo_id:
                self.commit_panel.set_querying(False)
                self.commit_panel.set_error(
                    "请先在仓库面板选择/指定一个仓库，或在上方填入 Jira issue 单号"
                    "（如 TST-234）后再查询。")
                self._log("提交查询：未指定仓库也未填 issue，已取消。")
                return
            self._log(f"查询提交记录：issue={issue_key or '(按当前仓库 best-effort)'}")
            self._spawn(
                self.client.get_commits, issue_key, self.client.repo_id, self.client.branch,
                on_finished=lambda commits: self._on_commits_loaded(commits, issue_key),
                on_error=lambda m: self._on_commits_error(m),
            )

    @safe_slot
    def _on_file_at_commit(self, commit_id: str, path: str):
        """在预览标签页展示某次提交中某文件的历史版本。"""
        self.tabs.setCurrentWidget(self.preview_panel)
        self.preview_panel.set_loading(f"{path} @ {commit_id[:8]}")
        self._log(f"查看历史版本：commit {commit_id[:8]} 的 {path}")
        self._spawn(
            self.client.get_file_at_commit, self.client.repo_id, commit_id, path,
            on_finished=lambda res: self._on_history_file(res, path, commit_id),
            on_error=lambda m: self.preview_panel.set_error(m),
        )

    @safe_slot
    def _on_history_file(self, res, path: str, commit_id: str):
        content, err = res
        if err:
            self.preview_panel.set_error(err)
            self._log(f"查看历史文件失败 {path}: {err}")
        else:
            self.preview_panel.set_content(
                content, f"{path}  (commit {commit_id[:8]})")
        self._update_status()

    @safe_slot
    def _on_commits_loaded(self, commits, issue_key):
        self.commit_panel.set_querying(False)
        self.commit_panel.set_commits(commits)
        self._log(f"提交记录：共 {len(commits)} 条"
                  + (f"（issue {issue_key}）" if issue_key else "（仓库 best-effort）"))

    @safe_slot
    def _on_commits_error(self, m: str):
        self.commit_panel.set_querying(False)
        # Worker 抛来的 m 是完整 traceback，这里只提取最后一段异常信息作为友好提示，
        # 不再把整段堆栈甩到面板（例如：请先在仓库面板选择/指定仓库…）。
        friendly = m
        if "Traceback" in m:
            lines = [ln.strip() for ln in m.strip().splitlines() if ln.strip()]
            picked = None
            for ln in reversed(lines):
                if "Error" in ln or "Exception" in ln:
                    picked = ln.split(":", 1)[-1].strip()
                    if picked:
                        break
            friendly = picked or (lines[-1] if lines else m)
        self.commit_panel.set_error(friendly)
        self._log(f"提交查询失败：{friendly}")

    # ----------------------------------------------------------- K8s 快照
    @safe_slot
    def _on_k8s_snapshot(self, opts: dict) -> None:
        if self._k8s_worker is not None and self._k8s_worker.isRunning():
            self._log("K8s 快照正在执行中，请先取消或等待完成。")
            return
        # 若指定环境，解析其 kubeconfig / 命名空间（覆盖裸参数）
        env = opts.get("env")
        if env:
            try:
                kc, ns = k8s_mgr.resolve_env_kubeconfig(env)
                opts["kubeconfig"] = kc or opts.get("kubeconfig")
                if ns and not opts.get("namespace"):
                    opts["namespace"] = ns
            except Exception as ex:
                self.k8s_panel.set_error(str(ex))
                self._log("环境解析失败：%s" % ex)
                return
        self.k8s_panel.set_running(True)
        self.k8s_panel.append_log("开始抓取 K8s 快照…")
        self.tabs.setCurrentWidget(self.k8s_panel)
        # 注意：run_snapshot 的 on_log/on_progress 由 Worker 默认转成
        # log/progress 信号（经队列投递到主线程），故这里只 connect 信号，
        # 不在 _spawn 里直接传 UI 回调（否则会在工作线程里触碰控件而崩溃）。
        w = self._spawn(
            run_snapshot, opts,
            on_finished=self._on_k8s_done,
            # 异常（含 UserError 干净文案）直接交给面板展示
            on_error=self._on_k8s_error,
        )
        w.log.connect(self.k8s_panel.append_log)
        w.log.connect(lambda m: self._log(m))
        w.progress.connect(self.k8s_panel.set_progress)
        self._k8s_worker = w

    @safe_slot
    def _on_k8s_done(self, result: dict) -> None:
        self.k8s_panel.set_running(False)
        self.k8s_panel.set_result(result)
        sm = result.get("summary", {})
        self._log("K8s 快照完成：共 %d，异常 %d，报告 %s"
                  % (sm.get("total", 0), sm.get("high", 0), result.get("report")))
        self._k8s_worker = None

    @safe_slot
    def _on_k8s_error(self, msg: str) -> None:
        self.k8s_panel.set_running(False)
        self.k8s_panel.set_error(msg)
        self._log("K8s 快照失败：%s" % msg)
        self._k8s_worker = None

    @safe_slot
    def _on_k8s_cancel(self) -> None:
        if self._k8s_worker is not None:
            self._k8s_worker.cancel()
            self._log("已请求取消 K8s 快照。")

    # ----------------------------------------------------------- K8s Pod YAML
    @safe_slot
    def _on_k8s_yaml_get(self, req: dict) -> None:
        self.k8s_panel.set_busy(True, "yaml")
        self.tabs.setCurrentWidget(self.k8s_panel)
        w = self._spawn(
            yaml_get_task, req["env"], req["kind"], req["name"], req.get("namespace"),
            req.get("clean", True),
            on_finished=lambda yaml_text: self.k8s_panel.set_yaml(yaml_text),
            on_error=lambda m: self.k8s_panel.set_error(m),
        )
        w.finished.connect(lambda: self.k8s_panel.set_busy(False, "yaml"))

    @safe_slot
    def _on_k8s_yaml_apply(self, req: dict) -> None:
        self.k8s_panel.set_busy(True, "yaml")
        self.tabs.setCurrentWidget(self.k8s_panel)
        w = self._spawn(
            yaml_apply_task, req["env"], req["kind"], req["name"],
            req.get("namespace"), req["content"],
            on_finished=lambda res: self.k8s_panel.set_yaml_result(res),
            on_error=lambda m: self.k8s_panel.set_error(m),
        )
        w.finished.connect(lambda: self.k8s_panel.set_busy(False, "yaml"))

    # ----------------------------------------------------------- K8s 网络检测
    @safe_slot
    def _on_k8s_net(self, req: dict) -> None:
        self.k8s_panel.set_busy(True, "net")
        self.tabs.setCurrentWidget(self.k8s_panel)
        w = self._spawn(
            net_task, req["env"], req.get("extra_hosts"),
            on_finished=lambda res: self.k8s_panel.set_net_result(res),
            on_error=lambda m: self.k8s_panel.set_error(m),
        )
        w.log.connect(self.k8s_panel.append_log)
        w.finished.connect(lambda: self.k8s_panel.set_busy(False, "net"))

    # ----------------------------------------------------------- 状态栏
    def _update_status(self) -> None:
        mode = self.client.config.mode.upper()
        rid = self.client.repo_id or "-"
        br = self.client.branch or "(默认)"
        cookie_ok = "已配置" if self.client.config.cookie else "未配置"
        pat_ok = "已配置" if self.client.config.pat else "未配置"
        self.statusBar().showMessage(
            f"模式 {mode} | 仓库 {rid} | 分支 {br} | "
            f"Cookie {cookie_ok} | PAT {pat_ok} | 速率 {self._qps}/秒")


    # ----------------------------------------------------------- 主题切换
    @safe_slot
    def _toggle_theme(self) -> None:
        app = QApplication.instance()
        cur = (app.property("theme") if app else None) or "light"
        new = "dark" if cur == "light" else "light"
        apply_global_style(app, new)
        if app is not None:
            app.setProperty("theme", new)
        try:
            QSettings("jira-git-gui", "JiraGitGUI").setValue("theme", new)
        except Exception:
            pass
        self._btn_theme.setText("☀ 主题" if new == "dark" else "🌓 主题")
        # 刷新代码高亮配色（高亮器按主题属性取色）
        try:
            hl = getattr(self.preview_panel, "_highlighter", None)
            if hl is not None:
                hl.set_theme(new)
        except Exception:
            pass

    # ----------------------------------------------------------- 克隆 / 下载
    @safe_slot
    def _clone(self):
        if not self.client.repo_id:
            self._log("请先指定仓库 ID。")
            return
        if not self.client.config.pat:
            self._log("当前未配置 PAT，无法克隆。请在连接设置中选择 PAT 模式并填入 Token。")
            return
        if not self.client.repo_name:
            self._log("缺少仓库名(repo_name)。若已配置 Cookie 可先「测试连接」自动探测，或手动填写。")
            return
        self._log(f"开始克隆仓库 {self.client.repo_id} …（PAT 模式下「测试连接」也会触发真实克隆）")
        self._spawn(
            self.client.clone_repo, self.client.repo_id, self.client.repo_name,
            self.client.branch, self.client.config.pat, self.client.config.username,
            on_finished=self._on_clone_done,
            on_error=lambda m: self._log(f"克隆异常：{m}"),
            on_log=self._log,
        )

    @safe_slot
    def _on_clone_done(self, res):
        ok, msg, path = res
        self._log(f"克隆结果：{msg}")
        if ok:
            self._log(f"本地路径：{path}。现在以本地模式加载文件树。")
            self._load_root()

    @safe_slot
    def _on_concurrency_changed(self, v: int) -> None:
        self._max_workers = int(v)
        self._log(f"下载并发数已设为 {self._max_workers}")

    @safe_slot
    def _on_rate_changed(self, v: int) -> None:
        self._qps = int(v)
        self.client.set_rate_limit(self._qps)
        self._update_status()
        self._log(f"请求速率上限已设为 {self._qps} 请求/秒")

    @safe_slot
    def _download(self):
        if not self.client.config.cookie:
            self._log("下载功能仅 Cookie 模式可用。请在连接设置中填入会话 Cookie。")
            return
        paths = self.tree_panel.collect_checked()
        if not paths:
            self._log("未勾选任何文件。请在文件树「选择」列勾选要下载的文件。")
            return
        self._log(f"开始下载 {len(paths)} 个文件（并发 {self._max_workers}，支持断点续传）…")
        self._begin_progress()
        self._dl_worker = self._spawn(
            self.client.download, paths,
            max_workers=self._max_workers,
            on_finished=self._on_download_done,
            on_error=lambda m: self._log(f"下载异常：{m}"),
            on_log=self._log,
            on_progress=self._on_progress,
        )

    @safe_slot
    def _on_download_done(self, res):
        ok_list, fail_list, dest, skipped = res
        self._update_status()  # 下载期内可能自动探测了分支
        self._end_progress()
        self._log(f"下载完成：成功 {len(ok_list)}（其中跳过已存在 {skipped}），"
                  f"失败 {len(fail_list)}。")
        for f in fail_list:
            self._log(f"  ✗ {f['path']}: {f['reason']}")
        if ok_list:
            self._log(f"已保存到：{dest}")

    @safe_slot
    def _download_all(self):
        if not self.client.config.cookie:
            self._log("整库下载仅 Cookie 模式可用。请在连接设置中填入会话 Cookie。")
            return
        if not self.client.repo_id:
            self._log("请先指定/选择一个仓库，再点「下载整个仓库(Cookie)」。")
            return
        self._log(f"开始递归下载整个仓库 {self.client.repo_id}（Cookie 模式，并发 {self._max_workers}，支持断点续传）…")
        self._begin_progress()
        self._dl_worker = self._spawn(
            self.client.download_repo, self.client.repo_id, self.client.branch,
            max_workers=self._max_workers,
            on_finished=self._on_download_repo_done,
            on_error=lambda m: self._log(f"整库下载异常：{m}"),
            on_log=self._log,
            on_progress=self._on_progress,
        )

    @safe_slot
    def _on_download_repo_done(self, res):
        ok_count, fail_list, dest, skipped = res
        self._update_status()  # 下载期内可能自动探测了分支
        self._end_progress()
        self._log(f"整库下载结束：新增 {ok_count} 个，跳过已存在 {skipped} 个，"
                  f"失败 {len(fail_list)} 个。")
        for f in fail_list[:20]:
            self._log(f"  ✗ {f['path']}: {f['reason']}")
        if fail_list:
            self._log(f"  （失败项共 {len(fail_list)} 个，仅显示前 20；"
                      f"再次点击可继续，失败项会被重试）")
        if ok_count or skipped:
            self._log(f"已保存到：{dest}（断点续传清单：{dest}/"
                      f"{JiraGitClient._MANIFEST_NAME}）")

    # ----------------------------------------------------- 进度条 / 取消 / 断点
    def _begin_progress(self) -> None:
        self._progress.setMaximum(0)   # 不确定模式，待总数确定后切换
        self._progress.setValue(0)
        self._progress_label.setText("准备中…")
        self._btn_cancel.setVisible(True)

    def _on_progress(self, done: int, total: int, path: str) -> None:
        if total and total > 0:
            self._progress.setMaximum(total)
            self._progress.setValue(done)
            pct = done * 100 // total
            self._progress_label.setText(f"{done}/{total} ({pct}%)")
        else:
            self._progress.setMaximum(0)
            self._progress_label.setText(f"已处理 {done} …")
        # 不再为每个文件写一行日志（整库几百文件会刷屏）；进度条已实时反映，
        # 仅失败项与汇总在 done 回调里记录。

    def _end_progress(self) -> None:
        self._btn_cancel.setVisible(False)
        self._progress.setMaximum(1)
        self._progress.setValue(1)
        self._progress_label.setText("完成")
        self._dl_worker = None

    @safe_slot
    def _cancel_download(self):
        if self._dl_worker:
            self._dl_worker.cancel()
            self._log("已请求取消下载，将在当前文件处理完后停止"
                      "（断点续传：再次点击同一仓库即可从断点继续）。")

    @safe_slot
    def _clear_resume(self):
        if not self.client.repo_id:
            self._log("请先选择仓库，再清空断点。")
            return
        mp = DOWNLOAD_DIR / str(self.client.repo_id) / JiraGitClient._MANIFEST_NAME
        if mp.exists():
            try:
                mp.unlink()
                self._log(f"已清空断点续传清单：{mp}（下次下载将重新拉取全部文件）")
            except Exception as ex:
                self._log(f"清空断点失败：{ex}")
        else:
            self._log("当前没有断点续传清单（无需清空）。")
