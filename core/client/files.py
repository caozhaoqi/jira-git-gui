# -*- coding: utf-8 -*-
"""JiraGitClient 的「文件 / 批量下载」Mixin。

拆分自 ``core/client.py``。负责：单文件/历史版本读取（Cookie 与本地克隆两路）、
Cookie 模式下的有界并发批量下载与整库递归下载（支持断点续传、进度/取消回调），
以及底层文件页抓取与解析辅助。

共享常量在此重新定义（与 ``core/client.py`` 顶部一致），避免与聚合主类形成循环 import。
"""
import base64
import json
import re
import subprocess
import urllib.parse
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional

import httpx

from core.constants import REPOS_DIR, DOWNLOAD_DIR, DEFAULT_DOWNLOAD_WORKERS
from core.errors import UserError

# 文件页解析正则
_RE_FILE_BALANCED = re.compile(
    r'<script[^>]*id="git-file-content-json"[^>]*>(.*?)</script>', re.S | re.I)
_RE_NESTED_JSON = re.compile(r'[{(]\s*"[^"]+"\s*:', re.S)
_RE_CODE_BLOCK = re.compile(r'<code[^>]*class="[^"]*bbb-gp-diff_code-cell-content[^"]*"[^>]*>(.*?)</code>', re.S | re.I)
_RE_VIEW_REPO_INFO = re.compile(r'var\s+repository\s*=\s*\{', re.S)
_RE_REPO_INFO_ID = re.compile(r'"id"\s*:\s*"?(\d+)', re.S)
_RE_REPO_INFO_NAME = re.compile(r'"displayName"\s*:\s*"([^"]*)"', re.S)
_RE_REPO_INFO_URL = re.compile(r'"gkRepoUrl"\s*:\s*"([^"]*)"', re.S)
_RE_TREE_FILE_ROW = re.compile(
    r'<a\b[^>]*href="([^"]*?(?:files|tree)[^"]*?filePath=([^&\s"\'<>]+)[^"]*)"', re.S | re.I)
_RE_TREE_FILE_NAME = re.compile(r'<td[^>]*class="[^"]*name[^"]*"[^>]*>(.*?)</td>', re.S | re.I)
_FILE_BROWSE_ERROR_RE = re.compile(
    r'not\s+be\s+displayed|too\s+large|binary|image|unable\s+to\s+load', re.I)


class FilesMixin:
    """文件读取 / 批量下载能力。"""

    def get_file(self, path: str, allow_binary: bool = False) -> tuple:
        """返回 (content, error)。content 为 None 时 error 有值。

        - 预览场景（默认 ``allow_binary=False``）：二进制文件（图片/压缩包等）在 Cookie
          模式下无法预览，返回提示，引导用户用「下载选中」保存到本地查看。
        - 合并场景（``allow_binary=True``）：需要把远端字节原样写回本地，故二进制内容
          也要返回（不再以「无法预览」为由拒绝）——这是 git 风格同步能合并不透明文件的前提。
        """
        if self.config.mode == "pat" and (REPOS_DIR / str(self.repo_id)).exists():
            try:
                content = self._local_file_read(REPOS_DIR / str(self.repo_id), path)
                if isinstance(content, (bytes, bytearray)):
                    if allow_binary:
                        return content, None
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
            if allow_binary:
                return content, None
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
            if should_cancel and should_cancel():
                return None
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
                        if should_cancel and should_cancel():
                            try:
                                ex.shutdown(wait=False, cancel_futures=True)
                            except Exception:
                                pass
                        try:
                            res = fut.result()
                        except CancelledError:
                            continue
                        if res is None:
                            continue
                        path, status, payload = res
                        if status == "ok":
                            ok_count += 1
                            ok_paths.append(path)
                            if manifest is not None:
                                manifest[path] = payload
                        else:
                            fail_list.append({"path": path, "reason": payload})
                        done += 1
                        if on_progress:
                            on_progress(done, total, path)
                finally:
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
        克隆权限时，用会话 Cookie 把仓库"整棵"抓回本地；中途中断后再次点击
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
    def _fetch_browse(self, repo_id: str, branch: str = "", path: str = "") -> Optional[httpx.Response]:
        """抓取一次文件浏览页，返回首个 200 且非登录页的响应。

        旧实现只用 ``GIJFileBrowser.jspa.json`` 单端点，且调用方固定传 ``branch=""``，
        导致文件接口不带 ``branchName`` → 本实例返回 404/跳登录，「文件树能展开但点开文件
        报『文件浏览页请求失败』」。这里：
          - 自动补 ``branchName``（分支为空时复用 :meth:`_resolve_branch` 的缓存解析）；
          - 依次尝试多个候选端点（``.json`` 结构化优先，回退 ``.jspa``），首个可用即返回；
          - 3xx 重定向到登录页 / 登录页本身会被跳过，全部失败时返回最后一个响应，
            便于调用方据状态码给出清晰报错；无 Cookie 时返回 None。
        """
        if not self.config.cookie:
            return None
        base = self.config.jira_url.rstrip("/")
        # 分支兜底：调用方常传空分支，这里解析一次（复用缓存，避免每次重探）。
        branch = branch or self._resolve_branch(repo_id, self.branch) or ""
        # 候选端点（按优先度排列）
        candidates = [
            f"{base}/secure/GIJFileBrowser.jspa.json?repoId={repo_id}",
            f"{base}/secure/GIJFileBrowser.jspa?repoId={repo_id}",
        ]
        if branch:
            candidates = [c + f"&branchName={branch}" for c in candidates]
        if path:
            candidates = [c + "&filePath=" + path.lstrip("/") for c in candidates]
        last: Optional[httpx.Response] = None
        for url in candidates:
            try:
                r = self.http_get(url, headers=self.cookie_headers())
            except Exception:
                continue
            # 3xx 重定向到登录页 / 登录页本身 → 跳过，尝试下一端点
            if r.status_code == 200 and not self._is_login_page(r.text):
                return r
            last = r
        return last

    def _fetch_content(self, repo_id: str, ref: str, path: str):
        """抓取单文件内容页，返回 ``(response, used_view)``。

        - ``used_view=True``：命中 ``GIJViewGitFileContent.jspa?revision=<ref>``（指定提交，
          该实例上可用；而 ``GIJFileBrowser.jspa.json`` 在本实例返回 404）。这正是浏览器里
          「查看文件」能预览的原因——它带上了具体的 ``revision``（commit SHA）。
        - ``used_view=False``：回退到 :meth:`_fetch_browse`（``GIJFileBrowser``，自动补
          branchName + filePath）。

        优先用 revision 端点：最贴合「某次提交的某文件」语义，且本实例实测可用；
        revision 端点不可用（无 ref / 非 200 / 登录页）再回退 browse 端点。
        """
        if not self.config.cookie:
            return None, False
        base = self.config.jira_url.rstrip("/")
        if ref:
            url = (f"{base}/secure/GIJViewGitFileContent.jspa"
                   f"?revision={ref}&repoId={repo_id}&path={path.lstrip('/')}")
            try:
                r = self.http_get(url, headers=self.cookie_headers())
            except Exception:
                r = None
            if r is not None and r.status_code == 200 and not self._is_login_page(r.text):
                return r, True
        # 回退：原 browse 端点（多候选 + 跳过登录页）
        return self._fetch_browse(repo_id, "", path), False

    def _fetch_raw_file(self, repo_id: str, ref: str, path: str):
        """绕过 web viewer 的大小限制，用插件 REST 原始文件端点直接取字节。

        仅当 web viewer 内嵌失败（文件过大/二进制）时作为兜底。接受「原始字节」响应
        （``text/plain`` / ``application/octet-stream`` / 未知类型），或 JSON 包内含
        ``content``/``rawFile``（base64 或文本）；其余（HTML/JSON 错误包）一律拒绝，
        绝不把错误页当文件内容写坏。返回 bytes/str 或 None。
        """
        if not ref or not self.config.cookie:
            return None
        base = self.config.jira_url.rstrip("/")
        safe_path = path.lstrip("/")
        candidates = [
            f"{base}/rest/git/1.0/repositories/{repo_id}/files/{ref}?path={safe_path}",
            f"{base}/rest/gitplugin/1.0/repository/{repo_id}/files/{ref}?path={safe_path}",
        ]
        for url in candidates:
            try:
                r = self.http_get(url, headers=self.cookie_headers())
            except Exception:
                continue
            if r.status_code != 200:
                continue
            ct = (r.headers.get("Content-Type") or "").lower()
            body = r.content
            if "html" in ct:
                continue  # 仍是 viewer / 登录 / 错误页
            if "json" in ct:
                try:
                    j = r.json()
                except Exception:
                    j = None
                if isinstance(j, dict):
                    c = j.get("content") or j.get("rawFile") or j.get("fileContent")
                    if isinstance(c, str):
                        try:
                            return base64.b64decode(c, validate=True)
                        except Exception:
                            return c
                continue
            # text/plain / octet-stream / 未知类型 → 原始内容
            if b"\x00" in body[:4096]:
                return body
            try:
                return body.decode("utf-8")
            except UnicodeDecodeError:
                return body
        return None


    @staticmethod
    def _extract_balanced_json(html_text: str) -> Optional[dict]:
        """从 <script id="git-file-content-json">…</script> 稳健提取 JSON 对象。

        该脚本块可能包含 HTML 实体转义（&quot; &lt; &gt; &amp;）、被截断或含嵌套引号，
        直接 ``json.loads`` 易失败。此处用「大括号平衡扫描」在原始 HTML 中切出最外层
        ``{...}``，再做 HTML 反转义与字符修复（替换 &quot;→"、&lt;→<、&gt;→>、
        &amp;→&，避免把 ``&amp;`` 误转成 ``&`` 破坏 URL），最后 ``json.loads``。
        """
        m = _RE_FILE_BALANCED.search(html_text or "")
        if not m:
            return None
        raw = m.group(1)
        start = raw.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(raw)):
            ch = raw[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return None
        blob = raw[start:end]
        blob = (blob.replace("&quot;", '"')
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&amp;", "&"))
        try:
            return json.loads(blob)
        except Exception:
            return None

    def _parse_repo_info(self, html_text: str) -> Optional[dict]:
        """从文件浏览页解析仓库基本信息（id / displayName / cloneUrl）。"""
        if not html_text or not self._strip_repo_info(html_text):
            return None
        info: dict = {}
        m = _RE_REPO_INFO_ID.search(html_text)
        if m:
            info["repo_id"] = m.group(1)
        m = _RE_REPO_INFO_NAME.search(html_text)
        if m:
            info["displayName"] = m.group(1)
        m = _RE_REPO_INFO_URL.search(html_text)
        if m:
            info["cloneUrl"] = m.group(1)
        if "repo_id" not in info:
            return None
        return info

    @staticmethod
    def _strip_repo_info(html_text: str) -> bool:
        return bool(_RE_VIEW_REPO_INFO.search(html_text or ""))

    def _parse_tree_files(self, html_text: str) -> List[dict]:
        """解析文件树页面，返回 [{'path','name','type'}] 列表。"""
        out: List[dict] = []
        for m in _TREE_FILE_ROW.finditer(html_text or ""):
            href, fpath, _ = m.group(1), m.group(2), m.group(3)
            name_m = _TREE_FILE_NAME.search(href + html_text[m.end(): m.end() + 400])
            name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip() if name_m else fpath
            typ = "file" if "filePath=" in href else "dir"
            out.append({"path": urllib.parse.unquote(fpath), "name": name, "type": typ})
        return out

    def _local_file_read(self, local_path: Path, path: str):
        """读取本地克隆中的文件内容（git show HEAD:<path>），返回文本或字节。"""
        try:
            res = subprocess.run(
                [self._git_bin, "-C", str(local_path), "show", f"HEAD:{path}"],
                capture_output=True, timeout=30)
            if res.returncode != 0:
                return None
            data = res.stdout
            if self._is_likely_text(data, ""):
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    return data
            return data
        except Exception:
            return None

    def _cookie_file_content(self, repo_id: str, ref: str, path: str,
                             client=None) -> tuple:
        """Cookie 模式取单文件内容：(ok, content, note)。

        - ok=True 时 content 为 str（文本）或 bytes（二进制），note=""
        - ok=False 时 content=None，note 为失败原因

        优先按 ``revision=<ref>`` 打 ``GIJViewGitFileContent.jspa``（浏览器「查看文件」同款
        端点，本实例实测可用）；ref 缺失或该端点不可用时回退 ``GIJFileBrowser``。
        """
        r, used_view = self._fetch_content(repo_id, ref, path)
        if r is None:
            return False, None, "未配置 Cookie，无法访问远端文件"
        if r.status_code != 200:
            # 3xx 重定向到登录页 / 401 / 403 多半是 Cookie 过期或会话失效；
            # 404 通常是 Jira 侧文件浏览端点不可用（此类实例远端预览本就不可用）。
            if r.status_code in (301, 302, 303, 307, 308, 401, 403):
                return False, None, ("远端文件浏览被拒绝（HTTP %s），"
                                      "多半是 Cookie 已过期或会话失效，请在连接设置更新 Cookie"
                                      % r.status_code)
            return False, None, ("远端文件浏览端点不可用（HTTP %s），"
                                  "该 Jira 实例的文件接口可能未启用；可改用 PAT 模式克隆到本地后预览"
                                  % r.status_code)
        html_text = r.text
        if self._looks_like_error_envelope(html_text):
            return False, None, "文件接口返回错误包（可能无权限或文件不存在）"
        # 1) 优先解析结构化内容（<script id="git-file-content-json">）。
        data = self._extract_balanced_json(html_text)
        if data:
            raw = data.get("rawFile") or data.get("content") or data.get("fileContent")
            if raw is None and isinstance(data.get("file"), dict):
                raw = data["file"].get("content")
            if raw is None:
                return False, None, "文件内容字段缺失"
            content = raw
            ct = data.get("contentType") or data.get("mimeType") or ""
            if isinstance(content, str):
                if self._is_likely_text(content.encode("utf-8", "replace"), ct):
                    return True, content, ""
                return True, content.encode("utf-8", "replace"), ""
            return True, content, ""
        # 2) 若是 GIJViewGitFileContent 渲染页，用 <pre>/<code> 兜底取正文。
        if used_view:
            code = self._extract_code_from_html(html_text)
            if code and code.strip():
                return True, code, ""
        # 3) 提取全部失败 → 再跑诊断正则给出具体原因。
        # ⚠️ ``_FILE_BROWSE_ERROR_RE`` 含 "image" 等宽泛词，正常 viewer 页（带图片预览图标/
        # "image/*" 等）也会误中，所以必须放到提取失败之后才检查。提取成功就提前 return，
        # 根本不会跑到这里——这是修掉「点 .py 也报文件过大或为二进制」误报的关键。
        if self._is_likely_text(html_text.encode("utf-8", "replace"), "text/html"):
            if _FILE_BROWSE_ERROR_RE.search(html_text):
                # web viewer 不内嵌内容（文件过大/二进制）：尝试插件 REST 原始文件端点兜底，
                # 绕过 viewer 的大小限制直接取字节。任一端点返回原始内容即采用。
                raw = self._fetch_raw_file(repo_id, ref, path)
                if raw is not None:
                    return True, raw, ""
                return False, None, ("远端文件过大或为二进制，Cookie 模式的 web 预览无法获取其内容；"
                                     "请改用本地克隆/PAT 后合并，或在文件树用「下载选中」保存到本地")
        return False, None, "未能从文件页解析出结构化内容"

    @staticmethod
    def _looks_like_error_envelope(html_text: str) -> bool:
        """判断是否命中插件错误包（权限/不存在/限流）。"""
        b = (html_text or "").lower()
        head = b[:2000]
        return bool(
            '"success": false' in head
            or '"errorcode"' in head
            or '"errormessage"' in head
            or '{"error"' in head)

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
            if not data:
                return True
            if b"\x00" in data[:4096]:
                return False
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                return False
            return True
        _BINARY_MIME_PREFIXES = (
            "application/octet-stream", "application/vnd.", "application/zip",
            "application/gzip", "application/x-tar", "image/", "audio/", "video/",
            "application/pdf", "application/msword", "application/mspowerpoint",
            "application/msexcel", "application/x-ms",
        )
        if ct.startswith(_BINARY_MIME_PREFIXES) or "officedocument" in ct:
            return False
        if (ct.startswith("text/")
                or ct in ("application/json", "application/xml", "application/javascript",
                          "application/ecmascript", "application/html")
                or ct.endswith("+xml")):
            return True
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
            t = t.replace("\u00a0", " ")
            return t

        rows = _RE_CODE_BLOCK.findall(html_text)
        if not rows:
            rows = re.findall(r'<code[^>]*>(.*?)</code>', html_text, re.S)
        if rows:
            out = [clean(r).rstrip("\r") for r in rows]
            joined = "\n".join(out)
            return joined + ("\n" if joined else "")

        pres = re.findall(r'<pre[^>]*>(.*?)</pre>', html_text, re.S)
        if pres:
            out = [clean(p).rstrip("\r") for p in pres]
            joined = "\n".join(out)
            return joined + ("\n" if joined else "")

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
