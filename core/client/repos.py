# -*- coding: utf-8 -*-
"""JiraGitClient 的「仓库发现」Mixin。

拆分自 ``core/client.py``。负责发现【全部】仓库：HTML 全量页解析 + REST 接口翻页遍历，
合并去重、结果缓存与原始响应诊断。详见各方法 docstring。

共享常量在此重新定义（与 ``core/client.py`` 顶部一致），避免与聚合主类形成循环 import。
"""
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from core.constants import REPOS_DIR
from core.models import RepoInfo
from core import throttle
from core.logger import get_logger

logger = get_logger("jira-git-gui")

# REST 仓库列表翻页参数
REST_PAGE_SIZE = 100   # 每页仓库数（服务端硬上限 100）
REST_MAX_PAGES = 500   # 安全上限：最多翻 500 页，防止异常时死循环

# 仓库发现候选 REST 端点（按优先级）
REST_ENDPOINTS = (
    "/rest/gitplugin/1.0/repository/all",
    "/rest/gitplugin/1.0/repositories",
    "/rest/gitplugin/latest/repositories",
    "/rest/git/1.0/repository",
)

# HTML 全部仓库页翻页参数
HTML_PAGE_SIZE = 100
HTML_MAX_PAGES = 50

# 全部仓库浏览页
ALL_REPOS_PAGE = "/secure/GIJRepositoryBrowser-AllRepositories.jspa"

# 匹配仓库链接锚点
_REPO_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href="([^"]*?GIJ[A-Za-z]*\.jspa\?[^"]*?repoId=(\d+)[^"]*)"[^>]*>'
    r'(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# 名称明显不是仓库名的噪声锚点
_NOISE_NAMES = {"", "commits", "files", "branches", "tags", "browse", "view", "clone"}

# 这些状态码表示「该 REST 端点对当前实例/账号确实不可用」
_REST_DEAD_STATUS = {401, 403, 404, 405, 410}
# 授权类状态码：与「端点不存在（404/410）」本质不同 —— 401/403 是 Cookie 会话过期，
# 更新 Cookie 后即可恢复。因此**不能**把它们缓存成「REST 端点不可用」，
# 否则用户即使更新了 Cookie，REST 也永远不会再探测（而全量仓库主要来自 REST）。
_AUTH_ERROR_STATUS = {401, 403}


class ReposMixin:
    """仓库发现能力。"""

    # ----------------------------------------------------- 仓库发现（Cookie）
    def discover_repos(self, force: bool = False) -> List[RepoInfo]:
        """发现【全部】仓库（Cookie 模式），翻页遍历、合并且记录发现数。"""
        if not self.config.cookie:
            return []
        if not force:
            cached = self._load_repo_cache()
            # 关键：用真值判断而非 `is not None`。空列表 [] 不是「有效缓存」，
            # 否则一次失败（如 Cookie 过期导致发现 0 个）会把空结果缓存 10 分钟，
            # 期间反复返回空列表，并把真实故障掩盖成一句「命中缓存 0 个」。
            if cached:
                logger.info("仓库发现：命中缓存 %d 个（10 分钟内），force=False 跳过网络请求",
                            len(cached))
                return cached

        saved_qps = throttle.get_rate_limiter().qps
        try:
            throttle.set_global_rate_limit(max(saved_qps, 30))
            merged = self._do_discover_repos()
        finally:
            throttle.set_global_rate_limit(saved_qps)

        self._save_repo_cache(merged)
        return merged

    def _do_discover_repos(self) -> List[RepoInfo]:
        """实际执行发现（限流已放宽，供 discover_repos 调用）。"""
        # 本次扫描是否出现过「登录态失效」信号（跳转登录页 / 401 / 403）。
        # 每次扫描前重置，供最后的 0 结果提示区分「Cookie 过期」与「接口不可用」。
        self._last_auth_failed = False
        ts = time.strftime("%Y%m%d_%H%M%S")
        raw_path = Path("logs") / f"discover_raw_{ts}.txt"
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_fp = raw_path.open("w", encoding="utf-8")
        except Exception:
            raw_fp = None
        try:
            html = self._discover_repos_html(raw_fp, max_pages=1)
            if self._rest_unavailable:
                logger.info("[发现-REST] 已缓存「REST 端点不可用」，本次跳过 REST 探测。")
                rest: Dict[str, RepoInfo] = {}
            else:
                rest = self._discover_repos_rest(raw_fp)
        finally:
            if raw_fp:
                raw_fp.close()
        if raw_path.exists():
            logger.info("仓库发现原始接口响应已写入：%s", raw_path)
        merged: Dict[str, RepoInfo] = {}
        if rest:
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
            for rid, ri in html.items():
                merged[rid] = ri
        if not merged:
            # 区分「Cookie 过期」与「接口不可用」，前者才是 0 结果的绝大多数真因
            if getattr(self, "_last_auth_failed", False):
                logger.warning("仓库发现：0 个 —— 检测到跳转登录页或 401/403，"
                               "Jira Cookie 会话已过期。请在「连接设置」更新 Cookie 后重试。")
            else:
                logger.warning("仓库发现：0 个。可能会话已过期，或该账号无可见仓库，"
                               "或 REST/HTML 接口均不可用。")
        else:
            logger.info("仓库发现完成：HTML 页面解析 %d 个，REST 全量遍历 %d 个，"
                        "合并去重后共 %d 个。", len(html), len(rest), len(merged))
        return sorted(merged.values(), key=lambda x: x.display_name.lower())

    # ---- 仓库列表缓存（store/repos_cache.json，TTL 10 分钟） ----
    _REPO_CACHE_FILE = REPOS_DIR.parent / "repos_cache.json"
    _REPO_CACHE_TTL = 600  # 秒

    def _save_repo_cache(self, repos: List[RepoInfo]) -> None:
        # 空结果不写入缓存：否则「发现 0 个」会被当作有效缓存保存 10 分钟，
        # 期间反复返回空列表，并把 Cookie 过期这类真实故障掩盖成「命中缓存 0 个」。
        if not repos:
            logger.info("仓库列表缓存：本次发现 0 个，不写入缓存（避免空结果掩盖真实故障）")
            return
        try:
            data = {
                "ts": time.time(),
                "repos": [
                    {
                        "repo_id": r.repo_id,
                        "display_name": r.display_name,
                        "clone_url": r.clone_url,
                        "default_branch": r.default_branch,
                    }
                    for r in repos
                ],
            }
            self._REPO_CACHE_FILE.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            logger.info("仓库列表缓存已写入：%s（%d 个）", self._REPO_CACHE_FILE, len(repos))
        except Exception as e:
            logger.warning("仓库列表缓存写入失败：%s", e)

    def _load_repo_cache(self) -> Optional[List[RepoInfo]]:
        try:
            if not self._REPO_CACHE_FILE.exists():
                return None
            data = json.loads(self._REPO_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - float(data.get("ts", 0)) > self._REPO_CACHE_TTL:
                logger.info("仓库列表缓存已过期（>%d 秒），重新发现", self._REPO_CACHE_TTL)
                return None
            repos = [RepoInfo(**r) for r in data.get("repos", [])]
            return sorted(repos, key=lambda x: x.display_name.lower())
        except Exception as e:
            logger.warning("仓库列表缓存读取失败（忽略，重新发现）：%s", e)
            return None

    def _discover_repos_html(self, raw_fp=None, max_pages: int = HTML_MAX_PAGES) -> Dict[str, RepoInfo]:
        """翻页遍历 AllRepositories HTML 页面，返回 repoId -> RepoInfo（含 branchName）。"""
        out: Dict[str, RepoInfo] = {}
        base_url = self.config.jira_url.rstrip("/") + ALL_REPOS_PAGE
        try:
            for page_idx in range(max_pages):
                url = f"{base_url}?pageSize={HTML_PAGE_SIZE}&pageIndex={page_idx}"
                r = self.http_get(url, headers=self.cookie_headers())
                tag = f"HTML AllRepositories [page {page_idx}]"
                self._dump_raw(raw_fp, tag, url, r)
                if r.status_code != 200 or "login" in str(r.url):
                    # 标记登录态失效，供最终提示区分「Cookie 过期」与「接口不可用」
                    self._last_auth_failed = True
                    logger.warning("[发现-HTML] page=%d 状态码=%s 或跳转登录页（%s），停止翻页",
                                   page_idx, r.status_code, r.url)
                    break
                page_repos = self._parse_repo_list(r.text)
                prev_count = len(out)
                for ri in page_repos:
                    out[ri.repo_id] = ri
                new_count = len(out) - prev_count
                logger.info("[发现-HTML] page=%d 状态=%s，本页 %d 个（新增 %d），累计 %d",
                            page_idx, r.status_code, len(page_repos), new_count, len(out))
                if not page_repos:
                    logger.info("[发现-HTML] page=%d 为空页，翻页结束", page_idx)
                    break
                total_hint = self._extract_total_repos(r.text)
                if total_hint is not None and len(out) >= total_hint:
                    logger.info("[发现-HTML] 已累计 %d 个（>= 页面声明总数 %d），翻页结束",
                                len(out), total_hint)
                    break
                if len(page_repos) < HTML_PAGE_SIZE:
                    logger.info("[发现-HTML] page=%d 仅 %d 个 < pageSize=%d，末页",
                                page_idx, len(page_repos), HTML_PAGE_SIZE)
                    break
        except Exception as e:
            logger.warning("[发现-HTML] 翻页异常：%s", e)
        return out

    @staticmethod
    def _extract_total_repos(html: str) -> Optional[int]:
        """从「Showing 1 - 100 repositories out of 385」提取仓库总数。"""
        m = re.search(r'out\s+of\s+(\d+)', html, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def _discover_repos_rest(self, raw_fp=None) -> Dict[str, RepoInfo]:
        """翻页遍历 git 插件 REST 仓库列表，返回 repoId -> RepoInfo（权威全量）。"""
        out: Dict[str, RepoInfo] = {}
        saw_dead = False
        saw_auth = False   # 401/403 或跳转登录页：授权失效，Cookie 更新后可恢复
        base = self.config.jira_url.rstrip("/")
        for ep in REST_ENDPOINTS:
            if out:
                break
            ep_out: Dict[str, RepoInfo] = {}
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
                            login_like = self._looks_like_login(r)
                            # 401/403 或跳转登录页 = 授权失效（Cookie 过期），更新 Cookie 即可恢复，
                            # 绝不能与 404「端点不存在」一样被缓存成永久不可用。
                            if r.status_code in _AUTH_ERROR_STATUS or login_like:
                                saw_auth = True
                                self._last_auth_failed = True
                            elif r.status_code in _REST_DEAD_STATUS:
                                saw_dead = True
                            logger.warning("[发现-REST] %s [%s] %s：状态=%s%s",
                                           ep, cname, start, r.status_code,
                                           "（疑似登录页）" if login_like else "")
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
                        if total is not None and len(ep_out) >= total:
                            break
                        if got < REST_PAGE_SIZE:
                            break
                        if len(ep_out) == prev:
                            break
                        start += REST_PAGE_SIZE
                    except Exception as e:
                        saw_dead = True
                        logger.warning("[发现-REST] %s [%s] %s 异常：%s",
                                       ep, cname, start, e)
                        break
            if ep_out:
                out.update(ep_out)
        if not out and saw_dead and not saw_auth:
            self._rest_unavailable = True
            logger.info("[发现-REST] 所有 REST 端点均不可用（多为 404），已缓存该结论；"
                        "后续「发现仓库」将跳过 REST 探测以节省请求。")
        elif not out and saw_auth:
            logger.warning("[发现-REST] 端点返回 401/403 或跳转登录页 —— 这是 Cookie 会话过期，"
                           "并非端点不存在；因此不缓存「不可用」结论，更新 Cookie 后会自动重试。")
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
        """粗略判断响应是否为登录页。"""
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
        """把 REST 响应归一为 ``(items, total)``。"""
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
        """从 REST 仓库对象里尽量提取可用的 clone URL。"""
        for key in ("gkRepoUrl", "glRepoUrl", "cloneUrl", "url", "remoteUrl", "sshUrl"):
            v = it.get(key)
            if not isinstance(v, str) or not v:
                continue
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
        """从单个 REST 仓库对象构造 RepoInfo（无 id 则忽略）。"""
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
            clone_url=ReposMixin._extract_clone_url(it),
            default_branch=it.get("defaultBranch") or it.get("branchName") or "",
        )

    @staticmethod
    def _strip_tags(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s or "").strip()

    def _parse_repo_list(self, html: str) -> List[RepoInfo]:
        """从 AllRepositories 页面解析仓库列表（repoId 去重，同名保留最长名）。"""
        repos: dict = {}
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
