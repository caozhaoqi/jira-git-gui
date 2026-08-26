# -*- coding: utf-8 -*-
"""JiraGitClient 的「浏览 / 树 / 提交」Mixin。

拆分自 ``core/client.py``。负责：文件层级浏览（list_level）、分支探测与解析、
本地克隆下的替代浏览路径，以及 commit 历史获取与解析。

共享常量在此重新定义（与 ``core/client.py`` 顶部一致），避免与聚合主类形成循环 import。
"""
import json
import re
from pathlib import Path
from typing import List, Optional

from core.constants import REPOS_DIR
from core.models import TreeEntry, Commit, CommitFile
from core.errors import UserError

# 分支解析正则
_RE_BRANCH_IN_PAGE = re.compile(r'<input[^>]+name="branchName"[^>]+value="([^"]*)"', re.I)
_RE_HIDDEN_BRANCH = re.compile(
    r'<input[^>]+type="hidden"[^>]+name="branchName"[^>]+value="([^"]*)"', re.I)
_RE_BRANCH_IN_JSON = re.compile(r'"branchName"\s*:\s*"([^"]*)"')
_RE_BRANCH_OPTION = re.compile(r'<option[^>]+value="([^"]*)"[^>]*>\s*([^<]*)\s*</option>', re.I)
_RE_VIEW_BRANCH = re.compile(r'[?&]branchName=([^&\s"\'<>]+)')

# 树判断正则
_TREE_PRESENT_RE = re.compile(r'id="[^"]*treeTable|class="[^"]*tree', re.I)
_TREE_LINK_RE = re.compile(
    r'<a\b[^>]*href="([^"]*?GIJ[^"]*?)(?<![?&])branchName=([^&\s"\'<>]+)[^"]*"[^>]*>(.*?)</a>',
    re.I | re.S)
_FILE_LINK_RE = re.compile(
    r'<a\b[^>]*href="([^"]*?files/[^"]*?branchName=([^&\s"\'<>]+)[^"]*?filePath=([^&\s"\'<>]+)[^"]*)"[^>]*>(.*?)</a>',
    re.I | re.S)
_DIR_RE = re.compile(r'''<a\b[^>]*href="([^"]*)"[^>]*>\s*([^<]*)</a>''', re.I)


class BrowseMixin:
    """文件浏览 / 分支解析 / 提交历史能力。"""

    def list_level(self, repo_id: str, branch: str, path: str) -> List[TreeEntry]:
        """返回某仓库某分支某路径下的条目列表（目录优先、再文件，均按名称排序）。"""
        if self.config.mode == "pat" and (REPOS_DIR / str(repo_id)).exists():
            entries = self._list_level_local(repo_id, branch, path)
        else:
            branch = self._resolve_branch(repo_id, branch)
            entries = self._list_level_cookie(repo_id, branch, path)
        dirs = sorted([e for e in entries if e.type == "dir"], key=lambda x: x.name.lower())
        files = sorted([e for e in entries if e.type == "file"], key=lambda x: x.name.lower())
        return dirs + files

    @staticmethod
    def _is_login_page(html_text: str) -> bool:
        """判断页面是否为登录页（需重新登录）。"""
        return ("login" in (html_text or "")[:500].lower()
                or "j_security_check" in (html_text or "").lower()
                or "os_password" in (html_text or "").lower())

    @staticmethod
    def _browse_has_tree(html_text: str) -> bool:
        """判断浏览页是否含文件树（用于区分「仓库存在但无文件」与「页面被拦截」）。"""
        return bool(_TREE_PRESENT_RE.search(html_text or "")
                    or _TREE_LINK_RE.search(html_text or ""))

    def _resolve_branch(self, repo_id: str, branch: str) -> str:
        """解析实际浏览用的分支：优先用显式 branch；否则从默认分支缓存 / 浏览页隐式获取。

        结果缓存到 ``self._branch_cache``（按 repo 缓存），避免每个目录层级都重复探测。
        """
        if branch:
            return branch
        if repo_id in self._branch_cache:
            return self._branch_cache[repo_id]
        resolved = ""
        try:
            if self.config.cookie:
                url = (f"{self.config.jira_url.rstrip('/')}/secure/"
                       f"GIJRepositoryBrowser.jspa?repoId={repo_id}")
                r = self.http_get(url, headers=self.cookie_headers())
                if r.status_code == 200 and not self._is_login_page(r.text):
                    m = _RE_BRANCH_IN_PAGE.search(r.text) or _RE_HIDDEN_BRANCH.search(r.text)
                    if m:
                        resolved = m.group(1)
                    else:
                        mj = _RE_BRANCH_IN_JSON.search(r.text)
                        if mj:
                            resolved = mj.group(1)
                    if resolved:
                        self._branch_cache[repo_id] = resolved
                        return resolved
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
                            return resolved
                except Exception:
                    pass
        except Exception:
            pass
        return resolved

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
        self._head_cache[key] = head
        return head

    def _list_level_cookie(self, repo_id: str, branch: str, path: str) -> List[TreeEntry]:
        """Cookie 模式：解析浏览器文件树页面，返回某层级条目。"""
        if not self.config.cookie:
            return []
        branch = self._resolve_branch(repo_id, branch)
        if not branch:
            return []
        url = (f"{self.config.jira_url.rstrip('/')}/secure/GIJRepositoryBrowser.jspa"
               f"?repoId={repo_id}&branchName={branch}")
        if path:
            url += "&filePath=" + path.lstrip("/")
        r = self.http_get(url, headers=self.cookie_headers())
        if r.status_code != 200 or self._is_login_page(r.text):
            return []
        html_text = r.text

        dirs: List[TreeEntry] = []
        files: List[TreeEntry] = []
        seen = set()

        # 目录链接（带树结构）
        for m in _TREE_LINK_RE.finditer(html_text):
            href, br, label = m.group(1), m.group(2), m.group(3)
            label = re.sub(r"<[^>]+>", "", label).strip()
            if not label or label in seen:
                continue
            if "branchName=" not in href and br:
                href += ("&" if "?" in href else "?") + "branchName=" + br
            cleaned = re.sub(r"<[^>]+>", "", href)
            dirs.append(TreeEntry(name=label, path=path, type="dir", href=cleaned))
            seen.add(label)

        # 文件链接
        for m in _FILE_LINK_RE.finditer(html_text):
            href, br, fpath, label = m.group(1), m.group(2), m.group(3), m.group(4)
            label = re.sub(r"<[^>]+>", "", label).strip()
            if not label or label in seen:
                continue
            file_path = fpath
            cleaned = re.sub(r"<[^>]+>", "", href)
            files.append(TreeEntry(
                name=label, path=file_path, type="file", href=cleaned))
            seen.add(label)

        return dirs + files

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
