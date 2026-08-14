"""JiraGitClient —— 与 Jira Git 插件（Xiplink/BigBrassBand）交互的核心客户端。

支持两种模式：
  - PAT 模式    : 用 Personal Access Token 走 git clone，全量拿到（含嵌套文件）
  - Cookie 模式 : 用 JSESSIONID 会话走 Web 抓取，浏览树 + 下载根目录文件

本模块不依赖任何 GUI 框架，可在任意线程中调用；耗时操作（clone/download）
接受可选的 on_log 回调用于进度上报。
"""
import base64
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from .constants import PROXY_URL, HTTP_TIMEOUT, REPOS_DIR, DOWNLOAD_DIR, DEFAULT_REQUEST_QPS
from .errors import UserError
from .models import ConnectConfig, RepoInfo, TreeEntry, Commit, CommitFile
from . import throttle
from core.logger import get_logger

logger = get_logger("jira-git-gui")

# REST 仓库列表翻页参数。
# 经实测，Xiplink「Git Integration for Jira」6.x 的用户级全量端点
# /rest/gitplugin/1.0/repository/all 采用 offset/limit 分页，且 limit 上限为 100
#（limit>100 直接 400），startAt/maxResults 约定对其无效（被忽略，永远回前 100）。
REST_PAGE_SIZE = 100   # 每页仓库数（服务端硬上限 100）
REST_MAX_PAGES = 500  # 安全上限：最多翻 500 页（5w 仓库），防止异常时死循环

# 仓库发现候选 REST 端点（按优先级）。第一个是 6.x 实测可用端点；
# 其余为旧版/其它部署的兜底（可能 404，命中后跳过以节省请求）。
REST_ENDPOINTS = (
    "/rest/gitplugin/1.0/repository/all",  # 6.x 用户级全量列表（实测可用，offset/limit 分页）
    "/rest/gitplugin/1.0/repositories",      # 旧版复数（部分实例可用）
    "/rest/gitplugin/latest/repositories",   # latest 版本
    "/rest/git/1.0/repository",             # 另一种旧路径
)

# HTML 全部仓库页翻页参数（Jira GIJRepositoryBrowser 标准分页：pageSize + pageIndex）
HTML_PAGE_SIZE = 100   # 每页仓库数（与 UI 的 View:100 一致，减少总请求数）
HTML_MAX_PAGES = 50    # 安全上限：最多翻 50 页（5000 仓库），防止异常时死循环

DEFAULT_DOWNLOAD_WORKERS = 4  # 整库/批量下载默认并发数（有界线程池，避免压垮服务端）

# 全部仓库浏览页（HTML）：列出所有仓库，每个仓库是一个指向
# GIJBrowseGit.jspa?repoId=XXX&branchName=YYY 的链接。
ALL_REPOS_PAGE = "/secure/GIJRepositoryBrowser-AllRepositories.jspa"

# 匹配仓库链接锚点：<a ... href="...GIJ*.jspa?...repoId=数字...">名称</a>
# group(1)=href, group(2)=repoId, group(3)=锚点文本（可能含标签，需清洗）
_REPO_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="([^"]*?GIJ[A-Za-z]*\.jspa\?[^"]*?repoId=(\d+)[^"]*)"[^>]*>'
    r'(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# 名称明显不是仓库名的噪声锚点，解析时跳过
_NOISE_NAMES = {"", "commits", "files", "branches", "tags", "browse", "view", "clone"}

# 这些状态码表示「请慢一点 / 暂不可用」，需要退避而非立即重试
_BACKOFF_STATUS = {429, 503}

# 这些状态码表示「该 REST 端点对当前实例/账号确实不可用」，
# 命中后本次会话内跳过其余 REST 探测（避免每次发现仓库都白打一堆 404）。
_REST_DEAD_STATUS = {401, 403, 404, 405, 410}


def _should_backoff(r: "httpx.Response") -> bool:
    """判断该响应是否需要退避重试（限流/服务暂不可用）。"""
    return r.status_code in _BACKOFF_STATUS


def _backoff_for(r: "httpx.Response", attempt: int) -> None:
    """根据 Retry-After 头（或指数退避）休眠，避免持续冲击被限流的服务器。"""
    ra = (r.headers.get("Retry-After") or "").strip()
    wait = None
    if ra.isdigit():
        wait = int(ra)
    else:
        try:
            import email.utils
            dt = email.utils.parsedate_to_datetime(ra)
            if dt:
                wait = max(0, (dt.timestamp() - time.time()))
        except Exception:
            wait = None
    if wait is None:
        wait = min(30.0, 2.0 * (attempt + 1))  # 指数退避，封顶 30s
    time.sleep(wait)


class NetworkWatchdog:
    """监控网络请求失败次数，在连续失败超过阈值后触发自动取消。

    设计目标：
    - 当网络长时间中断时（如断网 1 分钟以上），自动停止正在进行的
      扫描/下载/克隆任务，而不是让每个请求都超时重试（40s*5次）后放弃。
    - 任何一次成功的请求都会重置计数器，保证瞬时网络抖动不会误判。
    - 线程安全：所有方法通过 _lock 保护，可在任意工作线程中调用。

    使用方式：
        watchdog = NetworkWatchdog(threshold=5)
        # 将 watchdog.should_abort 嵌入 should_cancel 回调
        # 在 http_get 失败时调用 watchdog.notify_failure(err)
        # 在 http_get 成功时调用 watchdog.notify_success()
    """

    def __init__(self, threshold: int = 5, window: float = 60.0):
        self._threshold = threshold       # 连续失败多少次后判定网络不可用
        self._window = window             # 失败记录的有效期（秒），过期后自动重置
        self._failures = 0
        self._last_failure_time = 0.0
        self._down = False
        self._reason = ""
        self._lock = threading.Lock()

    def notify_failure(self, error_msg: str = "") -> None:
        """记录一次传输层失败（超时/连接拒绝/DNS 失败等）。"""
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self._threshold:
                self._down = True
                self._reason = f"网络连续失败 {self._failures} 次（最近错误：{error_msg[:120]}）"

    def notify_success(self) -> None:
        """请求成功后调用，重置失败计数。"""
        with self._lock:
            self._failures = 0
            self._down = False
            self._reason = ""

    def should_abort(self) -> bool:
        """检查网络是否已判定为不可用。

        若失败记录已超过 window 有效期（可能网络已恢复），自动重置。
        """
        with self._lock:
            if self._down and (time.time() - self._last_failure_time) > self._window:
                self._down = False
                self._failures = 0
                return False
            return self._down

    @property
    def down(self) -> bool:
        with self._lock:
            return self._down

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    def reset(self) -> None:
        """手动重置（例如用户重新连接后）。"""
        with self._lock:
            self._failures = 0
            self._down = False
            self._reason = ""


class JiraGitClient:
    def __init__(self) -> None:
        self.config = ConnectConfig()
        self.repo_id: str = ""
        self.repo_name: str = ""
        self.branch: str = ""
        self._branch_cache: dict = {}  # repo_id -> 已探测到的可用分支（避免重复探测）
        self._head_cache: dict = {}    # (repo_id, branch) -> HEAD commit（避免重复解析）
        self._git_bin = shutil.which("git") or "git"
        # 初始化全局限流器为默认速率（保护服务器），UI 旋钮可热更新
        throttle.set_global_rate_limit(DEFAULT_REQUEST_QPS)
        # 会话内缓存：REST 仓库列表端点经探测确认不可用时置位，
        # 后续 discover_repos 直接跳过 REST，避免每次发现都白打一堆 404。
        self._rest_unavailable: bool = False
        # 网络看门狗：批量操作期间监控网络健康度。由后端在任务启动前设置。
        self._watchdog: Optional[NetworkWatchdog] = None

    # ------------------------------------------------------------------ 配置
    def set_config(self, cfg: ConnectConfig) -> None:
        self.config = cfg
        # 连接配置变化（服务器/账号/cookie）后，REST 可用性结论作废，需重新探测
        self._rest_unavailable = False

    def set_rate_limit(self, qps: float) -> None:
        """设置对外请求速率上限（每秒请求数），热更新全局限流器。

        用于 UI 旋钮：调小可更温和地对待 Jira 服务器，调大可加速抓取。
        """
        throttle.set_global_rate_limit(float(qps))

    def set_repo(self, repo_id: str, repo_name: str = "", branch: str = "") -> None:
        self.repo_id = str(repo_id)
        if repo_name:
            self.repo_name = repo_name
        if branch:
            self.branch = branch
        self._branch_cache.clear()  # 切换仓库后丢弃旧分支探测结果
        self._head_cache.clear()

    @staticmethod
    def host_of(url: str) -> str:
        u = (url or "").strip().rstrip("/")
        m = re.match(r"https?://([^/]+)", u)
        return m.group(1) if m else u

    # ------------------------------------------------------------------ 网络
    @staticmethod
    def http_get(url: str, headers: Optional[dict] = None,
                 retries: int = 5,
                 watchdog: Optional["NetworkWatchdog"] = None) -> httpx.Response:
        """带重试的 GET：每次请求新建客户端（无连接池复用），专门对抗代理偶发的
        SSL UNEXPECTED_EOF / 连接重置；失败自动退避重试。

        发请求前先经全局令牌桶限流（``throttle.acquire``），确保无论并发多大，
        对 Jira 服务器的稳态请求速率都被钳住，避免把对方打崩。遇到 429/503 时
        读取 ``Retry-After`` 头做长退避。

        可选的 watchdog 用于网络连续性监控：连续传输层失败达到阈值时提前中止，
        避免断网状态下每个请求都白白耗尽 retries 次超时。
        """
        last = None
        for attempt in range(retries):
            if watchdog and watchdog.should_abort():
                raise httpx.TransportError(
                    f"网络已中断，自动停止请求（连续失败 {watchdog.failure_count} 次）")
            try:
                throttle.acquire()
                with httpx.Client(
                    timeout=HTTP_TIMEOUT,
                    follow_redirects=True,
                    proxy=PROXY_URL or None,
                    verify=False,
                    headers={"User-Agent": "jira-git-gui/1.0"},
                ) as client:
                    r = client.get(url, headers=headers or {})
                if _should_backoff(r):
                    _backoff_for(r, attempt)
                    last = httpx.HTTPStatusError(
                        f"服务器限流/暂不可用：{r.status_code}",
                        request=r.request, response=r)
                    continue
                if watchdog:
                    watchdog.notify_success()
                return r
            except (httpx.TransportError, httpx.HTTPError) as e:
                last = e
                if watchdog:
                    watchdog.notify_failure(str(e))
                time.sleep(0.6 * (attempt + 1))
        raise last if last else httpx.TransportError("unknown httpx error")

    def _make_client(self) -> "httpx.Client":
        """为批量请求创建可复用的 httpx 客户端（带代理 / 重试参数）。

        与 ``http_get`` 的「每次新建」不同，下载整库时会复用同一个客户端，避免
        每文件重复 TCP/TLS 握手，显著降低大批量下载的耗时。客户端是线程安全的，
        可在 ThreadPoolExecutor 的多个工作线程间共享。
        """
        return httpx.Client(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            proxy=PROXY_URL or None,
            verify=False,
            headers={"User-Agent": "jira-git-gui/1.0"},
        )

    @staticmethod
    def _request_with(client: "httpx.Client", url: str,
                      headers: Optional[dict] = None,
                      retries: int = 5,
                      watchdog: Optional["NetworkWatchdog"] = None) -> httpx.Response:
        """用给定（共享）客户端发带重试的 GET，重试语义与 ``http_get`` 一致。

        同样经全局令牌桶限流，并对 429/503 做 ``Retry-After`` 退避。
        可选 watchdog 用于网络连续性监控（见 ``http_get``）。
        """
        last = None
        for attempt in range(retries):
            if watchdog and watchdog.should_abort():
                raise httpx.TransportError(
                    f"网络已中断，自动停止请求（连续失败 {watchdog.failure_count} 次）")
            try:
                throttle.acquire()
                r = client.get(url, headers=headers or {})
                if _should_backoff(r):
                    _backoff_for(r, attempt)
                    last = httpx.HTTPStatusError(
                        f"服务器限流/暂不可用：{r.status_code}",
                        request=r.request, response=r)
                    continue
                if watchdog:
                    watchdog.notify_success()
                return r
            except (httpx.TransportError, httpx.HTTPError) as e:
                last = e
                if watchdog:
                    watchdog.notify_failure(str(e))
                time.sleep(0.6 * (attempt + 1))
        raise last if last else httpx.TransportError("unknown httpx error")

    # ---------------------------------------------------------- Cookie 模式辅助
    def cookie_headers(self) -> dict:
        return {"Cookie": self.config.cookie} if self.config.cookie else {}

    @staticmethod
    def encode_pat(pat: str) -> str:
        # git/HTTP 基础认证里 '/' 会被误解析，统一编码为 %2F
        return pat.replace("/", "%2F")

    @staticmethod
    def b64_prefix_account(pat: str) -> Optional[str]:
        """从 PAT 前缀解出可能的账号 ID（形如 base64('123456789012:s')）。"""
        head = pat.split("/", 1)[0]
        try:
            dec = base64.b64decode(head + "==").decode("utf-8", "ignore")
            if ":" in dec:
                return dec.split(":", 1)[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _pat_secret(pat: str) -> Optional[str]:
        """若 PAT 为 base64('account:secret') 形态，返回其内嵌密钥。

        部分私有 Jira/Git 部署的 PAT 实为「账号:密钥」的 base64 编码，
        真实 HTTP Basic 密码是这段密钥而非完整 token。无法解析时返回 None。
        """
        try:
            dec = base64.b64decode(pat + "=" * (-len(pat) % 4)).decode("utf-8", "ignore")
            if ":" in dec:
                return dec.split(":", 1)[1]
        except Exception:
            pass
        return None

    # ----------------------------------------------------------------- 连接
    def connect(self) -> dict:
        """探测连通性，返回状态字典。"""
        result = {
            "cookieOk": False,
            "patProvided": bool(self.config.pat),
            "repoDefaults": None,
            "note": "",
            "patTest": None,
        }
        if self.config.cookie:
            try:
                if self.repo_id:
                    r = self._fetch_browse(self.repo_id, self.branch, "")
                    if r.status_code == 200 and "login" not in str(r.url):
                        result["cookieOk"] = True
                        info = self._parse_repo_info(r.text)
                        if info:
                            result["repoDefaults"] = info
                            # Cookie 可用时尝试自动补全仓库名（供 PAT 克隆）
                            if info.get("displayName") and not self.repo_name:
                                self.repo_name = info["displayName"]
                else:
                    r = self.http_get(
                        f"{self.config.jira_url.rstrip('/')}/secure/Dashboard.jspa",
                        headers=self.cookie_headers(),
                    )
                    if (r.status_code == 200 and "login" not in str(r.url)
                            and "dead link" not in r.text.lower()):
                        result["cookieOk"] = True
            except Exception as ex:
                result["note"] = f"cookie 探测异常：{ex}"
        if self.config.pat and self.repo_id and self.repo_name:
            # 轻量连通测试：用 git ls-remote 秒级验证，不再触发完整 clone（旧实现会卡 300s）
            ok, msg = self._pat_test_quick(self.config.pat, self.config.username)
            result["patTest"] = {"ok": ok, "msg": msg}
        return result

    # ----------------------------------------------------- 仓库发现（Cookie）
    def discover_repos(self) -> List[RepoInfo]:
        """发现【全部】仓库（Cookie 模式），翻页遍历、合并且记录发现数。

        数据源：
        1. ``AllRepositories`` HTML 页面（单页解析，可能受页面分页限制，但能从链接里拿到
           ``branchName`` 默认分支）——作为**信息补全**来源。
        2. git 插件 REST 接口，按多种分页参数约定**翻页遍历**直到取尽——
           作为**权威全量**来源，不再受单页 HTML 渲染数量限制。

        每次发现都会把两个接口的原始响应完整写入 ``logs/discover_raw_<时间戳>.txt``，
        并在主日志逐个端点打印「状态码 / 内容类型 / 疑似登录页」，便于排查
        「为什么只返回 N 个」类问题（REST 404 / 返回登录页 / JSON 结构不匹配等）。
        """
        if not self.config.cookie:
            return []
        # 原始响应诊断：写入独立文件，避免污染主日志
        ts = time.strftime("%Y%m%d_%H%M%S")
        raw_path = Path("logs") / f"discover_raw_{ts}.txt"
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_fp = raw_path.open("w", encoding="utf-8")
        except Exception:
            raw_fp = None
        try:
            html = self._discover_repos_html(raw_fp)
            if self._rest_unavailable:
                # 会话内已确认 REST 端点不可用（多为 404），跳过这一轮 9 次白打，保护服务器
                logger.info("[发现-REST] 已缓存「REST 端点不可用」，本次跳过 REST 探测（"
                            "如需强制重试，请重新连接或重启应用）。")
                rest: Dict[str, RepoInfo] = {}
            else:
                rest = self._discover_repos_rest(raw_fp)
        finally:
            if raw_fp:
                raw_fp.close()
        if raw_path.exists():
            logger.info("仓库发现原始接口响应已写入：%s（含 HTML 与各 REST 端点的状态码/响应体）",
                        raw_path)
        merged: Dict[str, RepoInfo] = {}
        if rest:
            # REST 为权威全量来源：以 REST 为主。HTML（AllRepositories 页）在 6.x 是 SPA 空壳，
            # 解析出的锚点可能是噪声，因此仅用于【补全】REST 已有仓库的元信息（默认分支等），
            # 不引入 HTML 独有项，避免污染权威列表。
            for rid, ri in rest.items():
                merged[rid] = ri
            for rid, ri in html.items():
                cur = merged.get(rid)
                if cur is None:
                    continue
                if not cur.default_branch and ri.default_branch:
                    cur.default_branch = ri.default_branch
                if not cur.display_name and ri.display_name:
                    cur.display_name = ri.display_name
                if not cur.clone_url and ri.clone_url:
                    cur.clone_url = ri.clone_url
        else:
            # REST 完全不可用（全 404 等）→ 退化到 HTML 解析结果（可能为空的兜底）
            for rid, ri in html.items():
                merged[rid] = ri
        if not merged:
            logger.warning("仓库发现：0 个。可能会话已过期（无仓库可见），或该账号无可见仓库，"
                           "或 REST/HTML 接口均不可用（详见原始响应文件）。")
        else:
            logger.info("仓库发现完成：HTML 页面解析 %d 个，REST 全量遍历 %d 个，"
                        "合并去重后共 %d 个。", len(html), len(rest), len(merged))
        return sorted(merged.values(), key=lambda x: x.display_name.lower())

    def _discover_repos_html(self, raw_fp=None) -> Dict[str, RepoInfo]:
        """翻页遍历 AllRepositories HTML 页面，返回 repoId -> RepoInfo（含 branchName）。

        Jira GIJRepositoryBrowser-AllRepositories.jspa 支持标准分页参数
        ``pageSize``（每页条数）+ ``pageIndex``（0-based 页码），页面底部会显示
        ``Showing 1 - 100 repositories out of 385`` 形式的总数提示。
        本方法逐页请求、解析仓库锚点、合并去重，直到取尽全部页面。
        """
        out: Dict[str, RepoInfo] = {}
        base_url = self.config.jira_url.rstrip("/") + ALL_REPOS_PAGE
        try:
            for page_idx in range(HTML_MAX_PAGES):
                url = f"{base_url}?pageSize={HTML_PAGE_SIZE}&pageIndex={page_idx}"
                r = self.http_get(url, headers=self.cookie_headers())
                tag = f"HTML AllRepositories [page {page_idx}]"
                self._dump_raw(raw_fp, tag, url, r)
                if r.status_code != 200 or "login" in str(r.url):
                    logger.warning("[发现-HTML] page=%d 状态码=%s 或跳转登录页（%s），停止翻页",
                                   page_idx, r.status_code, r.url)
                    break
                page_repos = self._parse_repo_list(r.text)
                # 本页无新增仓库 → 已到末页（服务端返回空页或重复页）
                prev_count = len(out)
                for ri in page_repos:
                    out[ri.repo_id] = ri
                new_count = len(out) - prev_count
                logger.info("[发现-HTML] page=%d 状态=%s，本页 %d 个（新增 %d），累计 %d",
                            page_idx, r.status_code, len(page_repos), new_count, len(out))
                # 停止条件：本页解析出 0 个仓库锚点
                if not page_repos:
                    logger.info("[发现-HTML] page=%d 为空页，翻页结束", page_idx)
                    break
                # 尝试从页面文本提取 "out of N" 总数，用于提前终止
                total_hint = self._extract_total_repos(r.text)
                if total_hint is not None and len(out) >= total_hint:
                    logger.info("[发现-HTML] 已累计 %d 个（>= 页面声明总数 %d），翻页结束",
                                len(out), total_hint)
                    break
                # 本页不足一页大小 → 必为末页
                if len(page_repos) < HTML_PAGE_SIZE:
                    logger.info("[发现-HTML] page=%d 仅 %d 个 < pageSize=%d，末页",
                                page_idx, len(page_repos), HTML_PAGE_SIZE)
                    break
        except Exception as e:
            logger.warning("[发现-HTML] 翻页异常：%s", e)
        return out

    @staticmethod
    def _extract_total_repos(html: str) -> Optional[int]:
        """从「Showing 1 - 100 repositories out of 385」提取仓库总数。

        兼容中英文界面及不同插件版本的措辞差异。
        """
        m = re.search(r'out\s+of\s+(\d+)', html, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def _discover_repos_rest(self, raw_fp=None) -> Dict[str, RepoInfo]:
        """翻页遍历 git 插件 REST 仓库列表，返回 repoId -> RepoInfo（权威全量）。

        数据源要点（经实测目标 Jira 实例的 Git Integration for Jira 6.x）：
          - 真实可用的用户级全量端点是 ``/rest/gitplugin/1.0/repository/all``
            （**不是** 复数的 ``/repositories``，也不是管理员的 ``/sources/repositories``）。
          - 该端点返回信封 ``{"success":true,"total":385,"offset":0,"count":100,
            "repositories":[...]}``，用 **offset/limit** 分页，且 limit 上限 100
            （limit>100 直返 400）；**startAt/maxResults 约定对其无效**（被忽略）。
          - 同一实例上复数的 ``/repositories`` 等旧路径一律 404，故仅作兜底候选。

        为兼容其它版本/部署，依次尝试 ``REST_ENDPOINTS`` 中的多个端点，并对每个端点
        依次尝试 offset/limit 与 startAt/maxResults 两种分页约定（哪种先拿到数据用哪种）。
        全程受全局限流器节流，不会打崩服务器；每一步都写入原始诊断文件。
        """
        out: Dict[str, RepoInfo] = {}
        saw_dead = False  # 是否探测到「端点确实不可用」（401/403/404/405 或请求异常）
        base = self.config.jira_url.rstrip("/")
        for ep in REST_ENDPOINTS:
            if out:
                break
            ep_out: Dict[str, RepoInfo] = {}
            # 分页约定：优先 offset/limit（6.x 实测），再试 startAt/maxResults（旧版兜底）
            conventions = [
                ("offset/limit", lambda o, n: f"?offset={o}&limit={n}"),
                ("startAt/maxResults", lambda s, n: f"?startAt={s}&maxResults={n}"),
            ]
            for cname, build in conventions:
                if ep_out:
                    break
                start = 0
                for _ in range(REST_MAX_PAGES):
                    paged = base + ep + build(start, REST_PAGE_SIZE)
                    try:
                        r = self.http_get(paged, headers=self.cookie_headers())
                        self._dump_raw(raw_fp, f"REST {ep} [{cname}] {start}", paged, r)
                        if r.status_code != 200 or self._looks_like_login(r):
                            if r.status_code in _REST_DEAD_STATUS:
                                saw_dead = True
                            logger.warning("[发现-REST] %s [%s] %s：状态=%s%s",
                                           ep, cname, start, r.status_code,
                                           "（疑似登录页）" if self._looks_like_login(r) else "")
                            break
                        items, total = self._normalize_rest_envelope(r)
                        if not items:
                            break
                        prev = len(ep_out)
                        for it in items:
                            ri = self._parse_rest_repo_item(it)
                            if ri and ri.repo_id:
                                ep_out.setdefault(ri.repo_id, ri)
                        got = len(items)
                        logger.info("[发现-REST] %s [%s] 第 %d 页：本页 %d 个（新增 %d），"
                                    "累计 %d / total=%s",
                                    ep, cname, start // REST_PAGE_SIZE, got,
                                    len(ep_out) - prev, len(ep_out), total)
                        # 停止条件（任意一条命中即末页，避免死循环）：
                        if total is not None and len(ep_out) >= total:
                            break                       # 已取到服务端声明的总数
                        if got < REST_PAGE_SIZE:
                            break                       # 本页不足一页 → 末页
                        if len(ep_out) == prev:
                            break                       # 本页无新增 → 服务端忽略分页
                        start += REST_PAGE_SIZE
                    except Exception as e:
                        saw_dead = True
                        logger.warning("[发现-REST] %s [%s] %s 异常：%s",
                                       ep, cname, start, e)
                        break
            if ep_out:
                out.update(ep_out)
        # 全部端点都确认不可用（且拿不到任何仓库）→ 本次会话缓存该结论，后续发现跳过 REST
        if not out and saw_dead:
            self._rest_unavailable = True
            logger.info("[发现-REST] 所有 REST 端点均不可用（多为 404），已缓存该结论；"
                        "后续「发现仓库」将跳过 REST 探测以节省请求。")
        return out

    # ---- 原始响应诊断辅助 ----
    @staticmethod
    def _dump_raw(fp, tag: str, url: str, resp, max_body: int = 200000) -> None:
        """把单次 HTTP 响应的原始信息追加写入诊断文件（不影响主流程）。"""
        if fp is None:
            return
        try:
            try:
                body = resp.text
            except Exception:
                body = "<unreadable body>"
            try:
                ct = resp.headers.get("content-type", "")
            except Exception:
                ct = ""
            fp.write(f"\n===== {tag} =====\n")
            fp.write(f"URL: {url}\n")
            fp.write(f"STATUS: {getattr(resp, 'status_code', '?')}\n")
            fp.write(f"FINAL_URL: {getattr(resp, 'url', '?')}\n")
            fp.write(f"CONTENT-TYPE: {ct}\n")
            fp.write(f"BODY-LEN: {len(body)}\n")
            fp.write("----- BODY (truncated) -----\n")
            fp.write(body[:max_body])
            fp.write("\n")
            fp.flush()
        except Exception:
            pass

    @staticmethod
    def _looks_like_login(resp) -> bool:
        """粗略判断响应是否为登录页（会话失效时 Jira 会把 REST 重定向到登录页）。"""
        try:
            if "login" in str(getattr(resp, "url", "")).lower():
                return True
            ct = (resp.headers.get("content-type") or "").lower()
            if "html" in ct:
                txt = (resp.text or "")[:2000].lower()
                if "login" in txt or "j_security_check" in txt or "os_password" in txt:
                    return True
        except Exception:
            pass
        return False

    def _safe_json_list(self, resp) -> list:
        """把响应体安全解析为仓库对象列表（解析失败返回空列表，不抛异常）。"""
        try:
            data = resp.json()
        except Exception:
            return []
        return self._normalize_rest_list(data)

    @staticmethod
    def _normalize_rest_list(data) -> list:
        """把 REST 响应归一为仓库对象列表（兼容数组 / 包装对象 / 单对象）。"""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("repositories", "values", "repos", "repoList"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
            if "id" in data or "repoId" in data or data.get("displayName"):
                return [data]
        return []

    @staticmethod
    def _normalize_rest_envelope(resp) -> tuple:
        """把 REST 响应归一为 ``(items, total)``。

        ``items`` 为仓库对象列表；``total`` 为服务端声明的仓库总数（无则 None）。
        兼容：
          - 6.x 信封：``{"success":true,"total":385,"offset":0,"count":100,
                          "repositories":[...]}``
          - 旧版包装：``{"repositories":[...], "total":N}``
          - 裸数组：``[{...}, ...]``
          - 单对象：``{"id":..,"displayName":..}``
        """
        try:
            data = resp.json()
        except Exception:
            return [], None
        items: list = []
        total = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("repositories", "values", "repos", "repoList"):
                v = data.get(key)
                if isinstance(v, list):
                    items = v
                    break
            if not items and ("id" in data or "repoId" in data or data.get("displayName")):
                items = [data]
            tv = data.get("total")
            if isinstance(tv, int):
                total = tv
        return items, total

    @staticmethod
    def _extract_clone_url(it: dict) -> str:
        """从 REST 仓库对象里尽量提取可用的 clone URL。

        6.x 的 repository/all 不直接给 cloneUrl 字段，而是把真实地址藏在
        ``gkRepoUrl`` / ``glRepoUrl``（形如
        ``gitkraken://...?url=https%3A%2F%2Fcode.example.io%2Fgroup%2Frepo.git``）
        的 ``url`` 查询参数里。旧版则可能直接给 ``cloneUrl`` / ``url``。
        逐一尝试，返回第一个可用的 http(s) 地址。
        """
        for key in ("gkRepoUrl", "glRepoUrl", "cloneUrl", "url", "remoteUrl", "sshUrl"):
            v = it.get(key)
            if not isinstance(v, str) or not v:
                continue
            # gitkraken/gitlens 类 scheme：从 ?url=ENCODED 取出真实地址
            m = re.search(r"[?&]url=([^&]+)", v)
            if m:
                decoded = urllib.parse.unquote(m.group(1))
                if decoded.startswith("http"):
                    return decoded
            if v.startswith("http"):
                return v
        return ""

    @staticmethod
    def _parse_rest_repo_item(it) -> Optional[RepoInfo]:
        """从单个 REST 仓库对象构造 RepoInfo（无 id 则忽略）。

        注意：``id`` 可能是合法的数字 0，必须用 ``is None`` 判断而非真值，
        否则 ``0 or ...`` 会把 id=0 当成缺失而丢弃。
        """
        if not isinstance(it, dict):
            return None
        rid = it.get("id")
        if rid is None:
            rid = it.get("repoId")
        if rid is None:
            return None
        rid = str(rid)
        if not rid:
            return None
        return RepoInfo(
            repo_id=rid,
            display_name=it.get("displayName") or it.get("name") or it.get("repoName") or "",
            clone_url=JiraGitClient._extract_clone_url(it),
            default_branch=it.get("defaultBranch") or it.get("branchName") or "",
        )

    @staticmethod
    def _strip_tags(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s or "").strip()

    def _parse_repo_list(self, html: str) -> List[RepoInfo]:
        """从 AllRepositories 页面解析仓库列表。

        返回以 repoId 去重后的 RepoInfo；同名则保留最长名称；并尝试从
        repoId 链接的 branchName 参数提取默认分支。
        """
        repos: dict = {}  # repoId -> RepoInfo（保留最长名字）
        for m in _REPO_ANCHOR_RE.finditer(html):
            href, rid, raw_text = m.group(1), m.group(2), m.group(3)
            name = re.sub(r"\s+", " ", self._strip_tags(raw_text)).strip()
            if not name or name.lower() in _NOISE_NAMES:
                continue
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            branch = qs.get("branchName", [""])[0]
            existing = repos.get(rid)
            if existing is None or len(name) > len(existing.display_name):
                repos[rid] = RepoInfo(
                    repo_id=rid,
                    display_name=name,
                    default_branch=branch,
                )
        return list(repos.values())

    # ------------------------------------------------------- 文件树（单层）
    def list_level(self, path: str = "") -> List[TreeEntry]:
        """返回 path 目录的【直接子项】（单层，懒加载）。自动路由：
        PAT 模式且本地已克隆 -> 读本地；否则走 Cookie 模式。"""
        if self.config.mode == "pat" and (REPOS_DIR / str(self.repo_id)).exists():
            root = REPOS_DIR / str(self.repo_id)
            return self._list_level_local(root, path)
        if not self.config.cookie:
            raise UserError("Cookie 模式未配置会话")
        if not self.repo_id:
            raise UserError("缺少 repoId，请先在连接或仓库面板中指定")
        return self._list_level_cookie(self.repo_id, self.branch, path)

    # ----------------------------------------------------- 分支自动探测
    # 服务器的 GIJBrowseGit 在 branchName 为空时会被踢到登录页；且各仓库默认分支
    # 不一定是 master。因此浏览/读文件前需自动确定一个“可用分支”。
    _BRANCH_CANDIDATES = ["master", "main", "develop", "release", "trunk", "test", "prod"]

    @staticmethod
    def _is_login_page(r) -> bool:
        """browse 响应是否被踢到登录页（无权限 / 缺省分支触发重定向）。"""
        return "login" in str(getattr(r, "url", "")).lower()

    def _browse_has_tree(self, repo_id: str, branch: str) -> bool:
        """该 branch 的 browse 是否返回了可用的文件树（ns.data）。"""
        try:
            r = self._fetch_browse(repo_id, branch, "")
            if self._is_login_page(r):
                return False
            return bool(re.search(r'ns\.data\s*=', r.text))
        except Exception:
            return False

    def _resolve_branch(self, repo_id: str, branch: str) -> str:
        """返回可用于浏览的分支；空串表示都失败。

        优先用给定 branch；若其 browse 无树（空 branch 会被踢登录、或分支不存在），
        则按候选列表试探，取第一个能返回 ns.data 的分支。结果按 repo_id 缓存，
        避免每次列目录/读文件都重复探测 7 个候选分支（每次都发 HTTP）。
        """
        cache_key = str(repo_id)
        if cache_key in self._branch_cache:
            return self._branch_cache[cache_key]
        resolved = branch
        if branch and self._browse_has_tree(repo_id, branch):
            resolved = branch
        else:
            for cand in self._BRANCH_CANDIDATES:
                if cand == branch:
                    continue
                if self._browse_has_tree(repo_id, cand):
                    resolved = cand
                    break
        self._branch_cache[cache_key] = resolved
        return resolved  # 回退给上层报错，而不是静默空树

    def _resolve_head(self, repo_id: str, branch: str) -> Optional[str]:
        """返回当前分支的 HEAD commit（带缓存）。

        ``_cookie_file_content`` 取文件需要 HEAD commit 作引用；每次文件读取都
        重新 ``_fetch_browse`` 解析 ``ns.repoInfo`` 开销不小，故按 (repo_id, branch)
        缓存，重复读取文件 / 下载批量内只解析一次。
        """
        key = (str(repo_id), branch)
        if key in self._head_cache:
            return self._head_cache[key]
        r = self._fetch_browse(repo_id, branch, "")
        info = self._parse_repo_info(r.text)
        head = info.get("headCommit")
        if head:
            self._head_cache[key] = head
        return head


    def _list_level_cookie(self, repo_id: str, branch: str, path: str = "") -> List[TreeEntry]:
        branch = self._resolve_branch(repo_id, branch)
        self.branch = branch  # 回写，供 get_file / download 复用，避免重复探测
        return self._list_dir(repo_id, branch, path)

    def _list_dir(self, repo_id: str, branch: str, path: str = "") -> List[TreeEntry]:
        """单层目录列取（不解析分支，供递归下载内部复用）。"""
        r = self._fetch_browse(repo_id, branch, path)
        raw = self._parse_tree_files(r.text)
        out: List[TreeEntry] = []
        for e in raw:
            p = e["path"]
            is_dir = e["is_dir"]
            out.append(TreeEntry(
                name=p.split("/")[-1] or "(root)",
                path=p,
                type="dir" if is_dir else "file",
                size=e["size"],
                has_children=bool(is_dir),
            ))
        out.sort(key=lambda x: (x.type != "dir", x.name.lower()))
        return out

    def _list_level_local(self, root: Path, path: str = "") -> List[TreeEntry]:
        full = root / path
        out: List[TreeEntry] = []
        try:
            items = sorted(full.iterdir(),
                           key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception:
            return out
        for it in items:
            if it.name == ".git":
                continue
            rel = (path + "/" + it.name).lstrip("/")
            if it.is_dir():
                out.append(TreeEntry(it.name, rel, "dir",
                                     None, True))
            else:
                out.append(TreeEntry(it.name, rel, "file",
                                     it.stat().st_size, False))
        return out

    # ----------------------------------------------------------- 提交记录
    def get_commits(self, issue_key: Optional[str] = None,
                    repo_id: Optional[str] = None,
                    branch: Optional[str] = None,
                    show_files: bool = True,
                    limit: int = 50) -> List[Commit]:
        """拉取提交记录（Jira Git 插件的 commits 以 Jira issue 关联组织）。

        - 提供 ``issue_key``：走官方 REST ``/rest/gitplugin/1.0/issues/{key}/commits``，
          最可靠；``show_files=True`` 时每条 commit 还带改动文件清单
          （path / changeType / linesAdded / linesRemoved）。
        - 仅提供 ``repo_id``（best-effort）：尝试 ``/rest/gitplugin/1.0/commits?repoId=``，
          该端点并非所有私有部署都开放；不支持时给出友好提示，引导改用 issue 查询。
        - 两者都未提供：抛出说明。

        返回 ``List[Commit]``，按接口返回顺序（通常最新在前）。
        """
        cookie = self.cookie_headers()
        if not cookie:
            raise UserError("查看提交记录需要 Cookie 模式（会话）。请在连接设置中填入会话 Cookie。")
        base = self.config.jira_url.rstrip("/")

        if issue_key:
            url = (f"{base}/rest/gitplugin/1.0/issues/"
                   f"{urllib.parse.quote(issue_key.strip())}/commits")
            if show_files:
                url += "?showFiles=true"
            r = self.http_get(url, headers=cookie)
            if self._is_login_page(r) or r.status_code != 200:
                raise UserError(
                    "提交查询失败：会话可能已过期，或对该 issue 无读取权限。"
                    "请重新登录后在连接设置中更新 Cookie。")
            try:
                data = r.json()
            except Exception:
                raise UserError("提交查询返回非 JSON（可能会话过期或接口变更）。")
            commits = (data.get("commits")
                       or (data.get("data") or {}).get("commits") or [])
            return [self._parse_commit(c) for c in commits[:limit]]

        # best-effort：按仓库列提交（并非所有部署都支持）
        rid = repo_id or self.repo_id
        if rid:
            url = f"{base}/rest/gitplugin/1.0/commits?repoId={urllib.parse.quote(str(rid))}"
            if branch:
                url += f"&branchName={urllib.parse.quote(branch)}"
            if show_files:
                url += "&showFiles=true"
            r = self.http_get(url, headers=cookie)
            if r.status_code == 200 and not self._is_login_page(r):
                try:
                    data = r.json()
                    commits = (data.get("commits")
                               or (data.get("data") or {}).get("commits") or [])
                    if commits:
                        return [self._parse_commit(c) for c in commits[:limit]]
                except Exception:
                    pass
            raise UserError(
                "该 Jira 实例不提供『按仓库列全量提交』接口"
                "（Jira Git 插件的提交以 issue 关联组织）。\n"
                "请在提交记录面板输入 Jira issue 单号（如 TST-234）后查询，"
                "即可看到该 issue 关联的全部提交与改动文件。")
        raise UserError(
            "请先在仓库面板选择/指定仓库，或在提交记录面板输入 Jira issue 单号（如 TST-234）。")

    # --------------------------------------------------- 本地 Git 全量提交历史
    def get_local_commits(self, repo_id: str, branch: str = "",
                         limit: int = 50) -> List[Commit]:
        """本地 Git 模式：对已克隆到本地的仓库直接跑 ``git log``，拿到完整提交历史。

        不走 Jira REST（Jira Git 插件的 commits 以 issue 关联组织，无“按仓库全量
        git log”公开接口）；前提是仓库已通过 PAT 模式克隆到 ``store/repos/<repoId>/``。
        返回 ``List[Commit]``（含每条改动的 files：path / changeType）。
        """
        local_path = REPOS_DIR / str(repo_id)
        if not local_path.exists():
            raise UserError(
                "本地 Git 模式需要该仓库已克隆到本地（请先用 PAT 模式点「克隆仓库」）。\n"
                f"当前未找到本地克隆：{local_path}")
        cmd = [self._git_bin, "-C", str(local_path), "log",
               "--pretty=format:%x1e%H%x1f%an%x1f%ae%x1f%ad%x1f%s",
               "--date=iso", "-n", str(int(limit)), "--name-status"]
        if branch:
            cmd.append(branch)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as ex:
            raise RuntimeError(f"git log 执行失败：{ex}")
        if res.returncode != 0:
            raise RuntimeError("git log 失败：" + (res.stderr.strip() or "未知错误"))
        commits = self._parse_git_log(res.stdout)
        if not commits:
            raise UserError("该仓库本地 git log 为空（可能是浅克隆 --depth 1，历史被截断）。")
        return commits

    @staticmethod
    def _parse_git_log(out: str) -> List[Commit]:
        """解析 ``git log --name-status`` 输出（commit 以 \\x1e 分隔，字段以 \\x1f 分隔）。

        每个 commit 块：第 1 行为 header 字段(H/an/ae/ad/s)，其后为 name-status 行
        （``M\\tpath`` / ``A\\tpath`` / ``R100\\told\\tnew`` 等）。
        """
        commits: List[Commit] = []
        for block in out.split("\x1e"):
            if not block.strip():
                continue
            lines = block.split("\n")
            header = lines[0].split("\x1f")
            if len(header) < 5:
                continue
            cid, author, _email, date, message = header[:5]
            files: List[CommitFile] = []
            for ln in lines[1:]:
                ln = ln.rstrip("\r")
                if not ln.strip():
                    continue
                parts = ln.split("\t")
                ct = re.sub(r"\d+$", "", parts[0].strip())  # "R100" -> "R"
                if len(parts) < 2:
                    continue
                fpath = parts[-1]  # R 会有 old\tnew，取 new 路径
                files.append(CommitFile(path=fpath, change_type=ct))
            commits.append(Commit(
                commit_id=cid,
                display_id=cid[:8],
                author=author,
                date=date,
                message=message,
                branch="",
                repository_name="",
                files=files,
            ))
        return commits

    @staticmethod
    def _parse_commit(c: dict) -> Commit:
        cid = c.get("commitId") or c.get("id") or ""
        files: List[CommitFile] = []
        for f in (c.get("files") or []):
            files.append(CommitFile(
                path=f.get("path") or "",
                change_type=f.get("changeType") or f.get("type") or "",
                lines_added=f.get("linesAdded") or f.get("lines_added") or 0,
                lines_removed=f.get("linesRemoved") or f.get("lines_removed") or 0,
            ))
        branches = c.get("branches") or []
        branch = c.get("branch") or (branches[0] if branches else "")
        repo = c.get("repository") or {}
        repo_name = repo.get("name") if isinstance(repo, dict) else ""
        return Commit(
            commit_id=cid,
            display_id=cid[:8] if cid else "",
            author=(c.get("author") or "").strip(),
            date=c.get("date") or c.get("authorTimestamp") or "",
            message=(c.get("message") or "").strip(),
            branch=branch,
            repository_name=repo_name,
            files=files,
        )

    # ------------------------------------------------------------- 文件正文
    def get_file(self, path: str) -> tuple:
        """返回 (content, error)。content 为 None 时 error 有值。

        二进制文件（图片/压缩包等）在 Cookie 模式下无法预览，返回提示，
        引导用户用「下载选中」保存到本地查看。
        """
        if self.config.mode == "pat" and (REPOS_DIR / str(self.repo_id)).exists():
            try:
                content = self._local_file_read(REPOS_DIR / str(self.repo_id), path)
                if isinstance(content, (bytes, bytearray)):
                    return None, "二进制文件，请在文件树勾选后用「下载选中」保存到本地查看。"
                return content, None
            except Exception as ex:
                return None, str(ex)
        if not self.config.cookie:
            return None, "Cookie 未配置"
        branch = self._resolve_branch(self.repo_id, self.branch)
        self.branch = branch
        head = self._resolve_head(self.repo_id, branch)
        if not head:
            return None, "无法获取分支 HEAD commit"
        ok, content, note = self._cookie_file_content(self.repo_id, head, path)
        if not ok:
            return None, note
        if isinstance(content, (bytes, bytearray)):
            return None, (f"二进制文件（{len(content)} 字节），"
                          f"请在文件树勾选后用「下载选中」保存到本地查看。")
        return content, None

    def get_file_at_commit(self, repo_id: str, commit_id: str, path: str) -> tuple:
        """查看某次提交中某文件的【历史版本】内容（文本）。

        - 若该仓库已在本地克隆（PAT 模式），用 ``git show <sha>:<path>`` 直接取；
        - 否则走 Cookie 模式，用 commit SHA 作为 ref 调文件接口
          （插件 REST ``/files/{repo}/{sha}/{path}`` 支持任意 commit 引用）。
        返回 (content, error)；二进制文件返回 (None, 提示)。
        """
        local_path = REPOS_DIR / str(repo_id)
        if local_path.exists():
            try:
                res = subprocess.run(
                    [self._git_bin, "-C", str(local_path), "show",
                     f"{commit_id}:{path}"],
                    capture_output=True, timeout=30)
                if res.returncode == 0:
                    data = res.stdout
                    # 与 Cookie 模式一致：二进制按字节返回并给「二进制请下载」提示，
                    # 不再用 errors='replace' 把二进制预览成乱码（与 docx 二进制识别同类）。
                    if self._is_likely_text(data, ""):
                        try:
                            return data.decode("utf-8"), None
                        except UnicodeDecodeError:
                            return data, None
                    return None, (f"二进制文件（{len(data)} 字节），"
                                  f"请在文件树勾选后用「下载选中」保存到本地查看历史版本。")
            except Exception:
                pass
        if not self.config.cookie:
            return None, "未找到本地克隆，且未配置 Cookie，无法查看历史文件。"
        ok, content, note = self._cookie_file_content(repo_id, commit_id, path)
        if not ok:
            return None, note
        if isinstance(content, (bytes, bytearray)):
            return None, (f"二进制文件（{len(content)} 字节），"
                          f"请在文件树勾选后用「下载选中」保存到本地查看历史版本。")
        return content, None

    # ----------------------------------------------------------- git 克隆
    @staticmethod
    def _clone_user_candidates(pat: str, username: str) -> list:
        """为 PAT 克隆构造 username 候选（去重/去空，按优先级排序）。

        Jira Git 插件要求 username 为 PAT 所属账号、PAT 本身作 password。
        PAT 前缀 base64 解码后通常内嵌账号 ID，最权威，放最前；
        其次为用户显式配置的用户名。空候选会被丢弃（避免用空用户名发起无效克隆）。
        """
        cands: list = []
        acct = JiraGitClient.b64_prefix_account(pat)
        for c in (acct, username):
            c = (c or "").strip()
            if c and c not in cands:
                cands.append(c)
        return cands

    def clone_repo(self, repo_id: str, repo_name: str, branch: str,
                   pat: str, username: str,
                   on_log: Optional[Callable[[str], None]] = None) -> tuple:
        """git clone 到本地，返回 (ok, msg, local_path)。"""
        def log(m):
            if on_log:
                on_log(m)

        host = self.host_of(self.config.jira_url)
        local_path = REPOS_DIR / str(repo_id)
        if local_path.exists():
            try:
                subprocess.run([self._git_bin, "-C", str(local_path), "fetch", "--all"],
                               capture_output=True, text=True, timeout=120)
                log("已存在本地克隆，已 fetch 更新")
                return True, "已存在，已 fetch 更新", str(local_path)
            except Exception as ex:
                return True, f"已存在本地克隆（fetch 跳过：{ex}）", str(local_path)

        candidates = self._clone_user_candidates(pat, username)
        if not candidates:
            return False, ("克隆失败：缺少可用的 username（PAT 未内嵌账号且未配置用户名）。"
                           "请在「连接设置」填写用户名后重试。"), None

        # 密码候选：① 完整 PAT（标准用法，优先）；② 若 PAT 为 base64('account:secret')
        # 形态，则该 secret 也可能直接作为密码（部分私有 Git/Jira 部署采用此方案）。
        _enc = lambda p: urllib.parse.quote(p, safe="")
        passwords = [_enc(pat)]
        secret = self._pat_secret(pat)
        if secret:
            passwords.append(_enc(secret))

        last_err = ""
        auth_rejected = False
        for pw in passwords:
            for user in candidates:
                clone_url = (f"https://{user}:{pw}@{host}"
                             f"/git/{repo_id}/{repo_name}.git")
                cmd = [self._git_bin, "-c", "credential.helper=", "clone", "--depth", "1"]
                if branch:
                    cmd += ["-b", branch]
                cmd += [clone_url, str(local_path)]
                log(f"正在克隆（用户 {user}）...")
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if res.returncode == 0:
                        return True, f"克隆成功（用户 {user}）", str(local_path)
                    combined = (res.stderr or "") + "\n" + (res.stdout or "")
                    last_err = combined.strip().splitlines()[-1] if combined.strip() else ""
                    if any(k in last_err for k in (
                        "login.jsp", "permissionViolation", "Authentication failed",
                        "fatal: Authentication", "401",
                        "remote: Invalid username or password",
                    )):
                        auth_rejected = True
                except subprocess.TimeoutExpired:
                    last_err = "克隆超时"
                except Exception as ex:
                    last_err = str(ex)

        # 诊断：是否被踢到登录页 / 权限被拒（凭据送达后被服务器拒绝）
        if auth_rejected:
            return False, self._pat_diag(pat, username), None
        return False, f"克隆失败：{last_err}", None

    # ----------------------------------------------------- PAT 诊断 / 轻量连通测试
    def _pat_diag(self, pat: str, username: str) -> str:
        """构造 PAT 认证被拒时的诊断信息（克隆与快速测试共用）。"""
        acct = self.b64_prefix_account(pat) or username
        secret = self._pat_secret(pat)
        tried = "完整 PAT" + (" + 内嵌密钥" if secret else "")
        host = self.host_of(self.config.jira_url)
        return ("认证被服务器拒绝（凭据无效，或该账号无此仓库克隆权限）。\n"
                f"（已分别用「{tried}」两种方式尝试验证，均被拒绝。）\n"
                "请确认：\n"
                "  ① PAT 有效且未过期 / 未吊销；\n"
                f"  ② 该 PAT 所属账号（{acct}）对仓库 {self.repo_id}/{self.repo_name} 有浏览/克隆权限；\n"
                "  ③ 必要时在 Jira 重新生成 PAT（克隆范围）。\n"
                "可先用终端手动验证，以排除是 GUI 问题：\n"
                f"  git ls-remote https://{acct}:<PAT>@{host}/git/{self.repo_id}/{self.repo_name}.git")

    def _pat_test_quick(self, pat: str, username: str) -> tuple:
        """用 ``git ls-remote --heads`` 秒级验证 PAT 能否访问指定仓库（不克隆、不下载）。

        相比完整 ``git clone``（最长 300s 且会拉取大量对象），速度快且鉴权失败立即返回诊断。
        返回 (ok, msg)。
        """
        host = self.host_of(self.config.jira_url)
        users = self._clone_user_candidates(pat, username)
        if not users:
            return False, ("缺少可用的 username（PAT 未内嵌账号且未配置用户名）。"
                           "请在「连接设置」填写用户名后重试。")
        passwords = [urllib.parse.quote(pat, safe="")]
        secret = self._pat_secret(pat)
        if secret:
            passwords.append(urllib.parse.quote(secret, safe=""))
        ident = f"{self.repo_id}/{self.repo_name}"
        for pw in passwords:
            for user in users:
                url = f"https://{user}:{pw}@{host}/git/{ident}.git"
                try:
                    res = subprocess.run(
                        [self._git_bin, "ls-remote", "--heads", url],
                        capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    return False, "PAT 探测超时（ls-remote 无响应）"
                except Exception as ex:
                    return False, f"PAT 探测异常：{ex}"
                combined = (res.stderr or "") + "\n" + (res.stdout or "")
                if res.returncode == 0:
                    n = len([l for l in res.stdout.splitlines() if l.strip()])
                    return True, f"PAT 认证通过（用户 {user}，远端分支数 {n}）"
                if any(k in combined for k in (
                        "permissionViolation", "Authentication failed", "401",
                        "Invalid username or password", "fatal: Authentication",
                        "fatal: unable to access")):
                    return False, self._pat_diag(pat, username)
        return False, "PAT 认证失败（ls-remote 未返回有效结果，请检查仓库 ID / 名称）。"

    # ------------------------------------------------------ 断点续传清单
    _MANIFEST_NAME = ".jira_git_manifest.json"

    def _manifest_path(self, dest_root) -> Path:
        return Path(dest_root) / self._MANIFEST_NAME

    def _load_manifest(self, dest_root) -> dict:
        """载入断点续传清单：{path: size}。已存在且大小一致的文件可跳过。"""
        import json
        p = self._manifest_path(dest_root)
        try:
            if p.exists():
                return dict(json.loads(p.read_text(encoding="utf-8")).get("files", {}))
        except Exception:
            pass
        return {}

    def _save_manifest(self, dest_root, manifest: dict) -> None:
        """即时落盘断点续传清单，确保中断后再次运行能跳过已完成文件。"""
        import json
        p = self._manifest_path(dest_root)
        try:
            p.write_text(json.dumps({"files": manifest}, ensure_ascii=False),
                         encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------- Cookie 批量下载
    def download(self, paths: List[str],
                 on_log: Optional[Callable[[str], None]] = None,
                 on_progress: Optional[Callable[[int, int, str], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 max_workers: int = DEFAULT_DOWNLOAD_WORKERS) -> tuple:
        """Cookie 模式：批量下载所选文件到 downloads/<repoId>/ 保持目录结构。

        支持断点续传（已存在且大小一致的同路径文件自动跳过）、进度回调与取消、
        有界并发下载（max_workers）。返回 (ok_paths, fail_list, dest, skipped)。
        """
        def log(m):
            if on_log:
                on_log(m)

        if not self.config.cookie:
            return [], [{"path": p, "reason": "Cookie 未配置"} for p in paths], None, 0
        if not self.repo_id:
            return [], [{"path": p, "reason": "未指定仓库"} for p in paths], None, 0

        dest_root = DOWNLOAD_DIR / str(self.repo_id)
        dest_root.mkdir(parents=True, exist_ok=True)
        manifest = self._load_manifest(dest_root)
        _, fail_list, dest, skipped, ok_paths = self._download_files(
            self.repo_id, self.branch, dest_root, paths,
            on_progress, should_cancel, manifest, max_workers)
        self._save_manifest(dest_root, manifest)
        return ok_paths, fail_list, dest, skipped

    # --------------------------------------------------- Cookie 递归下载整库
    def _walk_all_files(self, repo_id: str, branch: str,
                        should_cancel: Optional[Callable[[], bool]] = None) -> List[str]:
        """DFS 枚举整棵文件树，返回全部【文件】相对路径（不含目录）。"""
        branch = self._resolve_branch(repo_id, branch)
        self.branch = branch
        out: List[str] = []
        stack = [""]
        while stack:
            if should_cancel and should_cancel():
                break
            cur = stack.pop()
            try:
                entries = self._list_dir(repo_id, branch, cur)
            except Exception:
                # 个别目录列取失败不阻断整体，交由后续下载阶段记录
                continue
            for e in entries:
                if e.type == "dir":
                    stack.append(e.path)
                else:
                    out.append(e.path)
        return out

    def _download_files(self, repo_id: str, branch: str, dest_root,
                        file_paths: List[str],
                        on_progress: Optional[Callable[[int, int, str], None]] = None,
                        should_cancel: Optional[Callable[[], bool]] = None,
                        manifest: Optional[dict] = None,
                        max_workers: int = DEFAULT_DOWNLOAD_WORKERS) -> tuple:
        """执行一批文件的下载（供 download / download_repo 复用）。

        带断点续传（manifest 中已存在且大小一致的文件跳过）、进度回调、可取消，
        并用有界线程池并行抓取+落盘以加速整库下载。
        返回 (ok_count, fail_list, dest, skipped, ok_paths)。
        """
        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        total = len(file_paths)
        if total == 0:
            return 0, [], str(dest_root), 0, []

        branch = self._resolve_branch(repo_id, branch)
        self.branch = branch
        head = self._resolve_head(repo_id, branch)
        if not head:
            return 0, [{"path": "(root)", "reason": "无法获取分支 HEAD commit"}], \
                   str(dest_root), 0, []

        ok_count = 0
        skipped = 0
        fail_list = []
        ok_paths = []
        done = 0

        # 断点续传预筛：已存在且大小一致的同路径文件直接跳过（不占网络）
        todo: List[str] = []
        for path in file_paths:
            if manifest is not None and path in manifest:
                target = dest_root / path
                if target.exists():
                    try:
                        if manifest[path] and target.stat().st_size == manifest[path]:
                            skipped += 1
                            done += 1
                            ok_paths.append(path)
                            if on_progress:
                                on_progress(done, total, path)
                            continue
                    except OSError:
                        pass
            todo.append(path)

        if not todo:
            return 0, [], str(dest_root), skipped, ok_paths

        def _fetch_one(path):
            """在子线程中抓取单文件并落盘，返回 (path, 'ok'|'fail', size|reason)。"""
            if should_cancel and should_cancel():
                return None  # 取消标记
            ok, content, note = self._cookie_file_content(
                repo_id, head, path, client=client)
            if not ok or content is None:
                return (path, "fail", note)
            target = dest_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if isinstance(content, (bytes, bytearray)):
                    target.write_bytes(bytes(content))
                else:
                    target.write_text(content, encoding="utf-8")
                sz = target.stat().st_size
            except OSError as e:
                return (path, "fail", f"写入失败：{e}")
            return (path, "ok", sz)

        workers = max(1, int(max_workers))
        client = self._make_client()
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_fetch_one, p) for p in todo]
                try:
                    for fut in as_completed(futures):
                        # 取消：丢弃尚未开始的子任务；但已完成的仍计入，保证计数与磁盘一致
                        if should_cancel and should_cancel():
                            try:
                                ex.shutdown(wait=False, cancel_futures=True)
                            except Exception:
                                pass
                        try:
                            res = fut.result()
                        except CancelledError:
                            continue  # 被取消的待处理任务，无贡献
                        if res is None:  # 任务在取消后才启动，主动返回 None
                            continue
                        path, status, payload = res
                        if status == "ok":
                            ok_count += 1
                            ok_paths.append(path)
                            if manifest is not None:
                                manifest[path] = payload  # 即时记入，中断可续
                        else:
                            fail_list.append({"path": path, "reason": payload})
                        done += 1
                        if on_progress:
                            on_progress(done, total, path)
                finally:
                    # 确保线程池一定关闭（取消或未取消都执行）
                    ex.shutdown(wait=False, cancel_futures=True)
        finally:
            try:
                client.close()
            except Exception:
                pass

        if manifest is not None:
            self._save_manifest(dest_root, manifest)
        return ok_count, fail_list, str(dest_root), skipped, ok_paths

    def download_repo(self, repo_id: str, branch: str, dest_root: Optional[str] = None,
                      on_log: Optional[Callable[[str], None]] = None,
                      on_progress: Optional[Callable[[int, int, str], None]] = None,
                      should_cancel: Optional[Callable[[], bool]] = None,
                      max_workers: int = DEFAULT_DOWNLOAD_WORKERS) -> tuple:
        """Cookie 模式：递归遍历整棵文件树并下载所有文件，保持目录结构。

        支持断点续传（已下载文件自动跳过）、进度回调与取消。适用于没有 PAT
        克隆权限时，用会话 Cookie 把仓库“整棵”抓回本地；中途中断后再次点击
        同一仓库即可从断点继续。
        返回 (ok_count, fail_list, dest, skipped)。
        """
        def log(m):
            if on_log:
                on_log(m)

        if not self.config.cookie:
            return 0, [{"path": "(root)", "reason": "Cookie 未配置"}], None, 0

        dest_root = Path(dest_root) if dest_root else (DOWNLOAD_DIR / str(repo_id))
        dest_root.mkdir(parents=True, exist_ok=True)

        log("枚举整棵文件树（准备阶段）…")
        file_paths = self._walk_all_files(repo_id, branch, should_cancel)
        if not file_paths:
            return 0, [{"path": "(root)",
                        "reason": "未能枚举到任何文件（分支浏览失败或被拦截）"}], \
                   str(dest_root), 0
        log(f"枚举到 {len(file_paths)} 个文件，开始下载"
            f"（断点续传：已存在的文件将自动跳过）…")

        manifest = self._load_manifest(dest_root)
        ok_count, fail_list, dest, skipped, _ = self._download_files(
            repo_id, branch, dest_root, file_paths,
            on_progress, should_cancel, manifest, max_workers)
        self._save_manifest(dest_root, manifest)
        log(f"递归下载结束：新增 {ok_count} 个，跳过已存在 {skipped} 个，"
            f"失败 {len(fail_list)} 个。")
        return ok_count, fail_list, dest, skipped

    # ------------------------------------------------------ 底层抓取与解析
    def _fetch_browse(self, repo_id: str, branch: str = "", path: str = "") -> httpx.Response:
        url = (f"{self.config.jira_url.rstrip('/')}/secure/GIJBrowseGit.jspa"
               f"?repoId={repo_id}&branchName={urllib.parse.quote(branch)}"
               f"&tagName=&commitId=&path={urllib.parse.quote(path)}")
        return self.http_get(url, headers=self.cookie_headers(),
                             watchdog=self._watchdog)

    def _parse_repo_info(self, html: str) -> dict:
        """从 ns.repoInfo 解析 displayName(仓库名) 与 lastCommit.name(分支 HEAD)。

        使用括号配平扫描，避免旧的 ``(\\{.*?\\});`` 正则遇到嵌套 lastCommit 对象时
        误截断 JSON 导致解析失败。
        """
        info: dict = {}
        m = re.search(r'ns\.repoInfo\s*=\s*', html, re.S)
        if not m:
            return info
        # 从首个 { 起做括号配平，截出完整 JSON 对象
        start = html.find("{", m.end())
        if start == -1:
            return info
        depth = 0
        end = -1
        for i in range(start, len(html)):
            ch = html[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return info
        try:
            d = json.loads(html[start:end])
        except Exception:
            return info
        info["displayName"] = d.get("displayName")
        info["repoId"] = d.get("id") or d.get("repoId")
        lc = d.get("lastCommit") or {}
        head = lc.get("name") if isinstance(lc, dict) else None
        if not head:
            head = d.get("headCommit")
        info["headCommit"] = head
        return info

    def _parse_tree_files(self, html: str) -> list:
        """从 ns.data.files 解析当前目录条目。"""
        files: list = []
        m = re.search(r'ns\.data\s*=\s*(\{.*?\});', html, re.S)
        if not m:
            return files
        try:
            d = json.loads(m.group(1))
        except Exception:
            return files
        for f in d.get("files", []):
            files.append({
                "path": f.get("path"),
                "is_dir": bool(f.get("directory")),
                "size": f.get("size"),
            })
        return files

    def _local_file_read(self, root: Path, path: str):
        """读取本地克隆文件，用于 PAT 模式预览。

        与 Cookie 模式一致地识别二进制：二进制按字节返回，供上层（get_file）
        给出「二进制文件，请下载」的友好提示；文本按 UTF-8 解码返回。
        旧实现总用 read_text(errors='replace') 返回 str，导致 get_file 的
        isinstance(content, bytes) 判断永远失效、二进制预览显示乱码——
        与之前 docx 二进制识别同类的问题。
        """
        p = root / path
        if not str(p.resolve()).startswith(str(root.resolve())):
            raise ValueError("非法路径")
        data = p.read_bytes()
        if self._is_likely_text(data, ""):
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return data
        return data

    def _cookie_file_content(self, repo_id: str, head_commit: str, path: str,
                             client: Optional["httpx.Client"] = None) -> tuple:
        """返回 (ok, content, note)。

        ``content`` 可能是 ``str``（文本文件）或 ``bytes``（二进制文件）。
        依次尝试：commit SHA -> 分支名 作为引用；REST 裸接口 -> JSP 查看页。
        根目录与嵌套子目录文件均可（插件接口本身支持任意 path，
        旧版“仅根目录”限制已移除）。

        ``client`` 可选：批量下载时传入共享的 httpx 客户端（线程安全，可复用），
        避免每文件新建客户端；不传则回退到 ``http_get``（每次新建，适合单请求）。
        """
        # 引用优先级：HEAD commit SHA 优先，失败再用分支名（某些仓库 lastCommit 取不到）
        refs = []
        if head_commit:
            refs.append(head_commit)
        if self.branch and self.branch not in refs:
            refs.append(self.branch)
        if not refs:
            return False, None, "缺少可用的引用（commit/分支），无法定位文件"

        _get = (lambda u, h: self._request_with(client, u, h, watchdog=self._watchdog)) \
            if client is not None \
            else (lambda u, h: self.http_get(u, h, watchdog=self._watchdog))

        # 路径保留字面斜杠（插件接口以 / 划分目录层级，quote 会把 / 变成 %2F 导致 404）
        qpath = urllib.parse.quote(path, safe="/")
        for ref in refs:
            # 1) REST 裸接口（文本 / 二进制文件，含嵌套路径）
            rest = (f"{self.config.jira_url.rstrip('/')}/rest/gitplugin/1.0/files/"
                    f"{repo_id}/{urllib.parse.quote(ref)}/{qpath}")
            r = _get(rest, self.cookie_headers())
            ct = (r.headers.get("content-type") or "").lower()
            # 仅当响应「看起来像 JSON 错误信封」时才绕过 REST 干净内容、改走 JSP
            # 文本提取；否则（含 config.json 等以 {[ 开头的合法文本文件）直接按字节处理。
            # 旧实现用「首字节 { 或 [」启发式，会把大量合法文本文件误判为 JSON 信封、
            # 错误改走 JSP 提取（常提取失败），与之前 openxml 子串误判完全同类。
            is_json_like = "json" in ct and self._looks_like_error_envelope(r.content)
            if r.status_code == 200 and not is_json_like:
                data = r.content
                # 文本型内容按 utf-8 写入；否则按二进制字节落盘，避免图片/压缩包被破坏
                if self._is_likely_text(data, ct):
                    # 严格解码：_is_likely_text 已确认是合法 UTF-8，不会丢字节；
                    # 若极端误判导致严格解码失败，则退回字节落盘，
                    # 绝不偷偷用 errors='replace' 把二进制写坏（曾有 docx 因此变乱码）
                    try:
                        return True, data.decode("utf-8"), ""
                    except UnicodeDecodeError:
                        return True, data, ""
                return True, data, ""
            # 2) JSP 查看页（含 .json 等被当二进制的文件；此处只能取文本）
            jsp = (f"{self.config.jira_url.rstrip('/')}/secure/GIJViewGitFileContent.jspa"
                   f"?revision={urllib.parse.quote(ref)}&repoId={repo_id}"
                   f"&path={qpath}")
            r2 = _get(jsp, self.cookie_headers())
            if r2.status_code == 200:
                # JSP 仅用于文本：二进制无法从 HTML 可靠还原，跳过以避免 r2.text
                # 把二进制按 UTF-8 解码（errors='replace'）写坏
                if self._is_likely_text(r2.content, r2.headers.get("content-type", "")):
                    from_html = self._extract_code_from_html(r2.text)
                    if from_html is not None:
                        return True, from_html, ""
        # 全失败：给出可诊断信息
        return (False, None,
                f"无法获取文件（已尝试引用 {refs}；REST/JSP 均未返回可用正文）。"
                f"该文件可能需用 PAT 模式克隆后下载。")

    @staticmethod
    def _looks_like_error_envelope(data: bytes) -> bool:
        """判断 REST 返回的 JSON 是否「错误信封」而非文件内容。

        仅当 content-type 为 json 且内容像错误信封（success:false / errorMessage 等）
        时，才认为它不是文件内容、需要回退到 JSP 提取。真 JSON 文本文件
        （如 config.json）虽也以 { 开头，但不含这些错误标记，应直接按文本返回。
        """
        head = data[:1024].lower()
        return (b'"success":false' in head
                or b'"success": false' in head
                or b'"errorcode"' in head
                or b'"errormessage"' in head
                or b'{"error"' in head)

    @staticmethod
    def _is_likely_text(data: bytes, content_type: str) -> bool:
        """判断是否应作为文本处理：文本型 content-type，或内容为合法 UTF-8 且无空字节。

        注意：content-type 仅取 ``;`` 之前的主类型，避免 ``charset=UTF-8`` 等后缀干扰；
        并对二进制 MIME（office / 压缩包 / 图片 / 音视频 等）做**显式排除**——
        例如 ``application/vnd.openxmlformats-officedocument...`` 含子串 ``xml``，
        绝不能靠子串匹配当成文本，否则二进制会被按 UTF-8 解码（errors='replace'）写坏。
        设计上「拿不准一律当二进制（保留字节），绝不解码」——文本误判为二进制至多没有预览，
        二进制误判为文本则会不可逆损坏文件。
        """
        ct = (content_type or "").lower().split(";")[0].strip()
        if not ct:
            # 无 content-type：完全依赖字节启发式
            if not data:
                return True
            if b"\x00" in data[:4096]:
                return False
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                return False
            return True
        # 显式二进制 MIME：强制按字节处理（落盘用 write_bytes）
        _BINARY_MIME_PREFIXES = (
            "application/octet-stream", "application/vnd.", "application/zip",
            "application/gzip", "application/x-tar", "image/", "audio/", "video/",
            "application/pdf", "application/msword", "application/mspowerpoint",
            "application/msexcel", "application/x-ms",
        )
        if ct.startswith(_BINARY_MIME_PREFIXES) or "officedocument" in ct:
            return False
        # 已知文本 MIME（整词 / 前缀匹配，不靠子串，避免 openxmlformats 中的 'xml' 误判）
        if (ct.startswith("text/")
                or ct in ("application/json", "application/xml", "application/javascript",
                          "application/ecmascript", "application/html")
                or ct.endswith("+xml")):
            return True
        # 其余未知 MIME：字节启发式兜底（含空字节 / 非法 UTF-8 => 二进制）
        if not data:
            return True
        if b"\x00" in data[:4096]:
            return False
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    @staticmethod
    def _extract_code_from_html(html_text: str):
        """从 GIJViewGitFileContent 页面提取文件正文，兼容多种渲染结构。

        优先级：
        1) 旧版 diff code 单元格（bbb-gp-diff_code-cell-content）
        2) 任意 <code> 块
        3) <pre> 代码块（新版插件常见）
        4) 兜底：整页去标签后取“像代码”的最大文本块
        """
        import html as html_lib

        def clean(block: str) -> str:
            t = re.sub(r"<[^>]+>", "", block)
            t = html_lib.unescape(t)
            t = t.replace("\u00a0", " ")  # &nbsp; -> 普通空格
            return t

        # 1) + 2) <code>
        rows = re.findall(
            r'<code[^>]*class="[^"]*bbb-gp-diff_code-cell-content[^"]*"[^>]*>(.*?)</code>',
            html_text, re.S)
        if not rows:
            rows = re.findall(r'<code[^>]*>(.*?)</code>', html_text, re.S)
        if rows:
            out = [clean(r).rstrip("\r") for r in rows]
            joined = "\n".join(out)
            return joined + ("\n" if joined else "")

        # 3) <pre> 代码块
        pres = re.findall(r'<pre[^>]*>(.*?)</pre>', html_text, re.S)
        if pres:
            out = [clean(p).rstrip("\r") for p in pres]
            joined = "\n".join(out)
            return joined + ("\n" if joined else "")

        # 4) 兜底：去 script/style 后逐标签拆分为行，仅保留“像代码”的行
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.S)
        text = re.sub(r"<[^>]+>", "\n", body)
        text = html_lib.unescape(text).replace("\u00a0", " ")
        lines = [ln.rstrip() for ln in text.splitlines()]
        markers = (";", "{", "}", "=", "function", "<?php", "import", "def ",
                   "class ", "<", "return", "SELECT")
        kept = [ln for ln in lines if any(m in ln for m in markers)]
        if len(kept) >= 3:
            return "\n".join(kept) + "\n"
        return None
