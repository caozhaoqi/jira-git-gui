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
            ok, msg, _ = self.clone_repo(
                self.repo_id, self.repo_name, "", self.config.pat,
                self.config.username)
            result["patTest"] = {"ok": ok, "msg": msg}
        return result

    # ----------------------------------------------------- 仓库发现（Cookie）
    def discover_repos(self) -> List[RepoInfo]:
        """发现全部仓库（Cookie 模式）。

        优先解析 GIJRepositoryBrowser-AllRepositories.jspa 页面（用户指定的入口），
        该页面列出所有仓库并带 repoId 链接；若页面解析为空，再回退到 REST 接口。
        """
        out: List[RepoInfo] = []
        if not self.config.cookie:
            return out
        # 1) 优先：AllRepositories 页面解析
        try:
            url = self.config.jira_url.rstrip("/") + ALL_REPOS_PAGE
            r = self.http_get(url, headers=self.cookie_headers())
            if r.status_code == 200 and "login" not in str(r.url):
                out = self._parse_repo_list(r.text)
        except Exception:
            pass
        # 2) 兜底：REST 接口
        if not out:
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
            raise RuntimeError("Cookie 模式未配置会话")
        if not self.repo_id:
            raise RuntimeError("缺少 repoId，请先在连接或仓库面板中指定")
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
        则按候选列表试探，取第一个能返回 ns.data 的分支。
        """
        if branch and self._browse_has_tree(repo_id, branch):
            return branch
        for cand in self._BRANCH_CANDIDATES:
            if cand == branch:
                continue
            if self._browse_has_tree(repo_id, cand):
                return cand
        return branch  # 回退给上层报错，而不是静默空树


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
        branch = self._resolve_branch(self.repo_id, self.branch)
        self.branch = branch
        r = self._fetch_browse(self.repo_id, branch, "")
        info = self._parse_repo_info(r.text)
        head = info.get("headCommit")
        if not head:
            return None, "无法获取分支 HEAD commit"
        ok, content, note = self._cookie_file_content(self.repo_id, head, path)
        return (content, None) if ok else (None, note)

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
            acct = self.b64_prefix_account(pat) or username
            tried = "完整 PAT" + (" + 内嵌密钥" if secret else "")
            diag = ("认证被服务器拒绝（凭据无效，或该账号无此仓库克隆权限）。\n"
                    f"（已分别用「{tried}」两种方式尝试验证，均被拒绝。）\n"
                    "请确认：\n"
                    "  ① PAT 有效且未过期 / 未吊销；\n"
                    f"  ② 该 PAT 所属账号（{acct}）对仓库 {repo_id}/{repo_name} 有浏览/克隆权限；\n"
                    "  ③ 必要时在 Jira 重新生成 PAT（克隆范围）。\n"
                    "可先用终端手动验证，以排除是 GUI 问题：\n"
                    f"  git clone https://{acct}:<PAT>@{host}/git/{repo_id}/{repo_name}.git")
            return False, diag, None
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
        branch = self._resolve_branch(self.repo_id, self.branch)
        self.branch = branch
        r = self._fetch_browse(self.repo_id, branch, "")
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

    # --------------------------------------------------- Cookie 递归下载整库
    def download_repo(self, repo_id: str, branch: str, dest_root: Optional[str] = None,
                      on_log: Optional[Callable[[str], None]] = None) -> tuple:
        """Cookie 模式：递归遍历整棵文件树并下载所有文件，保持目录结构。

        返回 (ok_count, fail_list, dest)。适用于没有 PAT 克隆权限时，
        用会话 Cookie 把仓库“整棵”抓回本地。
        """
        def log(m):
            if on_log:
                on_log(m)

        if not self.config.cookie:
            return 0, [{"path": "(root)", "reason": "Cookie 未配置"}], None

        branch = self._resolve_branch(repo_id, branch)
        self.branch = branch
        r0 = self._fetch_browse(repo_id, branch, "")
        info = self._parse_repo_info(r0.text)
        head = info.get("headCommit")
        if not head:
            return 0, [{"path": "(root)", "reason": "无法获取分支 HEAD commit"}], None

        dest_root = Path(dest_root) if dest_root else (DOWNLOAD_DIR / str(repo_id))
        dest_root.mkdir(parents=True, exist_ok=True)

        ok_count = 0
        fail_list = []
        # 用显式栈做 DFS（避免深目录递归爆栈）
        stack = [""]
        while stack:
            cur = stack.pop()
            try:
                entries = self._list_dir(repo_id, branch, cur)
            except Exception as ex:
                fail_list.append({"path": cur or "(root)", "reason": f"列目录失败：{ex}"})
                continue
            for e in entries:
                if e.type == "dir":
                    stack.append(e.path)
                    continue
                ok, content, note = self._cookie_file_content(repo_id, head, e.path)
                if ok and content is not None:
                    target = dest_root / e.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    ok_count += 1
                    if ok_count % 25 == 0:
                        log(f"已下载 {ok_count} 个文件…")
                else:
                    fail_list.append({"path": e.path, "reason": note})
        log(f"递归下载完成：成功 {ok_count} 个，失败 {len(fail_list)} 个。")
        return ok_count, fail_list, str(dest_root)

    # ------------------------------------------------------ 底层抓取与解析
    def _fetch_browse(self, repo_id: str, branch: str = "", path: str = "") -> httpx.Response:
        url = (f"{self.config.jira_url.rstrip('/')}/secure/GIJBrowseGit.jspa"
               f"?repoId={repo_id}&branchName={urllib.parse.quote(branch)}"
               f"&tagName=&commitId=&path={urllib.parse.quote(path)}")
        return self.http_get(url, headers=self.cookie_headers())

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

    def _local_file_read(self, root: Path, path: str) -> str:
        p = root / path
        if not str(p.resolve()).startswith(str(root.resolve())):
            raise ValueError("非法路径")
        return p.read_text(encoding="utf-8", errors="replace")

    def _cookie_file_content(self, repo_id: str, head_commit: str, path: str) -> tuple:
        """返回 (ok, content, note)。

        依次尝试：commit SHA -> 分支名 作为引用；REST 裸接口 -> JSP 查看页。
        根目录与嵌套子目录文件均可（插件接口本身支持任意 path，
        旧版“仅根目录”限制已移除）。
        """
        # 引用优先级：HEAD commit SHA 优先，失败再用分支名（某些仓库 lastCommit 取不到）
        refs = []
        if head_commit:
            refs.append(head_commit)
        if self.branch and self.branch not in refs:
            refs.append(self.branch)
        if not refs:
            return False, None, "缺少可用的引用（commit/分支），无法定位文件"

        # 路径保留字面斜杠（插件接口以 / 划分目录层级，quote 会把 / 变成 %2F 导致 404）
        qpath = urllib.parse.quote(path, safe="/")
        for ref in refs:
            # 1) REST 裸接口（文本文件，含嵌套路径）
            rest = (f"{self.config.jira_url.rstrip('/')}/rest/gitplugin/1.0/files/"
                    f"{repo_id}/{urllib.parse.quote(ref)}/{qpath}")
            r = self.http_get(rest, headers=self.cookie_headers())
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" not in ct.lower() and \
                    not r.text.lstrip().startswith(("{", "[")):
                return True, r.text, ""
            # 2) JSP 查看页（含 .json 等被当二进制的文件）
            jsp = (f"{self.config.jira_url.rstrip('/')}/secure/GIJViewGitFileContent.jspa"
                   f"?revision={urllib.parse.quote(ref)}&repoId={repo_id}"
                   f"&path={qpath}")
            r2 = self.http_get(jsp, headers=self.cookie_headers())
            if r2.status_code == 200:
                from_html = self._extract_code_from_html(r2.text)
                if from_html is not None:
                    return True, from_html, ""
        # 全失败：给出可诊断信息
        return (False, None,
                f"无法获取文件（已尝试引用 {refs}；REST/JSP 均未返回可用正文）。"
                f"该文件可能需用 PAT 模式克隆后下载。")

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
