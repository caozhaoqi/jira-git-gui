# -*- coding: utf-8 -*-
"""仓库 / 连接 / 文件浏览 / 搜索 / 提交 相关路由（只读域）。

把 server.py 中原先散落的 status / connect / repos / tree / file / search /
commits / file-at-commit 等端点收敛到独立的 APIRouter，server.py 仅负责 include。
"""
import asyncio
import os
import re
import fnmatch
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
from core.constants import DEFAULT_REQUEST_QPS
from core.client import DEFAULT_DOWNLOAD_WORKERS
from core.app_paths import get_data_root
from core.models import ConnectConfig
from api.schemas import ConnectReq, RepoSelectReq

router = APIRouter()

# 注意：下方用 @router.get/post/delete 等价于原 @app.get/post/delete


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
async def api_tree(path: str = ""):
    """列出目录单层子项（懒加载）。"""
    if not client.repo_id:
        raise HTTPException(400, "尚未指定仓库")
    try:
        entries = await asyncio.to_thread(client.list_level, path)
        return {
            "entries": [
                {
                    "name": e.name,
                    "path": e.path,
                    "type": e.type,
                    "size": e.size,
                    "has_children": e.has_children,
                    "mtime": e.mtime,
                }
                for e in entries
            ]
        }
    except Exception as ex:
        raise HTTPException(500, str(ex))


@router.get("/api/file")
async def api_file(path: str):
    """获取文件内容。"""
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

    限制：依赖 PAT 模式克隆到本地的仓库副本（store/repos/<repo_name>）。
    未克隆时报错，引导用户先克隆。两种模式都用纯 Python 遍历，零新依赖。
    """
    q = (q or "").strip()
    if not q:
        return {"results": [], "total": 0, "truncated": False}

    if not client.repo_name:
        return {"error": "请先选择并克隆仓库到本地（PAT 模式）才能搜索"}

    # 本地仓库根目录
    local_root = Path(get_data_root()) / "repos" / client.repo_name
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
