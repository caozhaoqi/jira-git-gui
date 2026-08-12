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
import time
import urllib.parse
from pathlib import Path
from typing import Callable, List, Optional

import httpx

from .constants import PROXY_URL, HTTP_TIMEOUT, REPOS_DIR, DOWNLOAD_DIR
from .models import ConnectConfig, RepoInfo, TreeEntry


class JiraGitClient:
    def __init__(self) -> None:
        self.config = ConnectConfig()
        self.repo_id: str = ""
        self.repo_name: str = ""
        self.branch: str = ""
        self._git_bin = shutil.which("git") or "git"

    # ------------------------------------------------------------------ 配置
    def set_config(self, cfg: ConnectConfig) -> None:
        self.config = cfg

    def set_repo(self, repo_id: str, repo_name: str = "", branch: str = "") -> None:
        self.repo_id = str(repo_id)
        if repo_name:
            self.repo_name = repo_name
        if branch:
            self.branch = branch

    @staticmethod
    def host_of(url: str) -> str:
        u = (url or "").strip().rstrip("/")
        m = re.match(r"https?://([^/]+)", u)
        return m.group(1) if m else u

    # ------------------------------------------------------------------ 网络
    @staticmethod
    def http_get(url: str, headers: Optional[dict] = None, retries: int = 5) -> httpx.Response:
        """带重试的 GET：每次请求新建客户端（无连接池复用），专门对抗代理偶发的
        SSL UNEXPECTED_EOF / 连接重置；失败自动退避重试。"""
        last = None
        for attempt in range(retries):
            try:
                with httpx.Client(
                    timeout=HTTP_TIMEOUT,
                    follow_redirects=True,
                    proxy=PROXY_URL or None,
                    verify=False,
                    headers={"User-Agent": "jira-git-gui/1.0"},
                ) as client:
                    return client.get(url, headers=headers or {})
            except (httpx.TransportError, httpx.HTTPError) as e:
                last = e
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
            ok, msg, _ = self.clone_repo(
                self.repo_id, self.repo_name, "", self.config.pat,
                self.config.username)
            result["patTest"] = {"ok": ok, "msg": msg}
        return result

    # ----------------------------------------------------- 仓库发现（Cookie）
    def discover_repos(self) -> List[RepoInfo]:
        out: List[RepoInfo] = []
        if not self.config.cookie:
            return out
        for ep in ("/rest/gitplugin/1.0/repositories", "/rest/git/1.0/repository"):
            try:
                r = self.http_get(self.config.jira_url.rstrip("/") + ep,
                                  headers=self.cookie_headers())
                if r.status_code == 200:
                    try:
                        data = r.json()
                        for it in data:
                            out.append(RepoInfo(
                                repo_id=str(it.get("id") or it.get("repoId") or ""),
                                display_name=it.get("displayName") or it.get("name") or "",
                                clone_url=it.get("cloneUrl") or it.get("url") or "",
                            ))
                    except Exception:
                        pass
                    if out:
                        break
            except Exception:
                pass
        return out

    # ------------------------------------------------------- 文件树（单层）
    def list_level(self, path: str = "") -> List[TreeEntry]:
        """返回 path 目录的【直接子项】（单层，懒加载）。自动路由：
        PAT 模式且本地已克隆 -> 读本地；否则走 Cookie 模式。"""
        if self.config.mode == "pat" and (REPOS_DIR / str(self.repo_id)).exists():
            root = REPOS_DIR / str(self.repo_id)
            return self._list_level_local(root, path)
        if not self.config.cookie:
            raise RuntimeError("Cookie 模式未配置会话")
        if not self.repo_id:
            raise RuntimeError("缺少 repoId，请先在连接或仓库面板中指定")
        return self._list_level_cookie(self.repo_id, self.branch, path)

    def _list_level_cookie(self, repo_id: str, branch: str, path: str = "") -> List[TreeEntry]:
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

    # ------------------------------------------------------------- 文件正文
    def get_file(self, path: str) -> tuple:
        """返回 (content, error)。content 为 None 时 error 有值。"""
        if self.config.mode == "pat" and (REPOS_DIR / str(self.repo_id)).exists():
            try:
                return self._local_file_read(REPOS_DIR / str(self.repo_id), path), None
            except Exception as ex:
                return None, str(ex)
        if not self.config.cookie:
            return None, "Cookie 未配置"
        r = self._fetch_browse(self.repo_id, self.branch, "")
        info = self._parse_repo_info(r.text)
        head = info.get("headCommit")
        if not head:
            return None, "无法获取分支 HEAD commit"
        ok, content, note = self._cookie_file_content(self.repo_id, head, path)
        return (content, None) if ok else (None, note)

    # ----------------------------------------------------------- git 克隆
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

        candidates = [username]
        acct = self.b64_prefix_account(pat)
        if acct and acct != username:
            candidates.append(acct)

        last_err = ""
        for user in candidates:
            clone_url = (f"https://{user}:{self.encode_pat(pat)}@{host}"
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
                last_err = (res.stderr.strip().splitlines()[-1]
                            if res.stderr.strip() else res.stdout.strip())
            except subprocess.TimeoutExpired:
                last_err = "克隆超时"
            except Exception as ex:
                last_err = str(ex)
        return False, f"克隆失败：{last_err}", None

    # ------------------------------------------------------- Cookie 批量下载
    def download(self, paths: List[str],
                 on_log: Optional[Callable[[str], None]] = None) -> tuple:
        """Cookie 模式：批量下载所选文件到 downloads/<repoId>/ 保持目录结构。
        返回 (ok_list, fail_list, dest)。"""
        def log(m):
            if on_log:
                on_log(m)

        if not self.config.cookie:
            return [], [{"path": p, "reason": "Cookie 未配置"} for p in paths], None
        r = self._fetch_browse(self.repo_id, self.branch, "")
        info = self._parse_repo_info(r.text)
        head = info.get("headCommit")
        if not head:
            return [], [{"path": p, "reason": "无法获取分支 HEAD commit"} for p in paths], None

        dest_root = DOWNLOAD_DIR / str(self.repo_id)
        dest_root.mkdir(parents=True, exist_ok=True)
        ok_list, fail_list = [], []
        for p in paths:
            ok, content, note = self._cookie_file_content(self.repo_id, head, p)
            if ok and content is not None:
                target = dest_root / p
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                ok_list.append(p)
                log(f"已下载 {p}")
            else:
                fail_list.append({"path": p, "reason": note})
        return ok_list, fail_list, str(dest_root)

    # ------------------------------------------------------ 底层抓取与解析
    def _fetch_browse(self, repo_id: str, branch: str = "", path: str = "") -> httpx.Response:
        url = (f"{self.config.jira_url.rstrip('/')}/secure/GIJBrowseGit.jspa"
               f"?repoId={repo_id}&branchName={urllib.parse.quote(branch)}"
               f"&tagName=&commitId=&path={urllib.parse.quote(path)}")
        return self.http_get(url, headers=self.cookie_headers())

    def _parse_repo_info(self, html: str) -> dict:
        """从 ns.repoInfo 解析 displayName(仓库名) 与 lastCommit.name(分支 HEAD)。"""
        info: dict = {}
        m = re.search(r'ns\.repoInfo\s*=\s*(\{.*?\});', html, re.S)
        if m:
            try:
                d = json.loads(m.group(1))
                info["displayName"] = d.get("displayName")
                info["repoId"] = d.get("id")
                lc = d.get("lastCommit") or {}
                info["headCommit"] = lc.get("name")
            except Exception:
                pass
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

    def _local_file_read(self, root: Path, path: str) -> str:
        p = root / path
        if not str(p.resolve()).startswith(str(root.resolve())):
            raise ValueError("非法路径")
        return p.read_text(encoding="utf-8", errors="replace")

    def _cookie_file_content(self, repo_id: str, head_commit: str, path: str) -> tuple:
        """返回 (ok, content, note)。root 文本走 REST 裸接口；root .json 走 JSP 提取；
        嵌套文件在 Cookie 模式无解（插件无服务端子目录列表/正文接口）。"""
        is_nested = "/" in path
        if is_nested:
            return False, None, "Cookie 模式不支持嵌套文件（子目录），请用 PAT 模式克隆"
        host = self.host_of(self.config.jira_url)
        # 1) REST 裸接口（root 文本文件）
        rest = (f"{self.config.jira_url.rstrip('/')}/rest/gitplugin/1.0/files/"
                f"{repo_id}/{head_commit}/{path}")
        r = self.http_get(rest, headers=self.cookie_headers())
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" not in ct.lower() and \
                not r.text.lstrip().startswith(("{", "[")):
            return True, r.text, ""
        # 2) JSP 查看页（含 .json 等被当二进制的 root 文件）
        jsp = (f"{self.config.jira_url.rstrip('/')}/secure/GIJViewGitFileContent.jspa"
               f"?revision={head_commit}&repoId={repo_id}&path={urllib.parse.quote(path)}")
        r2 = self.http_get(jsp, headers=self.cookie_headers())
        if r2.status_code == 200:
            from_html = self._extract_code_from_html(r2.text)
            if from_html is not None:
                return True, from_html, ""
        return False, None, f"无法获取（HTTP {r.status_code}/{r2.status_code}）"

    @staticmethod
    def _extract_code_from_html(html_text: str):
        """从 GIJViewGitFileContent 的 <code> 行中提取正文，还原 &nbsp; 为空格、HTML 实体。"""
        import html as html_lib
        rows = re.findall(
            r'<code[^>]*class="[^"]*bbb-gp-diff_code-cell-content[^"]*"[^>]*>(.*?)</code>',
            html_text, re.S)
        if not rows:
            rows = re.findall(r'<code[^>]*>(.*?)</code>', html_text, re.S)
        if not rows:
            return None
        out = []
        for row in rows:
            text = re.sub(r"<[^>]+>", "", row)
            text = html_lib.unescape(text)
            text = text.replace("\u00a0", " ")  # &nbsp; -> 普通空格
            out.append(text.rstrip("\r"))
        return "\n".join(out) + ("\n" if out else "")
