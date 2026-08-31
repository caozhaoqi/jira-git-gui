# -*- coding: utf-8 -*-
"""JiraGitClient 的「浏览 / 树 / 提交」Mixin。

拆分自 ``core/client.py``。负责：文件层级浏览（list_level）、分支探测与解析、
本地克隆下的替代浏览路径，以及 commit 历史获取与解析。

共享常量在此重新定义（与 ``core/client.py`` 顶部一致），避免与聚合主类形成循环 import。
"""
import json
import os
import re
from pathlib import Path
from typing import List, Optional

from core.constants import REPOS_DIR
from core.models import TreeEntry, Commit, CommitFile
from core.errors import UserError
from core.logger import get_logger
from core import cache as tree_cache

logger = get_logger("jira-git-gui")

# 分支解析正则
_RE_BRANCH_IN_PAGE = re.compile(r'<input[^>]+name="branchName"[^>]+value="([^"]*)"', re.I)
_RE_HIDDEN_BRANCH = re.compile(
    r'<input[^>]+type="hidden"[^>]+name="branchName"[^>]+value="([^"]*)"', re.I)
_RE_BRANCH_IN_JSON = re.compile(r'"branchName"\s*:\s*"([^"]*)"')
_RE_BRANCH_OPTION = re.compile(r'<option[^>]+value="([^"]*)"[^>]*>\s*([^<]*)\s*</option>', re.I)
_RE_VIEW_BRANCH = re.compile(r'[?&]branchName=([^&\s"\'<>]+)')

# --------------------------------------------------------------- 登录页判定
# ⚠️ 这里踩过大坑：Jira 11 的登录页会让「朴素判定」三个条件全部失效——
#   1) 标题是中文「登录到 Atlassian - Jira」，前 500 字符里**没有** ASCII "login"
#      （页面开头还有 7 个换行占位，把 "login" 进一步挤出窗口）；
#   2) 不再使用 j_security_check（那是老版本 Jira / Bamboo 的表单 action）；
#   3) os_password 是 Confluence / Bamboo 的字段，Jira 根本没有。
# 结果：GIJBrowseGit.jspa 明明返回了登录页，_is_login_page 却判 False，
# 于是工具拿登录页去跑文件树正则 → 0 条 → 文件树空白且**零报错**，
# 用户只能看到「文件树是空的」，完全无从定位。
# 现改为：以 <title> 为主判据（中英文都覆盖），再辅以表单/链接特征与未登录 meta。
_RE_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_RE_LOGIN_TITLE = re.compile(
    r"登\s*录|登\s*錄|登\s*陆|log\s*in|sign\s*in|authenticat|anmelden|connexion", re.I)
# 登录页特有的表单 / 链接 / 容器特征（小写比对）
_LOGIN_MARKERS = (
    'name="os_username"',      # Jira 登录表单用户名域（最稳的 Jira 特征）
    'name="j_username"',       # 老版本 Jira
    'id="login-form"',
    'id="login"',
    'class="login"',
    '/login.jspa',
    'login.jspa?',
    'j_security_check',
    'os_password',
)
# 未登录标记：Jira 在所有页面注入该 meta，登录页时 content 为空
_RE_ANON_USER = re.compile(r'name="ajs-remote-user"\s+content=""', re.I)

# 树判断正则
_TREE_PRESENT_RE = re.compile(r'id="[^"]*treeTable|class="[^"]*tree', re.I)
# 目录链接（仅用于 _browse_has_tree 的辅助判定；异步渲染实例的树已由 _extract_tree_files 抽 JSON）。
# 要求 GIJ + branchName + **非空** path= 值。注意两点：
#  1) 导航 tab（文件/提交/比较/分支）的 href 形如 `...branchName=master&path=`（path= 为空），
#     已由「非空 path= 值」排除；
#  2) 必须加 `(?<![A-Za-z])` 负向回顾，否则大小写不敏感下 `filePath=README.md` 里的
#     `Path=` 也会被当成 `path=` 命中，把文件链接误判成目录。
_TREE_LINK_RE = re.compile(
    r'<a\b[^>]*href="([^"]*?GIJ[^"]*?)(?<![?&])branchName=([^&\s"\'<>]+)'
    r'[^"]*?(?<![A-Za-z])path=([^&\s"\'<>]+)[^"]*"[^>]*>(.*?)</a>',
    re.I | re.S)
_FILE_LINK_RE = re.compile(
    r'<a\b[^>]*href="([^"]*?files/[^"]*?branchName=([^&\s"\'<>]+)[^"]*?filePath=([^&\s"\'<>]+)[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S)
_DIR_RE = re.compile(r'''<a\b[^>]*href="([^"]*)"[^>]*>\s*([^<]*)</a>''', re.I)

# Cookie 模式的 GIJBrowseGit 页面每个目录都要走一次远端渲染（实测单次可达 10s 量级）。
# 目录内容短时间内通常不会变化，缓存 5 分钟可消除「来回切换目录 / 刷新页面 /
# 重复跑差异扫描」造成的重复等待，但不会把缓存当作长期镜像；
# 调用方传 refresh=True 或调 invalidate_remote_tree_cache() 可强制重新请求。
_REMOTE_TREE_CACHE_TTL = 300


def _tree_cache_namespace(repo_id: str) -> str:
    return f"remote-tree-{repo_id}"


def _tree_cache_key(branch: str, path: str) -> str:
    return f"{branch or ''}|{path.lstrip('/') if path else ''}"


def _tree_entries_from_cache(data) -> Optional[List[TreeEntry]]:
    if not isinstance(data, list):
        return None
    try:
        result = []
        for item in data:
            if not isinstance(item, dict):
                return None
            result.append(TreeEntry(
                name=str(item.get("name") or ""),
                path=str(item.get("path") or ""),
                type="dir" if item.get("type") == "dir" else "file",
                size=item.get("size"),
                has_children=bool(item.get("has_children")),
                mtime=item.get("mtime"),
            ))
        return result
    except (TypeError, ValueError):
        return None


def _tree_entries_for_cache(entries: List[TreeEntry]) -> list[dict]:
    return [
        {
            "name": entry.name,
            "path": entry.path,
            "type": entry.type,
            "size": entry.size,
            "has_children": entry.has_children,
            "mtime": entry.mtime,
        }
        for entry in entries
    ]


def invalidate_remote_tree_cache(repo_id: str = "") -> int:
    """让远端目录缓存失效。

    Args:
        repo_id: 指定仓库则只清该仓库；留空则清所有仓库的远端目录缓存。

    Returns:
        清除的缓存条目数。
    """
    if not repo_id:
        # 「所有仓库」= 逐个 remote-tree-* 命名空间清除（cache.invalidate 只支持单命名空间）
        total = 0
        for ns in _remote_tree_namespaces():
            total += tree_cache.invalidate(ns)
        return total
    return tree_cache.invalidate(_tree_cache_namespace(str(repo_id)))


def _remote_tree_namespaces() -> list[str]:
    """列出所有远端目录缓存命名空间（用于无 repo_id 的整体失效）。"""
    root = tree_cache.CACHE_DIR
    if not root.exists():
        return []
    prefix = "remote-tree-"
    return [d.name for d in root.iterdir()
            if d.is_dir() and d.name.startswith(prefix)]


class BrowseMixin:
    """文件浏览 / 分支解析 / 提交历史能力。"""

    def list_level(self, repo_id: str, branch: str, path: str,
                   refresh: bool = False) -> List[TreeEntry]:
        """返回某仓库某分支某路径下的条目列表（目录优先、再文件，均按名称排序）。

        Args:
            refresh: True 时绕过目录缓存强制回源（远端浏览模式下生效）。
        """
        entries, _err = self.list_level_ex(repo_id, branch, path, refresh=refresh)
        return entries

    def list_level_ex(self, repo_id: str, branch: str, path: str,
                      refresh: bool = False):
        """同 :meth:`list_level`，但额外返回失败原因 ``(entries, error)``。

        ⚠️ 文件树长期「空但零报错」：Cookie 失效 / 分支解析失败 / 浏览页 404 时，
        老实现一律返回 ``[]``，``/api/tree`` 于是回 200 + ``{"entries": []}``，
        界面只显示「空」，用户既不知道原因也不知道该怎么办。
        这里把真实原因带出去，供接口层明确提示（如引导改用 PAT 克隆到本地）。
        成功时 error 为空串。
        """
        if self.config.mode == "pat" and (REPOS_DIR / str(repo_id)).exists():
            return self._list_level_local(repo_id, branch, path), ""

        resolved, why = self._resolve_branch_ex(repo_id, branch)
        if not resolved:
            return [], why or "未能解析出分支，无法浏览"
        entries, why = self._list_level_cookie_ex(
            repo_id, resolved, path, refresh=refresh)
        if why:
            return [], why
        dirs = sorted([e for e in entries if e.type == "dir"], key=lambda x: x.name.lower())
        files = sorted([e for e in entries if e.type == "file"], key=lambda x: x.name.lower())
        return dirs + files, ""

    def invalidate_tree_cache(self, repo_id: str = "") -> int:
        """让远端目录缓存失效（下次 list_level 强制回源）。

        Args:
            repo_id: 指定仓库；留空则用当前选中仓库，仍为空则清所有仓库。

        Returns:
            清除的缓存条目数。
        """
        rid = repo_id or getattr(self, "repo_id", "") or ""
        n = invalidate_remote_tree_cache(rid)
        logger.info("[文件树] 已清除远端目录缓存（repo=%s，%d 条）", rid or "全部", n)
        return n

    @staticmethod
    def _is_login_page(html_text: str) -> bool:
        """判断页面是否为登录页（需要重新登录）。

        不能只看前 500 字符里有没有 "login"：中文站点的标题是「登录到 Atlassian」，
        且页面开头有一串换行占位，ASCII "login" 根本不在前 500 字符内。
        判据优先级：<title> → 表单/链接特征 → 未登录 meta 兜底。
        """
        text = html_text or ""
        if not text:
            return False
        low = text.lower()

        # 1) 标题：最稳，中英文与常见本地化都覆盖
        m = _RE_TITLE.search(text)
        if m and _RE_LOGIN_TITLE.search(m.group(1)):
            return True

        # 2) 登录表单 / 链接特征
        for marker in _LOGIN_MARKERS:
            if marker in low:
                return True

        # 3) 兜底：Jira 注入的 ajs-remote-user 为空 = 未登录。
        #    只有登录/匿名页会同时满足「未登录」且出现密码类文案。
        if _RE_ANON_USER.search(low) and re.search(r"password|密码|登录|log\s*in", low):
            return True
        return False

    @staticmethod
    def _browse_has_tree(html_text: str) -> bool:
        """判断浏览页是否含文件树（用于区分「仓库存在但无文件」与「页面被拦截」）。

        同步渲染实例：页面里有 ``treeTable`` / 树形 ``<a>`` 链接。
        异步渲染实例（Git Integration 6.1.7+）：树以内联 JSON 给出
        （``ns.data = {"files":[...]}``），初始 HTML 里没有可点击的树链接，
        所以这里额外检测内联 ``"files":[`` 数组，否则默认分支探测会误判为「无树」。
        """
        text = html_text or ""
        if _TREE_PRESENT_RE.search(text) or _TREE_LINK_RE.search(text):
            return True
        return bool(re.search(r'"files"\s*:\s*\[', text))

    def _resolve_branch(self, repo_id: str, branch: str) -> str:
        """解析实际浏览用的分支：优先用显式 branch；否则从默认分支缓存 / 浏览页隐式获取。

        结果缓存到 ``self._branch_cache``（按 repo 缓存），避免每个目录层级都重复探测。
        """
        resolved, _why = self._resolve_branch_ex(repo_id, branch)
        return resolved

    def _resolve_branch_ex(self, repo_id: str, branch: str):
        """同 :meth:`_resolve_branch`，但额外返回失败原因 ``(branch, reason)``。

        分支解析不出来时老实现只返回空串，调用方（``_list_level_cookie``）据此
        静默返回空列表，最终表现为「文件树空白、没有任何提示」。这里把原因说清楚：
        Cookie 过期（Jira 回了登录页）、浏览页 404、还是页面结构变了。
        """
        if branch:
            return branch, ""
        if repo_id in self._branch_cache:
            return self._branch_cache[repo_id], ""
        reason = ""
        # 1) REST 直取默认分支（最稳，不依赖浏览器渲染 / 不受登录页影响）。
        #    Xiplink 插件 6.1.7 的 GIJBrowseGit.jspa 不带 branchName 时会回登录页、
        #    带 branchName 才渲染，老实现靠抓 <input name=branchName> 永远拿不到分支。
        #    /rest/gitplugin/1.0/repository/branches 直接给出 mainBranch，干净可靠。
        if self.config.cookie:
            rb, rb_why = self._resolve_default_branch_rest(repo_id)
            if rb:
                return rb, ""
            # REST 失败的原因（Cookie 过期 / 端点未授权）先记录下来，作为最终兜底提示
            reason = rb_why or reason
        resolved = ""
        try:
            if self.config.cookie:
                url = (f"{self.config.jira_url.rstrip('/')}/secure/"
                       f"GIJBrowseGit.jspa?repoId={repo_id}")
                r = self.http_get(url, headers=self.cookie_headers())
                if self._is_login_page(r.text):
                    # 最常见真因：Jira 回了登录页。注意此时 HTTP 仍是 200，
                    # 只靠状态码完全判断不出来。
                    reason = ("Jira 返回的是登录页（HTTP 200）——Cookie 已失效，请到"
                              "「连接设置」重新粘贴 Cookie；或在 PAT 模式下点"
                              "「克隆仓库 (PAT)」改为本地浏览")
                elif r.status_code != 200:
                    reason = (f"浏览页请求失败（HTTP {r.status_code}），Jira 侧 Git "
                              f"Integration 浏览页不可用；可改用 PAT 模式克隆到本地浏览")
                else:
                    m = _RE_BRANCH_IN_PAGE.search(r.text) or _RE_HIDDEN_BRANCH.search(r.text)
                    if m:
                        resolved = m.group(1)
                    else:
                        mj = _RE_BRANCH_IN_JSON.search(r.text)
                        if mj:
                            resolved = mj.group(1)
                    if resolved:
                        self._branch_cache[repo_id] = resolved
                        return resolved, ""
                    reason = ("浏览页已返回但解析不到分支名——插件页面结构可能已变化，"
                              "或该仓库要求用户在 Jira 的 Git Integration 中配置 PAT")
            if (REPOS_DIR / str(repo_id)).exists():
                try:
                    import subprocess
                    out = subprocess.run(
                        ["git", "-C", str(REPOS_DIR / str(repo_id)),
                         "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=15)
                    if out.returncode == 0:
                        resolved = out.stdout.strip()
                        if resolved:
                            self._branch_cache[repo_id] = resolved
                            return resolved, ""
                except Exception:
                    pass
        except Exception:
            pass
        # 常规路径（页面隐式分支 / 本地 git HEAD）都失败：按常见默认分支名兜底探测。
        # 仅对「服务端渲染出文件树」的成功响应返回分支，避免对异步渲染实例误选分支。
        if not resolved:
            probed = self._probe_default_branch(repo_id)
            if probed:
                return probed, ""
            reason = ("分支解析失败且默认分支探测也无果——该 Jira 实例的远端浏览页可能不可用"
                      "（异步渲染 / 端点 404），建议改用 PAT 模式克隆到本地浏览")
        return resolved, reason

    def _probe_default_branch(self, repo_id: str) -> str:
        """分支解析失败时按常见默认分支名逐一探测，返回首个可正常浏览的分支。

        仅在 :meth:`_resolve_branch_ex` 常规路径全失败时调用，结果同样写入 ``_branch_cache``
        （按 repo 缓存，避免重复探测）。对远端浏览页为异步渲染（初始 HTML 不含树）的实例，
        ``_browse_has_tree`` 为 False，这里不会误选分支；仅对服务端渲染树的成功返回分支。
        """
        for cand in ("master", "main", "develop", "trunk"):
            try:
                url = (f"{self.config.jira_url.rstrip('/')}/secure/GIJBrowseGit.jspa"
                       f"?repoId={repo_id}&branchName={cand}")
                r = self.http_get(url, headers=self.cookie_headers())
                if (r.status_code == 200
                        and not self._is_login_page(r.text)
                        and self._browse_has_tree(r.text)):
                    self._branch_cache[repo_id] = cand
                    return cand
            except Exception:
                continue
        return ""

    def _resolve_default_branch_rest(self, repo_id: str):
        """通过插件 REST 取默认分支：``/rest/gitplugin/1.0/repository/branches``。

        返回 ``(main_branch, reason)``。这是解析默认分支最稳的方式：
        不依赖浏览器渲染、也不会因为 GIJBrowseGit.jspa 不带 branchName 时回登录页
        而误判。优先用 ``mainBranch`` 字段，缺失时退而取首条 ``branchPairs``。
        失败（Cookie 过期 / 端点未授权）时返回 ("", 原因)，交由 HTML 探测兜底。
        """
        if not self.config.cookie:
            return "", "未配置 Cookie"
        try:
            url = (f"{self.config.jira_url.rstrip('/')}/rest/gitplugin/1.0/"
                   f"repository/branches?repoId={repo_id}")
            r = self.http_get(url, headers=self.cookie_headers())
            if r.status_code != 200 or self._is_login_page(r.text):
                return "", ("Jira 返回登录页（HTTP 200）——Cookie 已失效，请到"
                            "「连接设置」重新粘贴 Cookie；或在 PAT 模式下点"
                            "「克隆仓库 (PAT)」改为本地浏览")
            try:
                data = r.json()
            except Exception:
                return "", "分支接口返回非 JSON（可能插件版本不兼容）"
            main = data.get("mainBranch")
            if not main:
                pairs = data.get("branchPairs") or []
                main = pairs[0].get("branch") if pairs else None
            if main:
                self._branch_cache[repo_id] = main
                return main, ""
            return "", "分支接口未返回默认分支"
        except Exception as e:
            return "", f"解析默认分支失败：{e}"

    @staticmethod
    def _extract_tree_files(html_text: str):
        """从 GIJBrowseGit.jspa 页面抽取文件树 JSON。

        ⚠️ Xiplink 插件 6.1.7 的浏览页是**异步渲染**：目录内容在初始 HTML 里以内联
        JSON 形式给出——``ns.data = {"files":[{path, name, directory}, ...]}``
        （``directory`` 为 bool），而不是可点击的 ``<a href>``。老的 ``<a href>`` 正则
        只能匹配到导航标签（提交/比较/浏览），于是文件树里凭空出现 3 个假目录。

        这里直接抓内联 JSON，稳定且不受服务端渲染方式影响。找不到时返回 None
        （调用方据此报错，而不是返回假空列表）。

        ⚠️ 坑：页面里可能不止一处 ``"files":[``（例如别的脚本/配置也用同名数组，
        但其元素是 ``{id,label}`` 之类、不含 tree 字段）。若只取「第一个」数组，
        很可能误配到无关数组 → 解析出的条目因缺 ``path/name`` 被全部过滤 →
        文件树静默变空、还不报错，又回到「界面空、无从查」。所以这里枚举所有
        ``"files":[`` 数组，**优先选元素带 tree 特征（有 ``directory`` 或 ``path`` 键）
        的那个**；都没有时再退回第一个（保持旧行为）。
        """
        if not html_text:
            return None

        def _parse_array_from(start_bracket: int):
            """从下标 start_bracket（指向 '['）起，括号配平地解析出该 JSON 数组。"""
            depth = 0
            in_s = False
            esc = False
            for i in range(start_bracket, len(html_text)):
                ch = html_text[i]
                if esc:
                    esc = False
                    continue
                if ch == "\\" and in_s:
                    esc = True
                    continue
                if ch == '"':
                    in_s = not in_s
                    continue
                if in_s:
                    continue
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(html_text[start_bracket:i + 1])
                        except Exception:
                            return None
            return None

        candidates = []
        for m in re.finditer(r'"files"\s*:\s*\[', html_text):
            arr = _parse_array_from(m.end() - 1)  # m.end() 指向 '[' 之后，减一即 '[' 本身
            if arr is None:
                continue
            candidates.append(arr)

        if not candidates:
            return None

        # 优先选「像文件树」的数组：其元素多为 dict 且带 directory / path 键。
        def _looks_like_tree(arr):
            if not isinstance(arr, list) or not arr:
                return False
            score = 0
            for it in arr[:8]:  # 只看前几个元素即可判断
                if isinstance(it, dict) and ("directory" in it or "path" in it):
                    score += 1
            return score >= max(1, len(arr[:8]) // 2)

        for arr in candidates:
            if _looks_like_tree(arr):
                return arr
        # 没有典型 tree 数组：退回第一个（保留旧行为，避免漏掉结构特殊的实例）
        return candidates[0]

    def _resolve_head(self, repo_id: str, branch: str) -> str:
        """解析分支的 HEAD commit（用于精确、稳定的文件引用）。

        经由 ``self._branch_cache`` 同级缓存；无法解析时返回空字符串（调用方据此报错）。
        """
        key = (repo_id, branch)
        if key in self._head_cache:
            return self._head_cache[key]
        head = ""
        try:
            if self.config.cookie and self.config.jira_url:
                url = (f"{self.config.jira_url.rstrip('/')}/rest/git/1.0/repositories/"
                       f"{repo_id}/branches")
                r = self.http_get(url, headers=self.cookie_headers())
                if r.status_code == 200:
                    try:
                        data = r.json()
                        for item in (data.get("values") or []):
                            if item.get("displayId") == branch or item.get("id") == branch:
                                head = item.get("latestCommit") or item.get("head") or ""
                                break
                        if not head and data.get("values"):
                            head = (data["values"][0].get("latestCommit")
                                    or data["values"][0].get("head") or "")
                    except Exception:
                        head = ""
        except Exception:
            head = ""

        # 兜底：从仓库列表端点取 lastCommit.name。
        # 实测本实例的 /rest/git/1.0/repositories/{id}/branches 返回 404，
        # 而 /rest/gitplugin/1.0/repository/all 可用（total=388）且每个仓库都带
        # lastCommit.name —— 缺了这层兜底，get_file 会一直报「无法获取分支 HEAD commit」。
        if not head:
            head = self._head_from_repo_list(repo_id) or ""

        if head:
            logger.info("[HEAD] repo=%s 解析到 HEAD=%s", repo_id, head[:12])
        else:
            logger.warning("[HEAD] repo=%s 未能解析 HEAD commit。若该仓库 patInfo.userPatSetup=false，"
                           "需在 Jira 的 Git Integration 中为该用户配置 PAT", repo_id)

        self._head_cache[key] = head
        return head

    def _head_from_repo_list(self, repo_id: str) -> str:
        """从 ``/rest/gitplugin/1.0/repository/all`` 分页查找某仓库的 lastCommit.name。

        注意该端点的 limit 取值范围是 [1..100]（传 500 会 400 报错），故按 100 分页。
        """
        try:
            base = self.config.jira_url.rstrip('/')
            rid = str(repo_id)
            for offset in range(0, 1000, 100):
                url = f"{base}/rest/gitplugin/1.0/repository/all?limit=100&offset={offset}"
                r = self.http_get(url, headers=self.cookie_headers())
                if r.status_code != 200:
                    return ""
                items = (r.json() or {}).get("repositories") or []
                if not items:
                    return ""
                for it in items:
                    if str(it.get("id")) == rid:
                        lc = it.get("lastCommit") or {}
                        return str(lc.get("name") or "")
        except Exception as e:
            logger.warning("[HEAD] 从仓库列表解析 HEAD 失败（repo=%s）：%s", repo_id, e)
        return ""

    def _list_level_cookie(self, repo_id: str, branch: str, path: str,
                           refresh: bool = False) -> List[TreeEntry]:
        """Cookie 模式：解析浏览器文件树页面，返回某层级条目。"""
        entries, _err = self._list_level_cookie_ex(
            repo_id, branch, path, refresh=refresh)
        return entries

    def _list_level_cookie_ex(self, repo_id: str, branch: str, path: str,
                              refresh: bool = False):
        """同 :meth:`_list_level_cookie`，但额外返回失败原因 ``(entries, error)``。

        Args:
            refresh: True 时忽略缓存直接回源（并把新结果写回缓存）。
        """
        if not self.config.cookie:
            logger.warning("[文件树] 未配置 Cookie，无法浏览远端（repo=%s path=%s）",
                           repo_id, path or "/")
            return [], "未配置 Cookie，无法浏览远端"
        branch, why = self._resolve_branch_ex(repo_id, branch)
        if not branch:
            # 「文件浏览器看不到目录」的常见原因之一：分支解析不出来。
            # 远端浏览页不可用（如 Jira 插件页面 404）时就会走到这里。
            logger.warning("[文件树] 未能解析出分支，无法浏览（repo=%s path=%s）——%s",
                           repo_id, path or "/", why)
            return [], why or "未能解析出分支，无法浏览"
        # 同一目录经常因重新展开、切换标签页、刷新页面或重复跑差异扫描而重复请求。
        # 只缓存成功解析出的条目；登录页/404/结构变化等失败响应绝不落盘。
        # 注意：空列表也是有效结果（真空目录），_tree_entries_from_cache 会返回 []
        # 而不是 None，所以这里必须用 is not None 判定命中，不能用真值判断。
        cache_ns = _tree_cache_namespace(str(repo_id))
        cache_key = _tree_cache_key(branch, path)
        if not refresh:
            cached_entries = _tree_entries_from_cache(
                tree_cache.get(cache_ns, cache_key, ttl=_REMOTE_TREE_CACHE_TTL))
            if cached_entries is not None:
                logger.info("[文件树] 命中远端目录缓存（%d 条，repo=%s branch=%s path=%s）",
                            len(cached_entries), repo_id, branch, path or "/")
                return cached_entries, ""

        # ⚠️ 正确路径是 GIJBrowseGit.jspa（Jira Git Integration 插件的浏览页）。
        # 旧的 GIJRepositoryBrowser.jspa 对**所有**仓库都返回 404（死链接），
        # 导致文件树永远为空——这正是「浏览器能看、工具看不到」的原因。
        # 另：子路径参数名是 path（不是 filePath），与浏览器地址栏一致。
        url = (f"{self.config.jira_url.rstrip('/')}/secure/GIJBrowseGit.jspa"
               f"?repoId={repo_id}&branchName={branch}")
        if path:
            url += "&path=" + path.lstrip("/")
        r = self.http_get(url, headers=self.cookie_headers())
        if r.status_code != 200 or self._is_login_page(r.text):
            # 这里是最常见真因。此前静默返回空列表，界面只表现为「看不到目录」，无从定位：
            #  - 登录页   → Jira Cookie 会话过期，需在连接设置更新 Cookie
            #  - 非 200（多为 404）→ Jira 侧浏览页不可用，需改用 PAT 模式克隆到本地
            is_login = self._is_login_page(r.text)
            if is_login:
                why = ("Jira 返回登录页：Cookie 已失效，请到「连接设置」重新粘贴 Cookie；"
                       "或在 PAT 模式下点「克隆仓库 (PAT)」改为本地浏览")
            elif r.status_code != 200:
                why = (f"浏览页不可用（HTTP {r.status_code}）：Jira 侧 Git Integration "
                       f"浏览页访问失败，可改用 PAT 模式克隆到本地浏览")
            else:
                why = "浏览页请求失败"
            logger.warning(
                "[文件树] 远端浏览失败：状态=%s 登录页=%s（repo=%s branch=%s path=%s）——%s",
                r.status_code, is_login, repo_id, branch, path or "/", why)
            return [], why
        html_text = r.text
        # ⚠️ 关键修正：Jira Git Integration 插件（6.1.7）的浏览页是**异步渲染**的，
        # 文件树以内联 JSON 形式给出：
        #     ns.data = {"files":[{"path":"README.md","name":"README.md","directory":false}, ...]}
        # 旧代码用 _TREE_LINK_RE / _FILE_LINK_RE 去匹配 <a href> 链接，**只能匹配到
        # 顶部导航 tab（提交/比较/浏览）**，于是整棵树被解析成 3 个假目录。
        # 正确做法是直接抽取这段内联 JSON（见 _extract_tree_files）。
        files = self._extract_tree_files(html_text)
        if files is None:
            # 浏览页 200 但解析不出树——多半是插件页面结构变了（或异步渲染未就绪）。
            # 把页面前 600 字符打进日志，便于真实环境下定位（避免再次「界面空、无从查」）。
            snippet = (html_text or "")[:600].replace("\n", " ")
            logger.warning(
                "[文件树] 浏览页返回 200 但未解析到文件树（repo=%s branch=%s path=%s）。"
                "可能插件改为异步渲染且 ns.data 结构变化，或 Cookie 会话异常。页面前 600 字符：%s",
                repo_id, branch, path or "/", snippet)
            return [], ("浏览页未返回文件树数据（插件页面结构变化或异步渲染未就绪），"
                        "请在上方「本地目录」输入本地仓库路径，或改用 PAT 模式克隆到本地后浏览")

        entries: List[TreeEntry] = []
        seen = set()
        for item in files:
            is_dir = bool(item.get("directory"))
            name = item.get("name") or item.get("path") or ""
            path_ = item.get("path") or name
            if not name or name in seen:
                continue
            entries.append(TreeEntry(
                name=name,
                path=path_,
                type="dir" if is_dir else "file",
                has_children=is_dir,
            ))
            seen.add(name)

        if not entries:
            # 树 JSON 解析成功但为空数组（例如空目录），属正常情况，不报错误。
            logger.info("[文件树] 解析到 0 条条目（repo=%s branch=%s path=%s，可能是空目录或路径不存在）",
                        repo_id, branch, path or "/")
        else:
            logger.info("[文件树] 解析到 %d 条条目（repo=%s branch=%s path=%s）",
                        len(entries), repo_id, branch, path or "/")

        # 只有走到这里才代表「页面正常、树解析成功」——登录页 / 404 / 结构变化
        # 都在上方提前 return，不会被写进缓存。
        if not tree_cache.set(cache_ns, cache_key, _tree_entries_for_cache(entries)):
            logger.debug("[文件树] 远端目录缓存写入失败（repo=%s branch=%s path=%s）",
                         repo_id, branch, path or "/")
        return entries, ""

    def _list_level_local(self, repo_id: str, branch: str, path: str) -> List[TreeEntry]:
        """PAT/本地克隆模式：用 git ls-tree 解析目录层级（不依赖浏览器）。"""
        local_path = REPOS_DIR / str(repo_id)
        if not local_path.exists():
            return []
        branch = self._resolve_branch(repo_id, branch) or "HEAD"
        try:
            import subprocess
            res = subprocess.run(
                ["git", "-C", str(local_path), "ls-tree", branch, "--", path.lstrip("/")],
                capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return []
        except Exception:
            return []
        entries: List[TreeEntry] = []
        for line in res.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            meta, name = parts
            mode = meta.split()[1] if len(meta.split()) > 1 else ""
            is_dir = mode == "tree"
            entries.append(TreeEntry(
                name=name,
                path=(path.rstrip("/") + "/" + name) if path else name,
                type="dir" if is_dir else "file",
            ))
        return entries

    def list_level_local_dir(self, local_dir: str, path: str) -> List[TreeEntry]:
        """本地目录模式：用文件系统遍历（兼容无 .git 的目录）。

        与 :meth:`_list_level_local`（基于 ``git ls-tree``，要求本地克隆）不同，
        这里直接 ``os.scandir`` 给定目录，因此可用于任意本地工作副本（如未独立
        git 初始化的目录、或父仓库下的子目录）。``path`` 为相对 local_dir 的子路径。
        """
        base = (Path(local_dir) / path.lstrip("/")) if path else Path(local_dir)
        if not base.exists() or not base.is_dir():
            return []
        entries: List[TreeEntry] = []
        try:
            children = sorted(os.scandir(base), key=lambda e: e.name.lower())
        except (PermissionError, OSError):
            return []
        for child in children:
            rel = f"{path.rstrip('/')}/{child.name}" if path else child.name
            is_dir = child.is_dir()
            try:
                st = child.stat()
                size = st.st_size if not is_dir else None
                mtime = st.st_mtime
            except OSError:
                size, mtime = None, None
            entries.append(TreeEntry(
                name=child.name,
                path=rel,
                type="dir" if is_dir else "file",
                size=size,
                mtime=mtime,
                has_children=is_dir,
            ))
        dirs = sorted([e for e in entries if e.type == "dir"], key=lambda x: x.name.lower())
        files = sorted([e for e in entries if e.type == "file"], key=lambda x: x.name.lower())
        return dirs + files

    def get_commits(self, repo_id: str, branch: str, path: str = "",
                    limit: int = 50) -> List[Commit]:
        """返回某路径的提交历史（Cookie 模式走 REST，本地克隆走 git log）。"""
        if self.config.mode == "pat" and (REPOS_DIR / str(repo_id)).exists():
            return self.get_local_commits(repo_id, branch, path, limit)
        if not self.config.cookie:
            return []
        branch = self._resolve_branch(repo_id, branch)
        if not branch:
            return []
        out: List[Commit] = []
        start_at = 0
        while len(out) < limit:
            url = (f"{self.config.jira_url.rstrip('/')}/rest/git/1.0/repositories/"
                   f"{repo_id}/commits?branch={branch}&startAt={start_at}&limit=50")
            if path:
                url += "&path=" + path.lstrip("/")
            r = self.http_get(url, headers=self.cookie_headers())
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            items = data.get("values") or []
            if not items:
                break
            for it in items:
                out.append(self._parse_commit(it))
                if len(out) >= limit:
                    break
            if not data.get("isLastPage", True):
                start_at = data.get("nextStart", start_at + 50)
            else:
                break
        return out

    def get_local_commits(self, repo_id: str, branch: str, path: str = "",
                          limit: int = 50) -> List[Commit]:
        """本地克隆模式：用 git log 获取提交历史。"""
        local_path = REPOS_DIR / str(repo_id)
        if not local_path.exists():
            return []
        ref = branch or "HEAD"
        try:
            import subprocess
            fmt = "%H%x1f%an%x1f%ad%x1f%s"
            cmd = ["git", "-C", str(local_path), "log",
                   f"--max-count={limit}", f"--date=short", f"--pretty=format:{fmt}"]
            if path:
                cmd += ["--", path.lstrip("/")]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return []
        except Exception:
            return []
        out: List[Commit] = []
        for ln in res.stdout.splitlines():
            parts = ln.split("\x1f")
            if len(parts) != 4:
                continue
            out.append(Commit(
                commit_id=parts[0], author=parts[1], date=parts[2], message=parts[3]))
        return out

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
        """REST 提交对象 -> Commit。"""
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
