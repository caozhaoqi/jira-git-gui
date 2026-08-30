"""core.client 子包 —— 由拆分后的子模块聚合而成，对外符号保持兼容。"""

# ---- 以下来自原 core/client.py（聚合兼容层）----
"""JiraGitClient —— 与 Jira Git 插件（Xiplink/BigBrassBand）交互的核心客户端。

支持两种模式：
  - PAT 模式    : 用 Personal Access Token 走 git clone，全量拿到（含嵌套文件）
  - Cookie 模式 : 用 JSESSIONID 会话走 Web 抓取，浏览树 + 下载根目录文件

本模块不依赖任何 GUI 框架，可在任意线程中调用；耗时操作（clone/download）
接受可选的 on_log 回调用于进度上报。

为控制单文件规模，原单体实现已按功能域拆分为多个 Mixin 子模块，本文件仅做
聚合组装（保留 ``__init__`` 与共享状态初始化，方法由各 Mixin 提供）：
  - ``client_connection`` : 连接 / HTTP 基础
  - ``client_repos``      : 仓库发现
  - ``client_browse``     : 浏览 / 树 / 提交
  - ``client_files``      : 文件读取 / 批量下载
  - ``client_clone``      : git 克隆 / PAT 诊断 / 断点续传清单

对外 API（``JiraGitClient`` 的方法名与签名）完全不变。
"""
import subprocess
import time
from typing import Optional

import httpx

from core.constants import (HTTP_TIMEOUT, PROXY_URL, DEFAULT_REQUEST_QPS,
                         DEFAULT_DOWNLOAD_WORKERS, REPOS_DIR)
from core.errors import UserError
from core.models import ConnectConfig, RepoInfo, TreeEntry, Commit, CommitFile
from core import throttle
from core.watchdog import NetworkWatchdog
from core.logger import get_logger

from .connection import (ConnectionMixin, _should_backoff, _backoff_for)
from .repos import ReposMixin
from .browse import BrowseMixin
from .files import FilesMixin
from .clone import CloneMixin

logger = get_logger("jira-git-gui")


class JiraGitClient(ConnectionMixin, ReposMixin, BrowseMixin, FilesMixin, CloneMixin):
    """Jira Git 插件客户端（聚合各功能 Mixin）。

    共享实例状态（HTTP 连接池、配置、缓存、git 路径等）在此统一初始化，
    各 Mixin 的方法通过 ``self`` 访问，无需各自定义 ``__init__``。
    """

    def __init__(self, git_bin: str = "git"):
        self._git_bin = git_bin
        self._http_client: Optional["httpx.Client"] = None
        self.config = ConnectConfig(mode="cookie")
        self.repo_id = ""
        self.repo_name = ""
        self.branch = ""
        # 分支 / HEAD 解析结果缓存，避免每个目录层级都重复探测
        self._branch_cache: dict = {}
        self._head_cache: dict = {}
        # REST 端点可用性结论（一旦确认不可用，本次会话内跳过 REST 探测以省请求）
        self._rest_unavailable = False
        # 最近一次扫描是否出现「登录态失效」信号（跳转登录页 / 401 / 403）
        self._last_auth_failed = False

