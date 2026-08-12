"""主窗口：布局 + 信号绑定 + 异步任务编排。

关键点（防闪退 / 可追溯）：
- 启动时建立 LogBridge 把日志系统与主窗口日志面板打通（文件 + UI 双写）。
- 所有信号槽用 @safe_slot 包裹，任一槽内异常都会被记录到日志而非让进程崩溃。
- 后台任务的完整 traceback 已由 Worker 写入日志文件。
"""
import sys

from PyQt6.QtCore import QT_VERSION_STR, PYQT_VERSION_STR, Qt
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QToolBar, QLabel, QProgressBar, QPushButton,
    QTabWidget, QStatusBar,
)

from core.client import JiraGitClient
from core.config import load_config
from core.constants import PROXY_URL, DOWNLOAD_DIR
from core.logger import LogBridge, get_logger, set_log_bridge
from core.safe import safe_slot
from gui.connect_dialog import ConnectDialog
from gui.log_panel import LogPanel
from gui.preview_panel import PreviewPanel
from gui.repo_panel import RepoPanel
from gui.tree_panel import TreePanel
from gui.commit_panel import CommitPanel
from workers.tasks import Worker


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
        self.setWindowTitle("Jira Git 通用拉取工具")
        self.resize(1120, 740)

        # 持有活动 worker 引用，避免局部变量被 GC 导致 QThread 在运行中析构而崩（SIGABRT）
        self._workers: list = []

        # 组件
        self.log_panel = LogPanel()
        self.connect_dialog = ConnectDialog(self.client, self)
        self.repo_panel = RepoPanel(self.client, self)
        self.tree_panel = TreePanel()
        self.preview_panel = PreviewPanel()
        self.commit_panel = CommitPanel(self.client, self)

        # —— 日志桥：把核心日志转发到 UI 面板（必须在最早完成）——
        self._bridge = LogBridge()
        set_log_bridge(self._bridge)
        self._bridge.message.connect(self.log_panel.append)
        self._logger = get_logger()
        if self._env_loaded:
            self._log(f"{self._env_path} 有配置文件默认用配置文件")

        # 布局：左(仓库+树) | 右(预览+日志)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.repo_panel)
        left.addWidget(self.tree_panel)
        left.setStretchFactor(0, 0)
        left.setStretchFactor(1, 1)

        right = QSplitter(Qt.Orientation.Vertical)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.preview_panel, "文件预览")
        self.tabs.addTab(self.commit_panel, "提交记录")
        self.tabs.addTab(self.log_panel, "日志")
        right.addWidget(self.tabs)
        right.setStretchFactor(0, 1)

        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(left)
        main.addWidget(right)
        main.setStretchFactor(0, 1)
        main.setStretchFactor(1, 1)
        self.setCentralWidget(main)

        # 工具栏
        tb = QToolBar("主操作", self)
        self.addToolBar(tb)
        self.act_connect = tb.addAction("连接设置")
        self.act_connect.triggered.connect(self._open_connect)
        self.act_clone = tb.addAction("克隆仓库(PAT)")
        self.act_clone.triggered.connect(self._clone)
        self.act_download = tb.addAction("下载选中(Cookie)")
        self.act_download.triggered.connect(self._download)
        self.act_download_all = tb.addAction("下载整个仓库(Cookie)")
        self.act_download_all.triggered.connect(self._download_all)
        self.act_clear_resume = tb.addAction("清空断点")
        self.act_clear_resume.triggered.connect(self._clear_resume)
        self.act_clearlog = tb.addAction("清空日志")
        self.act_clearlog.triggered.connect(self.log_panel.clear)

        # —— 下载进度区（进度条 + 百分比标签 + 取消按钮）——
        self._progress_label = QLabel("")
        self._progress = QProgressBar()
        self._progress.setMinimum(0)
        self._progress.setMaximum(0)        # 先以“不确定”模式显示
        self._progress.setFixedWidth(240)
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
               on_progress=None):
        w = Worker(fn, *args)
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
        if self.client.branch:
            self._log(f"已用分支「{self.client.branch}」加载文件树，共 {len(entries)} 项。")
        else:
            self._log("文件树为空（未能确定可用分支或该仓库无可见文件）。")

    @safe_slot
    def _set_children(self, item, entries):
        self.tree_panel.set_children(item, entries)

    def _load_children(self, item, path):
        self._spawn(
            self.client.list_level, path,
            on_finished=lambda entries: self._set_children(item, entries),
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

    # ----------------------------------------------------------- 提交记录
    @safe_slot
    def _on_query_commits(self, issue_key: str):
        self.commit_panel.set_querying(True)
        self._log(f"查询提交记录：issue={issue_key or '(按当前仓库 best-effort)'}")
        self._spawn(
            self.client.get_commits, issue_key, self.client.repo_id, self.client.branch,
            on_finished=lambda commits: self._on_commits_loaded(commits, issue_key),
            on_error=lambda m: self._on_commits_error(m),
        )

    @safe_slot
    def _on_commits_loaded(self, commits, issue_key):
        self.commit_panel.set_querying(False)
        self.commit_panel.set_commits(commits)
        self._log(f"提交记录：共 {len(commits)} 条"
                  + (f"（issue {issue_key}）" if issue_key else "（仓库 best-effort）"))

    @safe_slot
    def _on_commits_error(self, m: str):
        self.commit_panel.set_querying(False)
        self.commit_panel.set_error(m)
        self._log(f"提交查询失败：{m}")

    # ----------------------------------------------------------- 状态栏
    def _update_status(self) -> None:
        mode = self.client.config.mode.upper()
        rid = self.client.repo_id or "-"
        br = self.client.branch or "(默认)"
        cookie_ok = "已配置" if self.client.config.cookie else "未配置"
        pat_ok = "已配置" if self.client.config.pat else "未配置"
        self.statusBar().showMessage(
            f"模式 {mode} | 仓库 {rid} | 分支 {br} | "
            f"Cookie {cookie_ok} | PAT {pat_ok}")


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
    def _download(self):
        if not self.client.config.cookie:
            self._log("下载功能仅 Cookie 模式可用。请在连接设置中填入会话 Cookie。")
            return
        paths = self.tree_panel.collect_checked()
        if not paths:
            self._log("未勾选任何文件。请在文件树「选择」列勾选要下载的文件。")
            return
        self._log(f"开始下载 {len(paths)} 个文件（支持断点续传）…")
        self._begin_progress()
        self._dl_worker = self._spawn(
            self.client.download, paths,
            on_finished=self._on_download_done,
            on_error=lambda m: self._log(f"下载异常：{m}"),
            on_log=self._log,
            on_progress=self._on_progress,
        )

    @safe_slot
    def _on_download_done(self, res):
        ok_list, fail_list, dest, skipped = res
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
        self._log(f"开始递归下载整个仓库 {self.client.repo_id}（Cookie 模式，支持断点续传）…")
        self._begin_progress()
        self._dl_worker = self._spawn(
            self.client.download_repo, self.client.repo_id, self.client.branch,
            on_finished=self._on_download_repo_done,
            on_error=lambda m: self._log(f"整库下载异常：{m}"),
            on_log=self._log,
            on_progress=self._on_progress,
        )

    @safe_slot
    def _on_download_repo_done(self, res):
        ok_count, fail_list, dest, skipped = res
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
