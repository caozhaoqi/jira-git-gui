# -*- coding: utf-8 -*-
"""JiraGitClient 的连接 / HTTP 基础 Mixin。

拆分自 ``core/client.py``。负责：连接配置、Cookie/PAT 编解码、带重试与
全局令牌桶限流的 HTTP GET、keep-alive 连接池管理。

本 Mixin 不定义 ``__init__``（由主类 ``JiraGitClient`` 统一初始化共享状态），
仅通过 ``self`` 访问实例属性；方法在运行时与主类其他方法共存于同一实例，
因此可自由调用其它 Mixin 提供的 ``self.xxx`` 方法。
"""
import base64
import time
from typing import Optional

import httpx

from core.constants import HTTP_TIMEOUT, PROXY_URL, DEFAULT_REQUEST_QPS
from core.errors import UserError
from core.models import ConnectConfig
from core import throttle


def _should_backoff(r: "httpx.Response") -> bool:
    """判断该响应是否需要退避重试（限流/服务暂不可用）。"""
    return r.status_code in {429, 503}


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


class ConnectionMixin:
    """连接 / HTTP 基础能力。"""

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
        import re
        m = re.match(r"https?://([^/]+)", u)
        return m.group(1) if m else u

    # ------------------------------------------------------------------ 网络
    def http_get(self, url: str, headers: Optional[dict] = None,
                 retries: int = 5,
                 watchdog: Optional["object"] = None) -> httpx.Response:
        """带重试的 GET：复用 keep-alive 连接池，传输层异常时丢弃重建。

        发请求前先经全局令牌桶限流（``throttle.acquire``），确保无论并发多大，
        对 Jira 服务器的稳态请求速率都被钳住。遇到 429/503 时读取 ``Retry-After`` 头做长退避。
        """
        last = None
        for attempt in range(retries):
            if watchdog and watchdog.should_abort():
                raise httpx.TransportError(
                    f"网络已中断，自动停止请求（连续失败 {watchdog.failure_count} 次）")
            try:
                throttle.acquire()
                client = self._get_http_client()
                try:
                    r = client.get(url, headers=headers or {})
                except Exception:
                    # 传输层异常：连接可能损坏，丢弃以便下次重建
                    self._drop_http_client()
                    raise
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

    def _get_http_client(self) -> "httpx.Client":
        """懒创建 / 复用 keep-alive 客户端（线程安全，可跨线程共享）。"""
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _drop_http_client(self) -> None:
        """丢弃当前连接池（传输层异常后调用，下次请求重建）。"""
        c, self._http_client = self._http_client, None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _make_client(self) -> "httpx.Client":
        """为批量请求创建可复用的 httpx 客户端（带代理 / 重试参数）。"""
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
                      watchdog: Optional["object"] = None) -> httpx.Response:
        """用给定（共享）客户端发带重试的 GET，重试语义与 ``http_get`` 一致。"""
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
        """若 PAT 为 base64('account:secret') 形态，返回其内嵌密钥。"""
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
            ok, msg = self._pat_test_quick(self.config.pat, self.config.username)
            result["patTest"] = {"ok": ok, "msg": msg}
        return result
