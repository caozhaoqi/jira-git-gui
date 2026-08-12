"""主窗口：布局 + 信号绑定 + 异步任务编排。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QToolBar,
)

from core.client import JiraGitClient
from core.constants import PROXY_URL
from gui.connect_dialog import ConnectDialog
from gui.log_panel import LogPanel
from gui.preview_panel import PreviewPanel
from gui.repo_panel import RepoPanel
from gui.tree_panel import TreePanel
from workers.tasks import Worker


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.client = JiraGitClient()
        self.setWindowTitle("Jira Git 通用拉取工具")
        self.resize(1120, 740)

        # 组件
        self.log_panel = LogPanel()
        self.connect_dialog = ConnectDialog(self.client, self)
        self.repo_panel = RepoPanel(self.client, self)
        self.tree_panel = TreePanel()
        self.preview_panel = PreviewPanel()

        # 布局：左(仓库+树) | 右(预览+日志)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.repo_panel)
        left.addWidget(self.tree_panel)
        left.setStretchFactor(0, 0)
        left.setStretchFactor(1, 1)

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.preview_panel)
        right.addWidget(self.log_panel)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 1)

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
        self.act_clearlog = tb.addAction("清空日志")
        self.act_clearlog.triggered.connect(self.log_panel.clear)

        # 信号
        self.repo_panel.repoSelected.connect(self._on_repo_selected)
        self.tree_panel.requestRoot.connect(self._load_root)
        self.tree_panel.requestChildren.connect(self._load_children)
        self.tree_panel.fileActivated.connect(self._open_file)

        self._log("就绪。先点「连接设置」配置 Jira 地址 / 账号 / 模式，再在仓库面板选择或指定仓库。")
        self._log(f"当前模式：{self.client.config.mode.upper()}；代理："
                  f"{'已探测' if PROXY_URL else '无'}")

    # ----------------------------------------------------------- 工具
    def _log(self, msg: str) -> None:
        self.log_panel.append(msg)

    def _spawn(self, fn, *args, on_finished=None, on_error=None, on_log=None):
        w = Worker(fn, *args)
        if on_finished:
            w.finished.connect(on_finished)
        if on_error:
            w.error.connect(on_error)
        if on_log:
            w.log.connect(on_log)
        w.finished.connect(w.deleteLater)
        w.error.connect(w.deleteLater)
        w.start()
        return w

    def _open_connect(self):
        self.connect_dialog.exec()

    # ----------------------------------------------------------- 仓库 / 树
    def _on_repo_selected(self, rid, rname, branch):
        self.client.set_repo(rid, rname, branch)
        self._log(f"已选择仓库 id={rid} name={rname or '(待探测)'} branch={branch or '(默认)'}")
        self._load_root()

    def _load_root(self):
        if not self.client.repo_id:
            self._log("尚未指定仓库（repoId）。请在连接设置或仓库面板中填写。")
            return
        self.tree_panel.clear()
        self._log("加载文件树根目录…")
        self._spawn(
            self.client.list_level, "",
            on_finished=self.tree_panel.set_root_entries,
            on_error=lambda m: self._log(f"加载树失败：{m}"),
        )

    def _load_children(self, item, path):
        self._spawn(
            self.client.list_level, path,
            on_finished=lambda entries, it=item: self.tree_panel.set_children(it, entries),
            on_error=lambda m: self._log(f"加载子目录失败 {path}：{m}"),
        )

    def _open_file(self, path):
        self.preview_panel.set_loading(path)
        self._spawn(
            self.client.get_file, path,
            on_finished=lambda res, p=path: self._on_file(res, p),
            on_error=lambda m: self.preview_panel.set_error(m),
        )

    def _on_file(self, res, path):
        content, err = res
        if err:
            self.preview_panel.set_error(err)
            self._log(f"读取文件失败 {path}: {err}")
        else:
            self.preview_panel.set_content(content, path)

    # ----------------------------------------------------------- 克隆 / 下载
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
        self._log(f"开始克隆仓库 {self.client.repo_id} …")
        self._spawn(
            self.client.clone_repo, self.client.repo_id, self.client.repo_name,
            self.client.branch, self.client.config.pat, self.client.config.username,
            on_finished=self._on_clone_done,
            on_error=lambda m: self._log(f"克隆异常：{m}"),
            on_log=self._log,
        )

    def _on_clone_done(self, res):
        ok, msg, path = res
        self._log(f"克隆结果：{msg}")
        if ok:
            self._log(f"本地路径：{path}。现在以本地模式加载文件树。")
            self._load_root()

    def _download(self):
        if not self.client.config.cookie:
            self._log("下载功能仅 Cookie 模式可用。请在连接设置中填入会话 Cookie。")
            return
        paths = self.tree_panel.collect_checked()
        if not paths:
            self._log("未勾选任何文件。请在文件树「选择」列勾选要下载的文件（仅根目录文件支持）。")
            return
        self._log(f"开始下载 {len(paths)} 个文件…")
        self._spawn(
            self.client.download, paths,
            on_finished=self._on_download_done,
            on_error=lambda m: self._log(f"下载异常：{m}"),
            on_log=self._log,
        )

    def _on_download_done(self, res):
        ok_list, fail_list, dest = res
        self._log(f"下载完成：成功 {len(ok_list)}，失败 {len(fail_list)}。")
        for f in fail_list:
            self._log(f"  ✗ {f['path']}: {f['reason']}")
        if ok_list:
            self._log(f"已保存到：{dest}")
