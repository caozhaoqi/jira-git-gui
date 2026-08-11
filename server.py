#!/usr/bin/env python3
"""
Jira Git 插件通用拉取 GUI —— 后端服务
支持两种模式：
  - PAT 模式：用 Personal Access Token 走 git clone，全量拿到（含嵌套文件）
  - Cookie 模式：用 JSESSIONID 会话走 Web 抓取，浏览树 + 下载根目录文件
单进程 FastAPI，同时托管前端 index.html。
"""
import os
import re
import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

BASE_DIR = Path("/Users/caozhaoqi/PycharmProjects/jira-git-gui")
STORE = BASE_DIR / "store"
REPOS_DIR = STORE / "repos"      # git clone 存放地： repos/<repoId>/
DOWNLOAD_DIR = STORE / "downloads"
REPOS_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 内存态（单用户本地工具，足够）
STATE = {
    "jira_url": "",
    "username": "",
    "mode": "pat",        # pat | cookie
    "pat": "",
    "cookie": "",
    "repo_id": "",
    "repo_name": "",
    "branch": "",
}

app = FastAPI()

HTTP_TIMEOUT = 40
# 本地克隆默认不含 .git 的遍历
GIT_BIN = shutil.which("git") or "git"
# 标记：server.py 已在上文定义 HTTP_TIMEOUT / _CLIENT 后提供 http_get

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def host_of(jira_url: str) -> str:
    u = jira_url.strip().rstrip("/")
    m = re.match(r"https?://([^/]+)", u)
    return m.group(1) if m else u

# 代理地址（单请求新建客户端，避免连接池复用被代理掐断的僵尸连接）
_PROXY_URL = ""
for _k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    if os.environ.get(_k):
        _PROXY_URL = os.environ[_k]
        break

def http_get(url: str, headers: dict = None, retries: int = 5) -> httpx.Response:
    """带重试的 GET：每次请求新建客户端（无连接池复用），专门对抗代理偶发的
    SSL UNEXPECTED_EOF / 连接重置；失败自动退避重试。"""
    import time
    last = None
    for attempt in range(retries):
        try:
            with httpx.Client(
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
                proxy=_PROXY_URL or None,
                verify=False,
                headers={"User-Agent": "jira-git-gui/1.0"},
            ) as client:
                return client.get(url, headers=headers or {})
        except (httpx.TransportError, httpx.HTTPError) as e:
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise last if last else httpx.TransportError("unknown httpx error")

def cookie_headers():
    if STATE.get("cookie"):
        return {"Cookie": STATE["cookie"]}
    return {}

def encode_pat(pat: str) -> str:
    # git/HTTP 基础认证里 '/' 会被误解析，统一编码为 %2F
    return pat.replace("/", "%2F")

def b64_prefix_account(pat: str):
    """从 PAT 前缀解出可能的账号 ID（形如 base64('123456789012:s')）"""
    head = pat.split("/", 1)[0]
    try:
        dec = base64.b64decode(head + "==").decode("utf-8", "ignore")
        if ":" in dec:
            return dec.split(":", 1)[0]
    except Exception:
        pass
    return None

import base64

# ----------------------------------------------------------------------------
# Jira Web / REST 抓取（Cookie 模式）
# ----------------------------------------------------------------------------

def fetch_browse(repo_id, branch="", path=""):
    """抓取 GIJBrowseGit.jspa 浏览页 HTML（含 ns.repoInfo / ns.data）"""
    url = (f"{STATE['jira_url'].rstrip('/')}/secure/GIJBrowseGit.jspa"
           f"?repoId={repo_id}&branchName={urllib.parse.quote(branch)}"
           f"&tagName=&commitId=&path={urllib.parse.quote(path)}")
    return http_get(url, headers=cookie_headers())

def parse_repo_info(html: str):
    """从 ns.repoInfo 解析 displayName(仓库名) 与 lastCommit.name(分支 HEAD)"""
    info = {}
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

def parse_tree_files(html: str):
    """从 ns.data.files 解析当前目录条目"""
    files = []
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

def list_level_cookie(repo_id, branch, path=""):
    """Cookie 模式：返回 path 目录的【直接子项】（单层，懒加载）。"""
    r = fetch_browse(repo_id, branch, path)
    entries = parse_tree_files(r.text)
    out = []
    for e in entries:
        p = e["path"]
        is_dir = e["is_dir"]
        out.append({
            "name": p.split("/")[-1] or "(root)",
            "path": p,
            "type": "dir" if is_dir else "file",
            "size": e["size"],
            "has_children": bool(is_dir),
        })
    out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return out

def list_level_local(root: Path, path=""):
    """PAT 模式：从本地克隆目录读单层子项。"""
    full = root / path
    out = []
    try:
        items = sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception:
        return out
    for it in items:
        if it.name == ".git":
            continue
        rel = (path + "/" + it.name).lstrip("/")
        if it.is_dir():
            out.append({"name": it.name, "path": rel, "type": "dir",
                        "size": None, "has_children": True})
        else:
            out.append({"name": it.name, "path": rel, "type": "file",
                        "size": it.stat().st_size, "has_children": False})
    return out

# ----------------------------------------------------------------------------
# git clone（PAT 模式）
# ----------------------------------------------------------------------------

def clone_repo(repo_id, repo_name, branch, pat, username):
    """git clone 到本地，返回 (ok, msg, local_path)。会尝试 username 失败时回退账号ID。"""
    host = host_of(STATE["jira_url"])
    local_path = REPOS_DIR / str(repo_id)
    if local_path.exists():
        # 已克隆则直接 fetch 更新
        try:
            subprocess.run([GIT_BIN, "-C", str(local_path), "fetch", "--all"],
                           capture_output=True, text=True, timeout=120)
            return True, "已存在，已 fetch 更新", str(local_path)
        except Exception as ex:
            return True, f"已存在本地克隆（fetch 跳过：{ex}）", str(local_path)

    candidates = [username]
    acct = b64_prefix_account(pat)
    if acct and acct != username:
        candidates.append(acct)

    last_err = ""
    for user in candidates:
        clone_url = (f"https://{user}:{encode_pat(pat)}@{host}"
                     f"/git/{repo_id}/{repo_name}.git")
        cmd = [GIT_BIN, "-c", "credential.helper=", "clone", "--depth", "1"]
        if branch:
            cmd += ["-b", branch]
        cmd += [clone_url, str(local_path)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode == 0:
                return True, f"克隆成功（用户 {user}）", str(local_path)
            last_err = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else res.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = "克隆超时"
        except Exception as ex:
            last_err = str(ex)
    return False, f"克隆失败：{last_err}", None

def local_tree(root: Path, rel=""):
    node = {"path": rel, "name": rel.split("/")[-1] or "(root)",
            "type": "dir", "children": []}
    full = root / rel
    try:
        items = sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception:
        return node
    for it in items:
        if it.name == ".git":
            continue
        if it.is_dir():
            node["children"].append(local_tree(root, (rel + "/" + it.name).lstrip("/")))
        else:
            node["children"].append({
                "path": (rel + "/" + it.name).lstrip("/"),
                "name": it.name, "type": "file", "size": it.stat().st_size,
            })
    return node

def local_file_read(root: Path, path: str):
    p = root / path
    if not str(p.resolve()).startswith(str(root.resolve())):
        raise HTTPException(400, "非法路径")
    return p.read_text(encoding="utf-8", errors="replace")

# ----------------------------------------------------------------------------
# Cookie 模式文件正文提取
# ----------------------------------------------------------------------------

def cookie_file_content(repo_id, head_commit, path):
    """返回 (ok, content, note)。root 文本走 REST 裸接口；root .json 走 JSP 提取；嵌套不可用。"""
    is_nested = "/" in path
    if is_nested:
        return False, None, "Cookie 模式不支持嵌套文件（子目录），请用 PAT 模式克隆"
    host = host_of(STATE["jira_url"])
    # 1) REST 裸接口（root 文本文件）
    rest = (f"{STATE['jira_url'].rstrip('/')}/rest/gitplugin/1.0/files/"
            f"{repo_id}/{head_commit}/{path}")
    r = http_get(rest, headers=cookie_headers())
    ct = r.headers.get("content-type", "")
    if r.status_code == 200 and "json" not in ct.lower() and not r.text.lstrip().startswith(("{", "[")):
        return True, r.text, ""
    # 2) JSP 查看页（含 .json 等被当二进制的 root 文件）
    jsp = (f"{STATE['jira_url'].rstrip('/')}/secure/GIJViewGitFileContent.jspa"
           f"?revision={head_commit}&repoId={repo_id}&path={urllib.parse.quote(path)}")
    r2 = http_get(jsp, headers=cookie_headers())
    if r2.status_code == 200:
        from_html = extract_code_from_html(r2.text)
        if from_html is not None:
            return True, from_html, ""
    return False, None, f"无法获取（HTTP {r.status_code}/{r2.status_code}）"

def extract_code_from_html(html_text: str):
    """从 GIJViewGitFileContent 的 <code> 行中提取正文，还原 &nbsp; 为空格、HTML 实体。"""
    import html as html_lib
    rows = re.findall(r'<code[^>]*class="[^"]*bbb-gp-diff_code-cell-content[^"]*"[^>]*>(.*?)</code>',
                      html_text, re.S)
    if not rows:
        rows = re.findall(r'<code[^>]*>(.*?)</code>', html_text, re.S)
    if not rows:
        return None
    out = []
    for row in rows:
        # 去掉行内标签，还原实体
        text = re.sub(r"<[^>]+>", "", row)
        text = html_lib.unescape(text)
        # &nbsp; (U+00A0) 还原为普通空格
        text = text.replace("\u00a0", " ")
        out.append(text.rstrip("\r"))
    return "\n".join(out) + ("\n" if out else "")

# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------

@app.get("/")
def index():
    return HTMLResponse(Path(BASE_DIR / "index.html").read_text(encoding="utf-8"))

@app.post("/api/connect")
async def api_connect(req: Request):
    body = await req.json()
    STATE["jira_url"] = body.get("jira_url", "").rstrip("/")
    STATE["username"] = body.get("username", "")
    STATE["mode"] = body.get("mode", "pat")
    STATE["pat"] = body.get("pat", "")
    STATE["cookie"] = body.get("cookie", "")
    if body.get("repo_id"):
        STATE["repo_id"] = body["repo_id"]
    if body.get("branch"):
        STATE["branch"] = body["branch"]
    if body.get("repo_name"):
        STATE["repo_name"] = body["repo_name"]
    result = {"cookieOk": False, "patProvided": bool(STATE["pat"]),
              "repoDefaults": None, "note": ""}
    # 测 cookie：抓一次浏览页（用 repoId 若已给，否则仅探连通）
    if STATE["cookie"]:
        try:
            probe_repo = body.get("repo_id") or STATE.get("repo_id") or ""
            if probe_repo:
                r = fetch_browse(probe_repo, body.get("branch", ""),
                                 body.get("path", ""))
            else:
                # 无 repoId 时探连通：请求根 browse（多半空，但能判断是否 200/登录页）
                r = http_get(f"{STATE['jira_url']}/secure/Dashboard.jspa",
                             headers=cookie_headers())
            if r.status_code == 200 and "login" not in str(r.url) and "dead link" not in r.text.lower():
                result["cookieOk"] = True
                if probe_repo:
                    info = parse_repo_info(r.text)
                    if info:
                        result["repoDefaults"] = info
        except Exception as ex:
            result["note"] = f"cookie 探测异常：{ex}"
    # 测 PAT：尝试 git ls-remote（需要已知仓库；无仓库先标 pending）
    if STATE["pat"] and body.get("repo_id") and body.get("repo_name"):
        ok, msg, _ = clone_repo(body["repo_id"], body["repo_name"], "",
                                STATE["pat"], STATE["username"])
        result["patTest"] = {"ok": ok, "msg": msg}
    return JSONResponse(result)

@app.get("/api/status")
def api_status():
    s = dict(STATE)
    s.pop("pat", None); s.pop("cookie", None)
    s["patSet"] = bool(STATE["pat"]); s["cookieSet"] = bool(STATE["cookie"])
    return JSONResponse(s)

@app.get("/api/repos")
def api_repos():
    """发现仓库：尝试 gitplugin REST；失败则靠 cookie 探测给定 repoId。"""
    out = []
    if STATE["cookie"]:
        # 尝试 REST 列表
        for ep in ("/rest/gitplugin/1.0/repositories",
                   "/rest/git/1.0/repository"):
            try:
                r = http_get(STATE["jira_url"].rstrip("/") + ep,
                             headers=cookie_headers())
                if r.status_code == 200:
                    try:
                        data = r.json()
                        for it in data:
                            out.append({
                                "repoId": it.get("id") or it.get("repoId"),
                                "displayName": it.get("displayName") or it.get("name"),
                                "cloneUrl": it.get("cloneUrl") or it.get("url"),
                            })
                    except Exception:
                        pass
                    if out:
                        break
            except Exception:
                pass
    return JSONResponse({"repos": out, "cookieOk": bool(STATE["cookie"])})

@app.post("/api/clone")
async def api_clone(req: Request):
    body = await req.json()
    repo_id = body.get("repo_id") or STATE["repo_id"]
    repo_name = body.get("repo_name") or STATE["repo_name"]
    branch = body.get("branch") or STATE["branch"]
    STATE["repo_id"] = repo_id; STATE["repo_name"] = repo_name; STATE["branch"] = branch
    if not repo_name and STATE["cookie"]:
        # 用 cookie 探测 displayName
        try:
            r = fetch_browse(repo_id, branch, "")
            info = parse_repo_info(r.text)
            if info.get("displayName"):
                repo_name = info["displayName"]
                STATE["repo_name"] = repo_name
        except Exception:
            pass
    if not repo_name:
        return JSONResponse({"ok": False, "msg": "缺少仓库名(repo_name)，请手动填入或在 Cookie 模式下自动探测"})
    ok, msg, path = clone_repo(repo_id, repo_name, branch, STATE["pat"], STATE["username"])
    return JSONResponse({"ok": ok, "msg": msg, "local_path": path,
                         "repo_name": repo_name})

@app.get("/api/tree")
def api_tree(path: str = ""):
    """返回 path 目录的【单层】子项，供前端懒加载展开。"""
    repo_id = STATE["repo_id"]; branch = STATE["branch"]
    if STATE["mode"] == "pat" and (REPOS_DIR / str(repo_id)).exists():
        root = REPOS_DIR / str(repo_id)
        return JSONResponse({"mode": "pat", "path": path,
                             "entries": list_level_local(root, path)})
    if not STATE["cookie"]:
        return JSONResponse({"mode": "cookie", "error": "Cookie 模式未配置会话"}, status_code=400)
    if not repo_id:
        return JSONResponse({"mode": "cookie", "error": "缺少 repoId"}, status_code=400)
    try:
        entries = list_level_cookie(repo_id, branch, path)
        return JSONResponse({"mode": "cookie", "path": path, "entries": entries})
    except Exception as ex:
        return JSONResponse({"mode": "cookie", "error": str(ex)}, status_code=500)

@app.get("/api/file")
def api_file(path: str):
    repo_id = STATE["repo_id"]; branch = STATE["branch"]
    if STATE["mode"] == "pat" and (REPOS_DIR / str(repo_id)).exists():
        try:
            content = local_file_read(REPOS_DIR / str(repo_id), path)
            return JSONResponse({"mode": "pat", "content": content})
        except Exception as ex:
            return JSONResponse({"mode": "pat", "error": str(ex)}, status_code=400)
    # Cookie 模式
    if not STATE["cookie"]:
        return JSONResponse({"error": "Cookie 未配置"}, status_code=400)
    r = fetch_browse(repo_id, branch, "")
    info = parse_repo_info(r.text)
    head = info.get("headCommit")
    if not head:
        return JSONResponse({"error": "无法获取分支 HEAD commit"}, status_code=400)
    ok, content, note = cookie_file_content(repo_id, head, path)
    if ok:
        return JSONResponse({"mode": "cookie", "content": content})
    return JSONResponse({"error": note}, status_code=400)

@app.post("/api/download")
async def api_download(req: Request):
    """Cookie 模式：批量下载所选文件到 downloads/<repoId>/ 保持目录结构。"""
    body = await req.json()
    paths = body.get("paths", [])
    repo_id = STATE["repo_id"]; branch = STATE["branch"]
    if not STATE["cookie"]:
        return JSONResponse({"ok": False, "error": "Cookie 未配置"})
    r = fetch_browse(repo_id, branch, "")
    info = parse_repo_info(r.text)
    head = info.get("headCommit")
    if not head:
        return JSONResponse({"ok": False, "error": "无法获取分支 HEAD commit"})
    dest_root = DOWNLOAD_DIR / str(repo_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    ok_list, fail_list = [], []
    for p in paths:
        ok, content, note = cookie_file_content(repo_id, head, p)
        if ok and content is not None:
            target = dest_root / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            ok_list.append(p)
        else:
            fail_list.append({"path": p, "reason": note})
    return JSONResponse({"ok": True, "downloaded": ok_list,
                         "failed": fail_list, "dest": str(dest_root)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
