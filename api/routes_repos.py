# -*- coding: utf-8 -*-
"""仓库 / 连接 / 文件浏览 / 搜索 / 提交 相关路由（只读域）。

把 server.py 中原先散落的 status / connect / repos / tree / file / search /
commits / file-at-commit 等端点收敛到独立的 APIRouter，server.py 仅负责 include。
"""
import asyncio
import os
import re
import fnmatch
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from fastapi import APIRouter
from api.common import (
    app, client, logger,
    _session, _env_loaded, _env_path,
    commit_to_dict,
)
from core.config import save_session, get_session_path
from core.constants import DEFAULT_REQUEST_QPS, REPOS_DIR
from core.client import DEFAULT_DOWNLOAD_WORKERS
from core.app_paths import get_data_root
from core.models import ConnectConfig
from api.schemas import ConnectReq, RepoSelectReq

router = APIRouter()

# 注意：下方用 @router.get/post/delete 等价于原 @app.get/post/delete


# --------------------------------------------------------------------------- #
#  路径直达（/api/tree/resolve）用的纯函数
# --------------------------------------------------------------------------- #
# 远端浏览每一层都要拉一次 GIJBrowseGit.jspa（实测单次可达 10s 量级）。
# 逐层展开去到 N 层深处就是 N × 单次延迟；这里把「从根到目标」的所有层级
# **并发**拉取，墙钟时间从 N×T 压到 ~1×T（外加令牌桶排队，默认 6 QPS）。
_TREE_RESOLVE_MAX_DEPTH = 32      # 防误粘贴超长路径打爆 Jira
_TREE_RESOLVE_MAX_WORKERS = 8


def _split_tree_path(raw: str) -> list[str]:
    """把用户粘贴的路径切成段。

    拒绝 ``..``：本地目录模式下路径会拼到 local_dir 后面，放行 ``..`` 会越权读到
    仓库外的文件。``.`` 与多余斜杠（``a//b``、首尾 ``/``）一律归一化掉，
    这样从浏览器地址栏或 `git show` 输出里粘来的路径都能直接用。
    """
    segs: list[str] = []
    for s in (raw or "").replace("\\", "/").split("/"):
        if not s or s == ".":
            continue
        if s == "..":
            raise ValueError("路径不允许包含 '..'")
        segs.append(s)
    return segs


def _tree_path_prefixes(segs: list[str]) -> list[str]:
    """生成从根到目标的每一层路径：``["", "a", "a/b", "a/b/c"]``。"""
    out = [""]
    cur = ""
    for s in segs:
        cur = f"{cur}/{s}" if cur else s
        out.append(cur)
    return out


def _validate_tree_chain(segs: list[str], levels: dict) -> tuple[str, str]:
    """沿链校验路径是否真实存在，返回 ``(target_type, broken_at)``。

    远端浏览页对不存在的路径是**返回空树而不是 404**，光看响应分不清「空目录」
    和「路径打错了」。这里用父层已拿到的条目逐级核对，把断点精确报给前端。
    """
    cur = ""
    target_type = "dir" if not segs else "missing"
    for seg in segs:
        parent = levels.get(cur) or []
        match = next((e for e in parent if e.name == seg), None)
        cur = f"{cur}/{seg}" if cur else seg
        if match is None:
            return "missing", cur
        target_type = match.type
    return target_type, ""


def _tree_entry_dict(e) -> dict:
    """TreeEntry -> 前端契约的 dict（/api/tree 与 /api/tree/resolve 共用）。"""
    return {
        "name": e.name,
        "path": e.path,
        "type": e.type,
        "size": e.size,
        "has_children": e.has_children,
        "mtime": e.mtime,
    }


@router.get("/api/status")
async def api_status():
    """当前连接 / 仓库状态。"""
    return {
        "mode": client.config.mode,
        "jira_url": client.config.jira_url,
        "username": client.config.username,
        "repo_id": client.repo_id,
        "repo_name": client.repo_name,
        "branch": client.branch,
        "pat_set": bool(client.config.pat),
        "cookie_set": bool(client.config.cookie),
        "cookie_source": "session" if _session.get("cookie") else ("env" if _env_loaded else ""),
        "env_loaded": _env_loaded,
        "env_path": _env_path,
        "session_path": get_session_path(),
        "qps": DEFAULT_REQUEST_QPS,
        "max_workers": DEFAULT_DOWNLOAD_WORKERS,
    }


@router.post("/api/connect")
async def api_connect(req: ConnectReq):
    """设置连接配置并测试连通性。

    Cookie 保留逻辑：前端连接弹窗出于安全不回显 Cookie 明文，用户未重新
    粘贴时 req.cookie 为空。此时若已有 session/上次保存的 Cookie，自动沿用，
    避免每次打开弹窗都丢失 Cookie。
    """
    # Cookie/PAT 模式下：用户未输入新值时，沿用当前已加载的值
    effective_cookie = req.cookie
    if req.mode == "cookie" and not effective_cookie and client.config.cookie:
        effective_cookie = client.config.cookie
    effective_pat = req.pat
    if req.mode == "pat" and not effective_pat and client.config.pat:
        effective_pat = client.config.pat

    cfg = ConnectConfig(
        jira_url=req.jira_url.rstrip("/"),
        username=req.username,
        mode=req.mode,
        pat=effective_pat,
        cookie=effective_cookie,
    )
    client.set_config(cfg)
    if req.repo_id:
        client.set_repo(req.repo_id, req.repo_name, req.branch)

    # 在后台线程执行测试（可能触发 clone）
    result = await asyncio.to_thread(client.connect) or {}

    # 如果探测到仓库名，更新
    rd = result.get("repoDefaults") or {}
    if rd.get("displayName"):
        client.repo_name = rd["displayName"]

    # Cookie 持久化：连通成功则保存到 session.json；失败则提示用户重新获取
    if req.mode == "cookie" and effective_cookie:
        if result.get("cookieOk"):
            save_session(effective_cookie, req.jira_url, req.username)
            result["cookieSaved"] = True
        else:
            result["cookieSaved"] = False
            result["cookieWarning"] = (
                "Cookie 验证失败，可能已过期。请重新从浏览器获取 Cookie 后再试。"
            )

    return result


@router.get("/api/repos")
async def api_discover_repos(refresh: bool = False):
    """发现全部仓库（Cookie 模式）。

    ``refresh=true`` 强制重新发现（绕过 10 分钟缓存）；默认命中缓存秒开。
    """
    if not client.config.cookie:
        return {"repos": [], "error": "未配置 Cookie"}
    try:
        repos = await asyncio.to_thread(client.discover_repos, refresh)
        return {
            "repos": [
                {
                    "repo_id": r.repo_id,
                    "display_name": r.display_name,
                    "clone_url": r.clone_url,
                    "default_branch": r.default_branch,
                }
                for r in repos
            ]
        }
    except Exception as ex:
        logger.error("发现仓库异常: %s", ex, exc_info=True)
        return {"repos": [], "error": str(ex)}


@router.post("/api/repo/select")
async def api_select_repo(req: RepoSelectReq):
    """选择当前仓库。"""
    client.set_repo(req.repo_id, req.repo_name, req.branch)
    return {
        "ok": True,
        "repo_id": client.repo_id,
        "repo_name": client.repo_name,
        "branch": client.branch,
    }


@router.get("/api/tree")
async def api_tree(path: str = "", local_dir: str = "", refresh: bool = False):
    """列出目录单层子项（懒加载）。支持 local_dir 本地目录模式。

    ``refresh=true`` 绕过远端目录缓存（默认 5 分钟 TTL）强制回源。
    """
    if local_dir and Path(local_dir).is_dir():
        try:
            entries = await asyncio.to_thread(
                client.list_level_local_dir, local_dir, path)
            return {
                "entries": [_tree_entry_dict(e) for e in entries],
                "local": True,
            }
        except Exception as ex:
            raise HTTPException(500, str(ex))
    if not client.repo_id:
        raise HTTPException(400, "尚未指定仓库")
    try:
        # list_level 签名是 (repo_id, branch, path)，三个参数缺一不可。
        # 用 list_level_ex 取失败原因：Cookie 失效 / 分支解析不出来时老实现只返回
        # 空列表，前端显示「空」而没有任何提示，用户完全无从处理（「文件树无法预览」
        # 就是这么来的）。
        # refresh=true 绕过目录缓存强制回源（默认命中 5 分钟 TTL 缓存）。
        entries, tree_err = await asyncio.to_thread(
            client.list_level_ex, client.repo_id, client.branch, path, refresh)
        return {
            "error": tree_err or None,
            "entries": [_tree_entry_dict(e) for e in entries],
        }
    except Exception as ex:
        raise HTTPException(500, str(ex))


@router.get("/api/tree/resolve")
async def api_tree_resolve(path: str = "", local_dir: str = "",
                           refresh: bool = False):
    """粘贴完整路径直达：一次性拉回从根到目标的**每一层**条目。

    远端浏览每层都要一次 GIJBrowseGit.jspa 请求（单次 10s 量级）。逐层点下去是
    N × T；这里把 N 层**并发**拉取，墙钟时间压到 ~1 × T（外加令牌桶排队）。

    返回各层条目（前端据此一次性铺开整条祖先链），并沿链校验路径是否真实存在：
    远端对不存在的路径只回空树、不给 404，不校验的话用户粘错路径只会看到一片空白。
    """
    ld = (local_dir or "").strip()
    started = time.time()

    try:
        segs = _split_tree_path(path)
    except ValueError as ex:
        return {"error": str(ex)}

    if len(segs) > _TREE_RESOLVE_MAX_DEPTH:
        return {
            "error": f"路径层级过深（{len(segs)} 层），上限 {_TREE_RESOLVE_MAX_DEPTH} 层"
        }

    use_local = bool(ld) and Path(ld).is_dir()
    if not use_local and not ld and not client.repo_id:
        return {"error": "尚未指定仓库"}
    if ld and not use_local:
        return {"error": f"本地目录不存在：{ld}"}

    prefixes = _tree_path_prefixes(segs)

    def _fetch(p: str):
        try:
            if use_local:
                return p, client.list_level_local_dir(ld, p), ""
            entries, err = client.list_level_ex(
                client.repo_id, client.branch, p, refresh=refresh)
            return p, entries, err
        except Exception as ex:  # 单层失败不应拖垮整条链
            return p, [], str(ex)

    levels: dict[str, list] = {}
    error = ""
    workers = max(1, min(len(prefixes), _TREE_RESOLVE_MAX_WORKERS))
    # ex.map 按输入顺序产出结果，levels 的组装顺序稳定（前端按序铺开）
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for p, entries, err in ex.map(_fetch, prefixes):
            levels[p] = entries or []
            if err and not error:
                error = err

    # 请求级失败（Cookie 失效 / 浏览页不可用）时各层都是空的，
    # 此时再做路径校验只会报一个误导性的「路径不存在」。
    if error:
        target_type, broken_at = "", ""
    else:
        target_type, broken_at = _validate_tree_chain(segs, levels)

    target_path = "/".join(segs)
    return {
        "path": target_path,
        "target": {"path": target_path, "type": target_type},
        "broken_at": broken_at or None,
        "levels": [
            {"path": p, "entries": [_tree_entry_dict(e) for e in levels.get(p, [])]}
            for p in prefixes
        ],
        "error": error or None,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


@router.get("/api/file")
async def api_file(path: str, local_dir: str = ""):
    """获取文件内容。支持 local_dir 本地目录模式（直接读本地文件）。"""
    if local_dir and Path(local_dir).is_dir():
        try:
            full = (Path(local_dir) / path.lstrip("/")) if path else Path(local_dir)
            if not full.exists() or not full.is_file():
                return {"error": f"本地文件不存在：{path}"}
            with open(full, "rb") as fh:
                head = fh.read(8000)
            if b"\x00" in head:
                return {"error": "二进制文件，请在文件树勾选后下载查看"}
            content = full.read_text(encoding="utf-8", errors="replace")
            return {"content": content}
        except Exception as ex:
            return {"error": str(ex)}
    content, err = await asyncio.to_thread(client.get_file, path)
    if err:
        return {"error": err}
    # content 可能是 str 或 bytes
    if isinstance(content, bytes):
        return {"error": "二进制文件，请在文件树勾选后下载查看"}
    return {"content": content}


@router.get("/api/search")
async def api_search(
    q: str = "",
    scope: str = "filename",
    path: str = "",
    limit: int = 200,
    case_sensitive: bool = False,
):
    """在已克隆到本地的仓库中搜索（文件名 / 文件内容）。

    限制：依赖 PAT 模式克隆到本地的仓库副本（store/repos/<repo_id>）。
    未克隆时报错，引导用户先克隆。两种模式都用纯 Python 遍历，零新依赖。
    """
    q = (q or "").strip()
    if not q:
        return {"results": [], "total": 0, "truncated": False}

    if not client.repo_id:
        return {"error": "请先选择并克隆仓库到本地（PAT 模式）才能搜索"}

    # 本地仓库根目录（与 store/repos/<repo_id> 基准一致，避免路径分裂）
    local_root = REPOS_DIR / str(client.repo_id)
    if not local_root.is_dir():
        return {"error": f"本地仓库不存在：{local_root}。请先克隆。"}
    # 限定子目录（必须落在 local_root 内，防越权）
    if path:
        sub = (local_root / path).resolve()
        try:
            sub.relative_to(local_root.resolve())
        except ValueError:
            return {"error": "搜索路径越界"}
        if not sub.is_dir():
            return {"error": f"路径不存在：{sub}"}
        search_root = sub
    else:
        search_root = local_root

    scope = (scope or "filename").lower()
    results = []

    # 跳过 .git 目录与常见大目录
    SKIP_DIRS = {".git", "node_modules", "venv", "__pycache__", ".idea", ".vscode", "dist", "build"}

    def _walk_filtered(root: Path):
        """生成（dirpath, dirnames, filenames），过滤掉 SKIP_DIRS。"""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git")]
            yield Path(dirpath), filenames

    if scope == "filename":
        # 文件名匹配（fnmatch 支持通配符，纯文本也当作包含）
        pat = q if any(c in q for c in "*?[") else f"*{q}*"
        pat_re = re.compile(fnmatch.translate(pat), 0 if case_sensitive else re.IGNORECASE)
        for dirpath, filenames in _walk_filtered(search_root):
            try:
                rel_dir = dirpath.relative_to(local_root)
            except ValueError:
                continue
            for fn in filenames:
                if not pat_re.match(fn):
                    continue
                rel = (rel_dir / fn).as_posix()
                results.append({
                    "path": rel,
                    "type": "filename",
                    "snippet": fn,
                    "line": None,
                })
                if len(results) >= limit:
                    return {"results": results, "total": len(results), "truncated": True}
    else:
        # 文件内容匹配：每行扫描，限定文本文件（按扩展名 + 启发式）
        TEXT_EXTS = {
            ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
            ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
            ".md", ".txt", ".rst", ".adoc",
            ".html", ".htm", ".css", ".scss", ".less",
            ".xml", ".csv", ".tsv", ".sql", ".sh", ".bash", ".zsh",
            ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
            ".rb", ".php", ".pl", ".lua", ".r", ".dart", ".swift",
        }
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pat_re = re.compile(q, flags)
        except re.error:
            return {"error": f"搜索模式语法错误：{q!r}"}

        for dirpath, filenames in _walk_filtered(search_root):
            try:
                rel_dir = dirpath.relative_to(local_root)
            except ValueError:
                continue
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext and ext not in TEXT_EXTS:
                    continue
                full = dirpath / fn
                try:
                    # 限 2MB，避免误打开大文件卡死
                    if full.stat().st_size > 2 * 1024 * 1024:
                        continue
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            m = pat_re.search(line)
                            if not m:
                                continue
                            snippet = line.rstrip("\n")[:200]
                            rel = (rel_dir / fn).as_posix()
                            results.append({
                                "path": rel,
                                "type": "content",
                                "line": line_no,
                                "snippet": snippet,
                            })
                            if len(results) >= limit:
                                return {
                                    "results": results,
                                    "total": len(results),
                                    "truncated": True,
                                }
                except (OSError, UnicodeError):
                    continue

    return {"results": results, "total": len(results), "truncated": False}


@router.get("/api/commits")
async def api_commits(issue_key: str = "", local_mode: bool = False):
    """查询提交记录。"""
    if local_mode:
        if not client.repo_id:
            return {"error": "本地 Git 模式需要先选择一个已克隆的仓库"}
        try:
            commits = await asyncio.to_thread(
                client.get_local_commits, client.repo_id, client.branch)
            return {"commits": [commit_to_dict(c) for c in commits]}
        except Exception as ex:
            return {"error": str(ex)}
    else:
        if not issue_key and not client.repo_id:
            return {"error": "请先选择仓库或填入 Jira issue 单号"}
        try:
            commits = await asyncio.to_thread(
                client.get_commits, issue_key, client.repo_id, client.branch)
            return {"commits": [commit_to_dict(c) for c in commits]}
        except Exception as ex:
            return {"error": str(ex)}


@router.get("/api/file-at-commit")
async def api_file_at_commit(commit_id: str, path: str):
    """查看某次提交中某文件的历史版本。"""
    if not client.repo_id:
        raise HTTPException(400, "未指定仓库")
    try:
        content, err = await asyncio.to_thread(
            client.get_file_at_commit, client.repo_id, commit_id, path)
        if err:
            return {"error": err}
        if isinstance(content, bytes):
            return {"error": "二进制文件，不支持预览"}
        return {"content": content}
    except Exception as ex:
        return {"error": str(ex)}
