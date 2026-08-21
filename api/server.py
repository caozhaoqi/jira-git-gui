# -*- coding: utf-8 -*-
"""FastAPI 后端 —— 封装 core/client.py，为 Electron/Web 前端提供 REST + SSE API。

架构：
- JiraGitClient 实例全局单例（单用户本地工具）
- 长任务（clone/download）在后台线程执行，日志和进度通过 SSE 实时推送
- 前端通过 REST 发起操作，通过 SSE 接收实时反馈

运行：
    python -m api.server          # 开发模式
    python -m api.server --port 8787
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import queue
import time
import fnmatch
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException, WebSocket
from fastapi.responses import (JSONResponse, StreamingResponse, FileResponse,
                                PlainTextResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.client import JiraGitClient, DEFAULT_DOWNLOAD_WORKERS, NetworkWatchdog
from core.config import load_config, load_session, save_session, clear_session, get_session_path, load_merge_config, load_cf_accounts, load_hcm_whitelist
from core.constants import DEFAULT_REQUEST_QPS, DOWNLOAD_DIR
from core.app_paths import get_data_root
from core.models import ConnectConfig, RepoInfo, TreeEntry, Commit, CommitFile
from api.schemas import (
    ConnectReq, RepoSelectReq, CloneReq, DownloadReq, DownloadRepoReq, RateLimitReq, CommitsReq,
    CfLogReq, CfLogExportReq, CfLoginReq, CfCaptchaReq, ClipboardSaveReq,
)

# --------------------------------------------------------------------------- #
#  全局状态
# --------------------------------------------------------------------------- #
app = FastAPI(title="Jira Git GUI API", version="2.0")

# 客户端单例
client = JiraGitClient()

# 从 .env 加载默认配置
_cfg, _env_loaded, _env_path = load_config()
if _env_loaded:
    client.set_config(_cfg)

# 从 session.json 加载上次保存的 Cookie（优先级高于 .env）
_session = load_session()
if _session.get("cookie"):
    client.config.cookie = _session["cookie"]
    if _session.get("jira_url") and not client.config.jira_url:
        client.config.jira_url = _session["jira_url"]
    if _session.get("username") and not client.config.username:
        client.config.username = _session["username"]

# 日志（使用 core.logger 但不安装 Qt hooks）
from core.logger import get_logger
logger = get_logger()

# SSE 事件总线：所有连接的客户端共享一个广播队列
_event_subscribers: list[asyncio.Queue] = []
_event_lock = threading.Lock()
# 主事件循环引用：供工作线程通过 call_soon_threadsafe 安全唤醒循环
_main_loop: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def _capture_loop():
    """捕获主事件循环，供 _broadcast 跨线程安全调度。"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()


def _enqueue_event(q: asyncio.Queue, item: dict) -> None:
    """在事件循环线程中执行入队（唤醒 await q.get() 的消费者）。"""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass

# 当前下载任务取消标志
_download_cancel = threading.Event()
# 当前下载任务状态
_task_status = {"running": False, "type": None}


def _broadcast(event: str, data: Any) -> None:
    """向所有 SSE 订阅者推送事件（跨线程安全）。

    长任务（clone/download/scan）在工作线程中调用本函数。直接调用
    asyncio.Queue.put_nowait 会经由 loop.call_soon 唤醒消费者，但
    call_soon 不会写 self-pipe，无法唤醒正阻塞在 select() 的事件循环，
    导致进度事件被延迟到任务结束或 15s 心跳才送达。改用
    call_soon_threadsafe 把入队操作调度回循环线程，可立即唤醒消费者。
    """
    msg = json.dumps(data, ensure_ascii=False, default=str)
    item = {"event": event, "data": msg}
    with _event_lock:
        subs = list(_event_subscribers)
    loop = _main_loop
    for q in subs:
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(_enqueue_event, q, item)
            except RuntimeError:
                # 循环已关闭，忽略
                pass
        else:
            # 事件循环尚未就绪（理论上仅主循环线程内调用可达）
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass


def _log_callback(msg: str) -> None:
    """client 回调：把日志推送到 SSE。"""
    _broadcast("log", {"msg": msg, "ts": time.strftime("%H:%M:%S")})


def _progress_callback(done: int, total: int, path: str) -> None:
    """client 回调：把进度推送到 SSE。"""
    pct = (done * 100 // total) if total > 0 else 0
    _broadcast("progress", {"done": done, "total": total, "pct": pct, "path": path})


def _make_should_cancel(user_cancel: threading.Event,
                        watchdog: NetworkWatchdog,
                        task_label: str = "任务"):
    """合成 should_cancel 回调：同时响应用户取消和网络看门狗触发。

    当看门狗首次触发时，额外广播 ``network_warning`` SSE 事件以通知前端。
    """
    _warned = threading.Event()

    def should_cancel() -> bool:
        if user_cancel.is_set():
            return True
        if watchdog.should_abort():
            if not _warned.is_set():
                _warned.set()
                _broadcast("network_warning", {
                    "level": "error",
                    "message": f"网络中断：{task_label} 因连续网络失败自动停止（{watchdog.reason}）",
                    "failure_count": watchdog.failure_count,
                })
                logger.warning("网络看门狗触发：%s", watchdog.reason)
            return True
        return False

    return should_cancel


# --------------------------------------------------------------------------- #
#  Pydantic 请求模型
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  REST 端点
# --------------------------------------------------------------------------- #
@app.get("/api/status")
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


@app.post("/api/connect")
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


@app.get("/api/repos")
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


@app.post("/api/repo/select")
async def api_select_repo(req: RepoSelectReq):
    """选择当前仓库。"""
    client.set_repo(req.repo_id, req.repo_name, req.branch)
    return {
        "ok": True,
        "repo_id": client.repo_id,
        "repo_name": client.repo_name,
        "branch": client.branch,
    }


@app.get("/api/tree")
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


@app.get("/api/file")
async def api_file(path: str):
    """获取文件内容。"""
    content, err = await asyncio.to_thread(client.get_file, path)
    if err:
        return {"error": err}
    # content 可能是 str 或 bytes
    if isinstance(content, bytes):
        return {"error": "二进制文件，请在文件树勾选后下载查看"}
    return {"content": content}


@app.get("/api/search")
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


@app.post("/api/clone")
async def api_clone(req: CloneReq):
    """克隆仓库（PAT 模式）。通过 SSE 推送进度。"""
    repo_id = req.repo_id or client.repo_id
    repo_name = req.repo_name or client.repo_name
    branch = req.branch or client.branch

    if not repo_id:
        raise HTTPException(400, "请先指定仓库 ID")
    if not client.config.pat:
        raise HTTPException(400, "当前未配置 PAT，无法克隆")

    # 在后台线程执行
    def _do_clone():
        _task_status["running"] = True
        _task_status["type"] = "clone"
        try:
            ok, msg, path = client.clone_repo(
                repo_id, repo_name, branch,
                client.config.pat, client.config.username,
                on_log=_log_callback,
            )
            _broadcast("clone_done", {"ok": ok, "msg": msg, "path": path})
        except Exception as ex:
            _broadcast("clone_done", {"ok": False, "msg": str(ex), "path": None})
            logger.error("克隆异常", exc_info=True)
        finally:
            _task_status["running"] = False
            _task_status["type"] = None

    threading.Thread(target=_do_clone, daemon=True).start()
    return {"started": True}


@app.post("/api/download")
async def api_download(req: DownloadReq):
    """下载选中文件（Cookie 模式）。通过 SSE 推送进度。"""
    if not client.config.cookie:
        raise HTTPException(400, "下载功能仅 Cookie 模式可用")
    if not req.paths:
        raise HTTPException(400, "未勾选任何文件")

    _download_cancel.clear()
    watchdog = NetworkWatchdog(threshold=5)
    client._watchdog = watchdog
    should_cancel = _make_should_cancel(_download_cancel, watchdog, "下载")

    def _do_download():
        _task_status["running"] = True
        _task_status["type"] = "download"
        try:
            ok_list, fail_list, dest, skipped = client.download(
                req.paths,
                max_workers=req.max_workers,
                on_log=_log_callback,
                on_progress=_progress_callback,
                should_cancel=should_cancel,
            )
            _broadcast("download_done", {
                "ok_count": len(ok_list),
                "fail_count": len(fail_list),
                "dest": str(dest) if dest else None,
                "skipped": skipped,
                "fails": fail_list,
            })
        except Exception as ex:
            _broadcast("download_done", {
                "ok_count": 0, "fail_count": 0, "dest": None,
                "skipped": 0, "error": str(ex),
            })
            logger.error("下载异常", exc_info=True)
        finally:
            client._watchdog = None
            _task_status["running"] = False
            _task_status["type"] = None

    threading.Thread(target=_do_download, daemon=True).start()
    return {"started": True}


@app.post("/api/download/repo")
async def api_download_repo(req: DownloadRepoReq):
    """下载整个仓库（Cookie 模式）。通过 SSE 推送进度。"""
    if not client.config.cookie:
        raise HTTPException(400, "整库下载仅 Cookie 模式可用")
    repo_id = req.repo_id or client.repo_id
    if not repo_id:
        raise HTTPException(400, "请先指定仓库")

    _download_cancel.clear()
    watchdog = NetworkWatchdog(threshold=5)
    client._watchdog = watchdog
    should_cancel = _make_should_cancel(_download_cancel, watchdog, "整库下载")

    def _do_download():
        _task_status["running"] = True
        _task_status["type"] = "download_repo"
        try:
            ok_count, fail_list, dest, skipped = client.download_repo(
                repo_id, req.branch or client.branch,
                max_workers=req.max_workers,
                on_log=_log_callback,
                on_progress=_progress_callback,
                should_cancel=should_cancel,
            )
            _broadcast("download_done", {
                "ok_count": ok_count,
                "fail_count": len(fail_list),
                "dest": str(dest) if dest else None,
                "skipped": skipped,
                "fails": fail_list[:20],
                "total_fails": len(fail_list),
            })
        except Exception as ex:
            _broadcast("download_done", {
                "ok_count": 0, "fail_count": 0, "dest": None,
                "skipped": 0, "error": str(ex),
            })
            logger.error("整库下载异常", exc_info=True)
        finally:
            client._watchdog = None
            _task_status["running"] = False
            _task_status["type"] = None

    threading.Thread(target=_do_download, daemon=True).start()
    return {"started": True}


@app.post("/api/download/cancel")
async def api_cancel_download():
    """取消当前下载。"""
    _download_cancel.set()
    return {"ok": True}


@app.post("/api/rate-limit")
async def api_set_rate_limit(req: RateLimitReq):
    """设置请求速率上限。"""
    client.set_rate_limit(req.qps)
    return {"ok": True, "qps": req.qps}


@app.delete("/api/session")
async def api_clear_session():
    """清除本地保存的 Cookie 会话。"""
    clear_session()
    return {"ok": True, "msg": "已清除本地 Cookie 会话"}


@app.get("/api/commits")
async def api_commits(issue_key: str = "", local_mode: bool = False):
    """查询提交记录。"""
    if local_mode:
        if not client.repo_id:
            return {"error": "本地 Git 模式需要先选择一个已克隆的仓库"}
        try:
            commits = await asyncio.to_thread(
                client.get_local_commits, client.repo_id, client.branch)
            return {"commits": [_commit_to_dict(c) for c in commits]}
        except Exception as ex:
            return {"error": str(ex)}
    else:
        if not issue_key and not client.repo_id:
            return {"error": "请先选择仓库或填入 Jira issue 单号"}
        try:
            commits = await asyncio.to_thread(
                client.get_commits, issue_key, client.repo_id, client.branch)
            return {"commits": [_commit_to_dict(c) for c in commits]}
        except Exception as ex:
            return {"error": str(ex)}


@app.get("/api/file-at-commit")
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


@app.delete("/api/resume")
async def api_clear_resume():
    """清空断点续传清单。"""
    if not client.repo_id:
        raise HTTPException(400, "请先选择仓库")
    mp = DOWNLOAD_DIR / str(client.repo_id) / JiraGitClient._MANIFEST_NAME
    if mp.exists():
        try:
            mp.unlink()
            return {"ok": True, "msg": f"已清空断点续传清单：{mp}"}
        except Exception as ex:
            raise HTTPException(500, f"清空断点失败：{ex}")
    return {"ok": True, "msg": "当前没有断点续传清单（无需清空）"}


# --------------------------------------------------------------------------- #
#  差异对比 & 合并（集成缓存）
# --------------------------------------------------------------------------- #
from core import differ as _differ
from core import cache as _cache
from core import sync_history as _history
from pydantic import BaseModel as _BM

class DiffScanReq(_BM):
    local_dir: str
    repo_name: str = ""
    use_cache: bool = True
    ignore_line_endings: bool = True

class DiffFileReq(_BM):
    local_dir: str
    path: str
    use_cache: bool = True

class MergeReq(_BM):
    local_dir: str
    path: str
    use_cache: bool = True
    status: str = ""  # 对应 DiffStatus，供批量合并时按状态过滤

@app.post("/api/diff/scan")
async def api_diff_scan(req: DiffScanReq):
    """扫描本地目录和远程仓库，返回差异列表（缓存优先）。

    扫描过程通过 SSE 推送进度：
    - scan_stage: 阶段切换 {stage, message, ...}
    - scan_progress: 远程扫描进度 {done, total, pct, message}
    - scan_done: 扫描完成 {summary}
    - scan_error: 扫描出错 {message}
    - network_warning: 网络中断自动停止 {level, message}
    """
    import os
    if not os.path.isdir(req.local_dir):
        _broadcast("scan_error", {"message": f"本地目录不存在：{req.local_dir}"})
        raise HTTPException(400, f"本地目录不存在：{req.local_dir}")
    if not client.repo_id:
        _broadcast("scan_error", {"message": "请先选择远程仓库"})
        raise HTTPException(400, "请先选择远程仓库")

    namespace = str(client.repo_id)

    # 创建网络看门狗，绑定到客户端供底层请求使用
    scan_cancel = threading.Event()
    watchdog = NetworkWatchdog(threshold=5)
    client._watchdog = watchdog
    should_cancel = _make_should_cancel(scan_cancel, watchdog, "差异扫描")

    # 在后台线程执行扫描（缓存优先），通过回调推送 SSE 进度
    def _scan():
        # 远程扫描涉及大量目录级请求（数千个），默认 6qps 全局限流会拖到几分钟。
        # 扫描是用户主动的低并发批量操作，临时放宽到 20qps（3 workers 足够），
        # 结束后恢复用户设定值，避免影响其它操作。
        import core.throttle as _th
        saved_qps = _th.get_rate_limiter().qps
        try:
            _th.set_global_rate_limit(max(saved_qps, 20))
            return _scan_inner()
        finally:
            _th.set_global_rate_limit(saved_qps)

    def _scan_inner():
        try:
            # 阶段 1：本地扫描
            _broadcast("scan_stage", {"stage": "local", "message": "正在扫描本地文件…", "pct": 5})
            local_files = _differ.scan_local_cached(req.local_dir, use_cache=req.use_cache)
            _broadcast("scan_stage", {
                "stage": "remote", "message": "正在扫描远程文件…",
                "local_count": len(local_files), "pct": 10,
            })

            # 阶段 2：远程扫描（带进度回调 + 看门狗）
            def _on_remote_progress(scanned, pending, processed, dirs_seen):
                # 远程扫描进度 10% ~ 80%：按「已处理目录 / 已见目录」占比估算
                ratio = min(1.0, processed / max(dirs_seen, 1))
                pct = round(10 + 70 * ratio)
                _broadcast("scan_progress", {
                    "done": scanned,
                    "total": dirs_seen,
                    "pct": pct,
                    "pending_dirs": pending,
                    "message": f"已扫描 {scanned} 个文件，{pending} 个目录待扫",
                })

            remote_files = _differ.scan_remote_cached(
                client, namespace,
                max_workers=3,
                tree_ttl=3600,
                on_progress=_on_remote_progress,
                use_cache=req.use_cache,
                should_cancel=should_cancel,
            )
            _broadcast("scan_stage", {
                "stage": "diff", "message": "正在计算差异…",
                "local_count": len(local_files),
                "remote_count": len(remote_files),
                "pct": 90,
            })

            # 阶段 3：差异计算
            result = _differ.compute_diff(
                local_files, remote_files,
                ignore_line_endings=req.ignore_line_endings,
            )
            _broadcast("scan_done", {"summary": result.summary()})
            return result
        finally:
            client._watchdog = None

    try:
        result = await asyncio.to_thread(_scan)
    except Exception as ex:
        _broadcast("scan_error", {"message": str(ex)})
        raise HTTPException(500, f"扫描失败：{ex}")

    # 返回摘要 + 条目列表（排除 same 以减少传输量）
    entries = [
        {
            "path": e.path,
            "status": e.status.value,
            "local_size": e.local_size,
            "remote_size": e.remote_size,
        }
        for e in result.entries
        if e.status != _differ.DiffStatus.SAME
    ]

    return {
        "summary": result.summary(),
        "entries": entries,
        "cached": req.use_cache,
    }


@app.post("/api/diff/file")
async def api_diff_file(req: DiffFileReq):
    """获取单个文件的 unified diff（远程内容缓存优先）。"""
    import os
    local_path = os.path.join(req.local_dir, req.path)
    if not os.path.isfile(local_path):
        local_content = ""
    else:
        local_content = Path(local_path).read_text(encoding="utf-8", errors="replace")

    namespace = str(client.repo_id) if client.repo_id else "default"
    remote_content = await asyncio.to_thread(
        _differ.get_file_cached,
        client, namespace, req.path,
        86400, req.use_cache,
    )

    diff_text = _differ.file_diff(local_path, remote_content or "")
    normalized_same = _differ.is_whitespace_only_diff(local_path, remote_content or "")

    # 结构化文件：返回规范化展开后的内容供前端侧并排/raw 视图可读展示。
    # 仅展示层——合并仍走 get_file_cached 的原始远程字节，不受影响。
    show_local = _differ.canonical_text(local_path, local_content) if local_content else ""
    show_remote = _differ.canonical_text(req.path, remote_content or "") if remote_content else ""

    return {
        "path": req.path,
        "diff": diff_text,
        "local_content": show_local,
        "remote_content": show_remote,
        "normalized_same": normalized_same,
        "cached": req.use_cache,
    }


@app.post("/api/diff/merge")
async def api_diff_merge(req: MergeReq):
    """将远程文件合并到本地（远程内容缓存优先）。"""
    namespace = str(client.repo_id) if client.repo_id else "default"
    remote_content = await asyncio.to_thread(
        _differ.get_file_cached,
        client, namespace, req.path,
        86400, req.use_cache,
    )

    ok = _differ.merge_to_local(req.local_dir, req.path, remote_content or "")
    return {"ok": ok, "path": req.path}


@app.post("/api/diff/merge-batch")
async def api_diff_merge_batch(reqs: list[MergeReq], status_filter: str = ""):
    """批量合并多个文件（缓存优先，并行抓取 + 并行写入）。

    性能优化（针对「本地合并慢」）：
    1. 高并发：抓取默认 12 并发（比扫描激进，因为每个请求只有 1 个文件），
       写入默认 20 并发（纯本地 I/O，无网络瓶颈）
    2. 分阶段：抓取和写入解耦（用 asyncio.Queue 管道），抓取不被写入阻塞
    3. 快速跳过：本地相同大小且读取内容与远程 bytes 相同 → 不写盘
    4. 父目录缓存：用全局 _DIR_CACHE 集合同步 mkdir(parents=True)
    5. 每完成一个文件即推送进度（merge_progress SSE），便于前端实时显示

    Args:
        status_filter: 按差异状态过滤，逗号分隔，如 "remote_only"。
                       为空时处理全部请求。
    """
    if status_filter:
        filters = {s.strip() for s in status_filter.split(",") if s.strip()}
        reqs = [r for r in reqs if r.status in filters]

    namespace = str(client.repo_id) if client.repo_id else "default"
    total = len(reqs)
    _broadcast("merge_start", {"total": total})
    _differ.clear_dir_cache()

    FETCH_WORKERS = min(12, max(4, DEFAULT_DOWNLOAD_WORKERS * 2))
    WRITE_WORKERS = 20

    # pipeline: fetch -> [queue] -> write, bounded to avoid memory blow-up
    pipe: "asyncio.Queue[tuple[int, MergeReq, str, Optional[str]]]" = asyncio.Queue(
        maxsize=FETCH_WORKERS * 2
    )
    done_counter = 0
    counter_lock = asyncio.Lock()

    async def _fetch(idx: int, req: MergeReq):
        """抓取一个远程文件（经缓存），结果写入管道。"""
        err: Optional[str] = None
        content: Optional[str] = ""
        try:
            content = await asyncio.to_thread(
                _differ.get_file_cached,
                client, namespace, req.path,
                86400, req.use_cache,
            )
        except Exception as ex:
            err = str(ex)
        await pipe.put((idx, req, content, err))

    async def _writer():
        """从管道取 (idx, req, content, err) 并写入磁盘。"""
        nonlocal done_counter
        while True:
            item = await pipe.get()
            try:
                (idx, req, content, fetch_err) = item
                # 毒丸：停止信号
                if req is None:
                    break
                ok = False
                err = fetch_err
                if err is None:
                    try:
                        ok = _differ.merge_to_local(
                            req.local_dir, req.path, content or "",
                        )
                    except Exception as ex:
                        ok = False
                        err = str(ex)
                if not ok and err is None:
                    err = "写入失败（可能权限不足）"
                async with counter_lock:
                    done_counter += 1
                    cur = done_counter
                _broadcast("merge_progress", {
                    "done": cur,
                    "total": total,
                    "pct": (cur * 100 // total) if total > 0 else 100,
                    "path": req.path,
                    "ok": ok,
                    "error": err,
                })
                results[idx] = {"path": req.path, "ok": ok, "error": err}
            finally:
                pipe.task_done()

    # 预分配 results 列表（按原始顺序，便于与前端 reqs 一一对应）
    results: list[dict] = [None] * total  # type: ignore[list-item]

    # 启动写者
    writer_tasks = [asyncio.create_task(_writer()) for _ in range(WRITE_WORKERS)]

    # 并发抓取（FETCH_WORKERS 限流，避免同时向 Jira 发 500 个请求）
    fetch_sem = asyncio.Semaphore(FETCH_WORKERS)

    async def _fetch_limited(idx, req):
        async with fetch_sem:
            await _fetch(idx, req)

    fetch_tasks = [asyncio.create_task(_fetch_limited(i, r)) for i, r in enumerate(reqs)]
    try:
        await asyncio.gather(*fetch_tasks)
    except Exception as ex:
        # 某个 fetch 的异常已写入对应 result；这里只是防止 gather 抛到外层
        logger.warning("merge batch gather(fetch) exception: %s", ex)

    # 等所有写入任务消费完队列
    await pipe.join()

    # 发送毒丸停止写者
    for _ in writer_tasks:
        await pipe.put((-1, None, None, None))
    await asyncio.gather(*writer_tasks, return_exceptions=True)

    ok_count = sum(1 for r in results if r and r["ok"])
    fail_count = total - ok_count
    fails = [{"path": r["path"], "error": r["error"]}
             for r in results if r and not r["ok"]][:50]
    _broadcast("merge_done", {"ok_count": ok_count, "fail_count": fail_count, "fails": fails})
    return {"results": results}


@app.post("/api/diff/invalidate")
async def api_diff_invalidate():
    """使当前仓库的缓存失效（强制下次重新拉取）。"""
    if not client.repo_id:
        raise HTTPException(400, "请先选择仓库")
    n = _cache.invalidate(str(client.repo_id))
    return {"ok": True, "cleared": n}


@app.get("/api/diff/repo-mappings")
async def api_diff_repo_mappings():
    """返回 .env 中 MERGE_REPO_* 配置的远程仓库 → 本地目录映射。"""
    cfg = load_merge_config()
    mappings = [
        {"repo_name": name, "local_dir": local_dir}
        for name, local_dir in cfg["repo_map"].items()
    ]
    return {"mappings": mappings}


@app.get("/api/diff/discover-local-dirs")
async def api_diff_discover_local_dirs(repo_name: str = ""):
    """根据仓库名自动扫描本地候选目录。

    扫描策略：
    1. 优先使用 .env 中 MERGE_SCAN_ROOTS（逗号分隔）作为根目录；
       未配置则回退到 ~/Downloads 与 ~。
    2. 在每个根目录下搜索 basename 与 repo_name 相同或包含的目录，
       深度最多 2 层，避免遍历整个文件系统。
    """
    if not repo_name:
        raise HTTPException(400, "repo_name 不能为空")

    import os

    merge_cfg = load_merge_config()
    roots_str = merge_cfg.get("scan_roots", "")
    roots: list[Path] = []
    if roots_str:
        roots = [Path(p.strip()).expanduser() for p in roots_str.split(",") if p.strip()]
    if not roots:
        roots = [Path.home() / "Downloads", Path.home()]

    repo_lower = repo_name.lower()
    candidates: list[tuple[int, str]] = []

    def _score(basename: str) -> int:
        b = basename.lower()
        if b == repo_lower:
            return 100
        if repo_lower in b:
            return 70
        if b in repo_lower:
            return 50
        return 0

    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                sc = _score(entry.name)
                if sc:
                    candidates.append((sc, entry.path))
                # 深度 2：再扫一级子目录
                try:
                    for sub in os.scandir(entry):
                        if sub.is_dir():
                            sc2 = _score(sub.name)
                            if sc2:
                                # 子目录匹配分数略降
                                candidates.append((sc2 - 10, sub.path))
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            continue

    # 去重并按分数降序
    seen: set[str] = set()
    result: list[str] = []
    for score, path in sorted(candidates, key=lambda x: -x[0]):
        if path not in seen:
            seen.add(path)
            result.append(path)

    return {"repo_name": repo_name, "candidates": result[:10]}


# --------------------------------------------------------------------------- #
#  缓存管理
# --------------------------------------------------------------------------- #
@app.get("/api/cache/info")
async def api_cache_info():
    """获取缓存统计信息。"""
    return _cache.cache_info()


@app.delete("/api/cache")
async def api_cache_clear(namespace: str = ""):
    """清空缓存（可指定命名空间）。"""
    if namespace:
        n = _cache.invalidate(namespace)
    else:
        n = _cache.clear_all()
    return {"ok": True, "cleared": n}


# --------------------------------------------------------------------------- #
#  同步历史（类 git log）
# --------------------------------------------------------------------------- #
@app.get("/api/sync-history")
async def api_sync_history_list(limit: int = 50, date: str = ""):
    """列出同步历史（类 git log）。"""
    return {"entries": _history.list_history(limit=limit, date_str=date or None)}


@app.get("/api/sync-history/stats")
async def api_sync_history_stats():
    """同步统计信息。"""
    return _history.stats()


@app.get("/api/sync-history/{commit_id}")
async def api_sync_history_show(commit_id: str):
    """查看某次同步详情（类 git show）。"""
    entry = _history.show(commit_id)
    if not entry:
        raise HTTPException(404, "未找到该同步记录")
    return entry


@app.delete("/api/sync-history")
async def api_sync_history_clear(date: str = ""):
    """清空同步历史（可指定日期）。"""
    n = _history.clear(date_str=date or None)
    return {"ok": True, "cleared": n}


# --------------------------------------------------------------------------- #
#  K8s Pod 状态 / 日志快照
# --------------------------------------------------------------------------- #
from core.k8s_snapshot import (
    run_snapshot as _k8s_run_snapshot,
    fetch_logs as _k8s_fetch_logs,
    run_kubectl as _k8s_run_kubectl,
)
from core import k8s_manager as _k8s_mgr
from core.errors import UserError as _UserError

# 单用户本地工具：一次只允许一个快照任务；任务状态通过 SSE 广播
@app.get("/api/events")
async def api_events(request: Request):
    """SSE 端点：推送日志、进度、任务完成事件。"""
    q = asyncio.Queue(maxsize=500)
    with _event_lock:
        _event_subscribers.append(q)

    async def event_stream():
        try:
            # 推送初始状态
            yield f"event: ready\ndata: {json.dumps({'status': 'connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: {msg['event']}\ndata: {msg['data']}\n\n"
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield f"event: ping\ndata: {json.dumps({'ts': time.time()})}\n\n"
        finally:
            with _event_lock:
                if q in _event_subscribers:
                    _event_subscribers.remove(q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
#  辅助
# --------------------------------------------------------------------------- #
def _commit_to_dict(c: Commit) -> dict:
    return {
        "commit_id": c.commit_id,
        "display_id": c.display_id,
        "author": c.author,
        "date": c.date,
        "message": c.message,
        "branch": c.branch,
        "repository_name": c.repository_name,
        "files": [
            {
                "path": f.path,
                "change_type": f.change_type,
                "lines_added": f.lines_added,
                "lines_removed": f.lines_removed,
            }
            for f in c.files
        ],
    }


# --------------------------------------------------------------------------- #
#  云函数日志查询
# --------------------------------------------------------------------------- #
@app.get("/api/cf/accounts")
async def api_cf_accounts():
    """返回本地配置文件中的 CF 账号列表（含密码，仅供本机前端自动填充）。

    来源为 cf_accounts.local.json（已被 .gitignore 忽略，含真实账号密码，
    绝不进入 git）。找不到时回退 example 模板（无真实密码）。
    """
    try:
        accounts = load_cf_accounts()
    except Exception as e:
        logger.exception(f"[CF] 读取账号配置失败: {e}")
        accounts = []
    return {"accounts": accounts}


import httpx

# HCM 平台连接业务白名单（改了会连不上平台）：统一从 hcm_whitelist.json 读取，不再硬编码。
# 含 hcminner 鉴权头、真实日志查询接口路径、参考项目名、真实平台域名。
_HCM_WL = load_hcm_whitelist()
_HCM_HCMINNER_HEADER = _HCM_WL["hcminner"].get("header", "hcminner")
_HCM_HCMINNER_VALUE = _HCM_WL["hcminner"].get("value", "1")
_HCM_MODEL_LIST_API = _HCM_WL["model_list_api"].get("path", "/api/hcm.model.list")


_CF_CAPTCHA_CACHE = {}
_CF_CAPTCHA_TTL = {}
_CF_CAPTCHA_MAX = 200


def _sniff_image_type(data: bytes) -> str | None:
    """按 magic bytes 判定图片真实类型（CF 验证码端点常把 content-type 错标为 text/html）。"""
    if not data or len(data) < 4:
        return None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"RIFF",) and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _new_cf_client(req_proxy: str, existing_cookies=None):
    """创建 httpx 客户端；existing_cookies 可选 CookieJar（验证码→登录同会话）。"""
    kwargs = dict(timeout=15, follow_redirects=True)
    if req_proxy:
        kwargs["proxy"] = req_proxy
    else:
        kwargs["transport"] = httpx.AsyncHTTPTransport()
    if existing_cookies is not None:
        kwargs["cookies"] = existing_cookies
    return httpx.AsyncClient(**kwargs)


@app.post("/api/cf/captcha")
async def api_cf_captcha(req: CfCaptchaReq):
    """获取 CF 登录图片验证码。

    参考 hcm-cloud-vue 前端源码：验证码图片端点为 /img/imagevalidatecode?index={index}&v={random}，
    登录时需回传同一个 image_code_index + 用户输入的 image_code。
    返回：{captcha_id, image_code_index, image: "data:image/xxx;base64,xxxxx"}
    """
    if not req.server_url:
        raise HTTPException(400, "请先配置服务器地址")
    base = req.server_url.rstrip("/")
    # CF 验证码图片端点（参考 controller.login.js: init_image_code）
    url = f"{base}/img/imagevalidatecode"

    # 清理过期的 captcha 缓存（3 分钟 TTL）
    import time as _time
    now = _time.time()
    for cid in list(_CF_CAPTCHA_TTL.keys()):
        if now - _CF_CAPTCHA_TTL[cid] > 180:
            _CF_CAPTCHA_CACHE.pop(cid, None)
            _CF_CAPTCHA_TTL.pop(cid, None)
    if len(_CF_CAPTCHA_CACHE) > _CF_CAPTCHA_MAX:
        oldest = sorted(_CF_CAPTCHA_TTL, key=_CF_CAPTCHA_TTL.get)
        for cid in oldest[:100]:
            _CF_CAPTCHA_CACHE.pop(cid, None)
            _CF_CAPTCHA_TTL.pop(cid, None)

    import secrets, base64
    captcha_id = secrets.token_urlsafe(12)
    # image_code_index 关联验证码图与登录请求，参考 hcm-cloud-vue 的 window.image_code_index
    image_code_index = secrets.token_hex(4)
    try:
        jar = httpx.Cookies()
        async with _new_cf_client(req.proxy, existing_cookies=jar) as client:
            # 先访问登录页获取初始 cookie（有些部署必须先有 session cookie 才能拿验证码图）
            try:
                await client.get(f"{base}/login")
            except Exception:
                pass
            resp = await client.get(url, params={"index": image_code_index, "v": secrets.token_hex(4)})
            resp.raise_for_status()
            if not resp.content or len(resp.content) < 10:
                raise HTTPException(502, "服务器未返回验证码图片")
            # 注意：CF 该端点常把图片 content-type 错标为 text/html，必须按 magic bytes 判定真实图片类型
            ctype = _sniff_image_type(resp.content)
            if ctype is None:
                snippet = resp.content[:200].decode("utf-8", "ignore")
                raise HTTPException(502, f"服务器未返回有效图片验证码，响应前200字符: {snippet}")
            b64 = base64.b64encode(resp.content).decode("ascii")
            _CF_CAPTCHA_CACHE[captcha_id] = {"jar": jar.jar, "index": image_code_index}
            _CF_CAPTCHA_TTL[captcha_id] = _time.time()
            return {
                "captcha_id": captcha_id,
                "image_code_index": image_code_index,
                "image": f"data:{ctype};base64,{b64}",
            }
    except httpx.ConnectError as e:
        raise HTTPException(502, f"无法连接服务器 {base}: {e}")
    except httpx.TimeoutException:
        raise HTTPException(504, "获取验证码超时")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[CF] 获取验证码异常: {e}")
        raise HTTPException(500, f"获取验证码失败: {type(e).__name__}: {e}")


@app.post("/api/cf/logs")
async def api_cf_logs(req: CfLogReq):
    """代理查询 CF 平台 dynamic_log 日志。

    通过后端代理请求目标服务器，避免浏览器跨域限制。
    认证方式（依次尝试）：
      1) Cookie: token=xxx（最常见的前端登录态形式）
      2) Authorization: Bearer {token} + hcminner: 1（内部 OpenAPI 形式）
    查询接口：POST {model_list_api}（路径来自 hcm_whitelist.json，改了会连不上平台）
    """
    if not req.token:
        raise HTTPException(400, "请先配置 token")
    # 保护：明显不是 token 的值（HTML片段、input属性值片段）直接报错
    suspicious = ("<html", "<!doctype", "__image_validate_index", "input ", "name=")
    low = req.token.lower()
    for s in suspicious:
        if s in low:
            raise HTTPException(400, "Token 格式异常，请重新获取 Token 后再查询（当前值疑似 HTML/验证码片段，而非登录 Token）")

    base = req.server_url.rstrip("/")
    url = f"{base}{_HCM_MODEL_LIST_API}"
    base_headers_json = {"Content-Type": "application/json"}
    # 参考 hcm-core/test/test_avoid_check.py: dynamic_log 用 filter_dict + 直接值（非 advance_filter_dict + {eq}）
    filter_dict = {}
    if req.log_type:
        filter_dict["log_type"] = req.log_type
    payload = {
        "model": "dynamic_log",
        "page_index": req.page_index,
        "page_size": req.page_size,
        "filter_dict": filter_dict,
    }

    # 构造三种认证方式，依次尝试
    attempts = [
        # 方式1：Cookie（最常见）
        {"name": "cookie", "headers": base_headers_json, "cookies": {"token": req.token}},
        # 方式2：Bearer + hcminner（hcminner 头来自 hcm_whitelist.json，改了会连不上平台）
        {"name": "bearer_hcminner",
         "headers": {**base_headers_json, "Authorization": f"Bearer {req.token}", _HCM_HCMINNER_HEADER: _HCM_HCMINNER_VALUE}},
        # 方式3：Header token（有些部署是 x-token / 纯 token header）
        {"name": "header_token",
         "headers": {**base_headers_json, "token": req.token}},
    ]

    def _client_kwargs():
        kw = dict(timeout=30, follow_redirects=True)
        if req.proxy:
            kw["proxy"] = req.proxy
        else:
            kw["transport"] = httpx.AsyncHTTPTransport()
        return kw

    logger.info(f"[CF] 查询: base={base} model=dynamic_log log_type={req.log_type or '(全部)'} page_size={req.page_size} proxy={req.proxy or '(直连)'}")

    last_error = None
    for i, att in enumerate(attempts):
        try:
            logger.info(f"[CF] 尝试方式{i+1}: {att['name']}")
            client_kw = _client_kwargs()
            if "cookies" in att:
                client_kw["cookies"] = att["cookies"]
            async with httpx.AsyncClient(**client_kw) as client:
                resp = await client.post(url, json=payload, headers=att["headers"])
                logger.info(f"[CF] 方式{i+1} 响应: status={resp.status_code} len={len(resp.content)}")
                if resp.status_code == 405:
                    last_error = HTTPException(resp.status_code, f"[{att['name']}] HTTP 405 Method Not Allowed: {resp.text[:300]}")
                    continue  # 方法不对，换下一种
                if resp.status_code >= 400:
                    # 解析 CF 错误响应：{errcode, errmsg, description} 或 {success, message}
                    try:
                        err_body = resp.json()
                    except ValueError:
                        err_body = None
                    eb = err_body if isinstance(err_body, dict) else {}
                    errcode = eb.get("errcode")
                    errmsg = eb.get("errmsg") or eb.get("description") or eb.get("message") or resp.text[:400]
                    # 80001 model 不存在：换认证方式也无法解决，直接给出明确提示
                    if errcode == 80001 or (isinstance(errmsg, str) and "Unknown Model Name" in errmsg):
                        raise HTTPException(400, f"[{att['name']}] {errmsg}（model「dynamic_log」在当前部署/租户不存在，请确认日志 model 名或租户是否启用云函数日志）")
                    # 17003 执行错误 / 未登录类：通常是会话上下文失效，换认证方式再试
                    session_like = errcode == 17003 or any(k in str(errmsg) for k in ("未登录", "登录过期", "未授权", "unauthorized", "请先登录"))
                    if session_like or resp.status_code in (401, 403):
                        hint = "（token 可能已失效，建议重新登录获取 Token）" if errcode == 17003 else ""
                        last_error = HTTPException(resp.status_code, f"[{att['name']}] HTTP {resp.status_code}: {errmsg[:400]}{hint}")
                        continue
                    raise HTTPException(resp.status_code, f"[{att['name']}] HTTP {resp.status_code}: {errmsg[:400] or resp.text[:400]}")
                try:
                    data = resp.json()
                except ValueError:
                    raw = resp.text[:1000]
                    raise HTTPException(502, f"[{att['name']}] 返回非JSON: {raw[:600]}")
                # 业务失败判断（兼容 {success:false,...} 与 {errcode:...} 两种格式）
                biz_fail = (isinstance(data, dict) and data.get("success") is False) or \
                           (isinstance(data, dict) and data.get("errcode") and data.get("errcode") != 0)
                if biz_fail:
                    msg = (
                        data.get("errmsg") or data.get("description") or data.get("message") or data.get("msg") or
                        (isinstance(data.get("result"), dict) and data["result"].get("message")) or
                        str(data)[:500]
                    )
                    # 80001 model 不存在直接提示
                    if data.get("errcode") == 80001 or "Unknown Model Name" in str(msg):
                        raise HTTPException(400, f"[{att['name']}] {msg}（model「dynamic_log」在当前部署/租户不存在）")
                    # 常见 "未登录/登录过期/17003执行错误" 等 — 继续下一种方式
                    if data.get("errcode") == 17003 or any(k in str(msg) for k in ("未登录", "登录过期", "未授权", "unauthorized", "请先登录")):
                        last_error = HTTPException(401, f"[{att['name']}] 业务失败: {msg}（token 可能已失效，建议重新登录获取 Token）")
                        continue
                    raise HTTPException(400, f"[{att['name']}] 业务失败: {msg}")
                logger.info(f"[CF] 方式{i+1} 成功")
                if isinstance(data, dict) and "result" in data:
                    return {"method": att["name"], "raw": data, "data": data["result"]}
                return {"method": att["name"], **data} if isinstance(data, dict) else data
        except httpx.ConnectError as e:
            last_error = HTTPException(502, f"[{att['name']}] 无法连接服务器 {base}: {e}")
            # 连接失败所有方式都失败 — 直接中断
            raise last_error
        except httpx.TimeoutException as e:
            last_error = HTTPException(504, f"[{att['name']}] 请求超时: {e}")
            continue
        except HTTPException as e:
            last_error = e
            if (i == len(attempts) - 1):
                raise
            continue
        except Exception as e:
            logger.exception(f"[CF] 方式{i+1} 异常: {e}")
            last_error = HTTPException(500, f"[{att['name']}] {type(e).__name__}: {e}")
            if i == len(attempts) - 1:
                raise last_error

    raise last_error if last_error else HTTPException(500, "查询失败，未知错误")


@app.post("/api/cf/logs/export")
async def api_cf_logs_export(req: CfLogExportReq):
    """将查询到的 CF 云函数日志导出为本地 JSON 文件，供 AI 分析系统运行问题。

    写入 logs/cf_logs/cf_logs_<log_type>_<timestamp>.json，返回绝对路径。
    注：旧版本目录名为 logs/hcm_logs/，为兼容历史日志同时查询两处。
    """
    from datetime import datetime
    if not req.rows:
        raise HTTPException(400, "无可导出的日志数据")
    export_dir = _PROJECT_ROOT / "logs" / "cf_logs"
    export_dir.mkdir(parents=True, exist_ok=True)
    safe_log_type = "".join(c if c.isalnum() or c in "-_" else "_" for c in (req.log_type or "unknown"))[:60]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"cf_logs_{safe_log_type}_{ts}.json"
    fpath = export_dir / fname
    out = {
        "export_info": {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "CF 云函数日志 (dynamic_log)",
            "server_url": req.server_url,
            "log_type": req.log_type,
            "auth_method": req.auth_method,
            "page_index": req.page_index,
            "page_size": req.page_size,
            "total": req.total,
            "returned_count": len(req.rows),
        },
        "logs": req.rows,
    }
    if req.raw is not None:
        out["raw_response"] = req.raw
    content = json.dumps(out, ensure_ascii=False, indent=2)
    try:
        fpath.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.exception(f"[CF] 导出日志写入失败: {e}")
        raise HTTPException(500, f"写入文件失败: {e}")
    logger.info(f"[CF] 日志已导出: {fpath} ({len(req.rows)} 条)")
    return {"ok": True, "path": str(fpath), "filename": fname, "count": len(req.rows), "content": content}


@app.post("/api/cf/clipboard-save")
async def api_cf_clipboard_save(req: ClipboardSaveReq):
    """将剪贴板文本内容保存为本地文件，返回文件路径。

    写入 logs/cf_clipboard/ 目录，文件名自动生成或使用指定名称。
    注：旧版本目录名为 logs/hcm_clipboard/，为兼容历史剪贴板文件同时查询两处。
    """
    if not req.text or not req.text.strip():
        raise HTTPException(400, "剪贴板内容为空")
    from datetime import datetime
    export_dir = _PROJECT_ROOT / "logs" / "cf_clipboard"
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in (req.filename or "").strip())[:80]
    if not safe_name:
        safe_name = f"clipboard_{ts}.txt"
    elif not safe_name.endswith((".txt", ".json", ".log", ".md")):
        safe_name += ".txt"
    fpath = export_dir / safe_name
    try:
        fpath.write_text(req.text, encoding="utf-8")
    except Exception as e:
        logger.exception(f"[CF] 剪贴板文件写入失败: {e}")
        raise HTTPException(500, f"写入文件失败: {e}")
    logger.info(f"[CF] 剪贴板已保存: {fpath} ({len(req.text)} chars)")
    return {"ok": True, "path": str(fpath), "filename": safe_name, "size": len(req.text)}


@app.post("/api/cf/login")
async def api_cf_login(req: CfLoginReq):
    """使用账号密码登录 CF 平台，获取 token。

    登录接口：POST /login (form data, NOT JSON)
    字段：mobile / password / pure_result=true / transfer_strategy=no / un_redirect=true
    Token 返回在 response body 的 token 字段中（pure_result=true 时）。
    """
    if not req.server_url or not req.mobile or not req.password:
        raise HTTPException(400, "请填写服务器地址、手机号和密码")

    base = req.server_url.rstrip("/")
    url = f"{base}/login"
    logger.info(f"[CF] 发起登录: url={url} mobile={req.mobile} proxy={req.proxy or '(直连)'} need_captcha={bool(req.image_code)}")
    try:
        # 如果有 captcha_id，复用同一会话 cookie jar 与 image_code_index
        jar_override = None
        cached_index = ""
        if req.captcha_id and req.captcha_id in _CF_CAPTCHA_CACHE:
            entry = _CF_CAPTCHA_CACHE[req.captcha_id]
            jar_override = entry.get("jar") if isinstance(entry, dict) else entry
            cached_index = entry.get("index", "") if isinstance(entry, dict) else ""
            _CF_CAPTCHA_CACHE.pop(req.captcha_id, None)
            _CF_CAPTCHA_TTL.pop(req.captcha_id, None)
        # image_code_index 优先用前端显式传入的，否则用拉取验证码时缓存的
        image_code_index = req.image_code_index or cached_index
        kwargs = dict(timeout=15, follow_redirects=True)
        if req.proxy:
            kwargs["proxy"] = req.proxy
        else:
            kwargs["transport"] = httpx.AsyncHTTPTransport()
        if jar_override is not None:
            kwargs["cookies"] = jar_override
        # 登录表单：带图片验证码参数（如果有），参考 hcm-cloud-vue baseservices.js login()
        form_data = {
            "mobile": req.mobile,
            "password": req.password,
            "pure_result": "true",
            "transfer_strategy": "no",
            "un_redirect": "true",
            "mode": "PWD",
        }
        if req.image_code:
            form_data["image_code"] = req.image_code
        if image_code_index:
            form_data["image_code_index"] = image_code_index
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(url, data=form_data)
            logger.info(f"[CF] 登录响应: status={resp.status_code} content-type={resp.headers.get('content-type')} len={len(resp.content)}")
            if resp.status_code >= 400:
                detail = resp.text[:800]
                logger.error(f"[CF] 登录失败 HTTP {resp.status_code}: {detail}")
                raise HTTPException(resp.status_code, f"CF服务器返回HTTP {resp.status_code}：{detail}")
            try:
                data = resp.json()
            except ValueError as e:
                raw = resp.text[:1200]
                logger.error(f"[CF] 登录返回非JSON: {raw[:300]}")
                raise HTTPException(502, f"登录返回非JSON内容（可能是登录页HTML）: {raw[:500]}")
            # 登录失败（如账号密码错误、需图片验证码）：透传 need_img_valid/message，前端据此拉验证码
            if isinstance(data, dict) and data.get("success") is False:
                msg = data.get("message", "登录失败")
                need_img = bool(data.get("need_img_valid"))
                logger.warning(f"[CF] 登录被拒: status={data.get('status')} need_img_valid={need_img} msg={msg}")
                return JSONResponse({"ok": False, "need_img_valid": need_img, "message": msg})
            token = ""
            if isinstance(data, dict):
                token = data.get("token", "")
                if not token and isinstance(data.get("result"), dict):
                    token = data["result"].get("token", "")
            if not token:
                for cookie in resp.cookies.jar:
                    if cookie.name == "token":
                        token = cookie.value
                        break
            if not token:
                logger.error(f"[CF] 登录成功但未取到token，响应: {str(data)[:500]}")
                raise HTTPException(500, f"登录成功但未获取到 token，响应内容: {str(data)[:500]}")
            logger.info(f"[CF] 登录成功，token长度={len(token)}")
            return {"ok": True, "token": token, "server_url": base}
    except httpx.ConnectError as e:
        logger.error(f"[CF] 登录连接失败: {e}")
        raise HTTPException(502, f"无法连接服务器 {base}: {e}")
    except httpx.TimeoutException as e:
        logger.error(f"[CF] 登录超时: {e}")
        raise HTTPException(504, "登录请求超时")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[CF] 登录异常: {e}")
        raise HTTPException(500, f"登录失败: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
#  静态前端（优先 web-react/dist，回退 web/）
#  React 版产物（vite build --base /web/）输出到 web-react/dist，
#  与原生 web/ 一样经 app.mount("/web", ...) 提供，路径语义完全一致。
# --------------------------------------------------------------------------- #
WEB_DIR = _PROJECT_ROOT / "web-react" / "dist"
if not WEB_DIR.exists():
    WEB_DIR = _PROJECT_ROOT / "web"
if WEB_DIR.exists():
    class _NoCacheStaticFiles(StaticFiles):
        """禁用浏览器/中间缓存的静态文件提供器，避免前端改完还加载旧文件。"""
        def file_response(self, *a, **kw):
            resp = super().file_response(*a, **kw)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
    app.mount("/web", _NoCacheStaticFiles(directory=str(WEB_DIR), html=True), name="web")


@app.get("/")
async def index():
    """默认返回 Web 前端首页。"""
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        resp = FileResponse(str(index_path))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    return JSONResponse({"msg": "Web frontend not found. API is running at /api/"})


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Jira Git GUI API Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("API Server 启动")
    logger.info("Python  : %s", sys.version.replace("\n", " "))
    logger.info("监听    : http://%s:%d", args.host, args.port)
    logger.info("=" * 60)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


# --------------------------------------------------------------------------- #
#  K8s 运维路由（api/routes_k8s.py）
#  必须在 main() 之前 include：main() 里 uvicorn.run 是阻塞的，放后面永不注册。
# --------------------------------------------------------------------------- #
from api.routes_k8s import router as k8s_router
app.include_router(k8s_router)


if __name__ == "__main__":
    main()
