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
import sys
import threading
import queue
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.client import JiraGitClient, DEFAULT_DOWNLOAD_WORKERS, NetworkWatchdog
from core.config import load_config, load_session, save_session, clear_session, get_session_path
from core.constants import DEFAULT_REQUEST_QPS, DOWNLOAD_DIR
from core.models import ConnectConfig, RepoInfo, TreeEntry, Commit, CommitFile

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
class ConnectReq(BaseModel):
    jira_url: str = ""
    username: str = ""
    mode: str = "pat"
    pat: str = ""
    cookie: str = ""
    repo_id: str = ""
    repo_name: str = ""
    branch: str = ""


class RepoSelectReq(BaseModel):
    repo_id: str
    repo_name: str = ""
    branch: str = ""


class CloneReq(BaseModel):
    repo_id: str = ""
    repo_name: str = ""
    branch: str = ""


class DownloadReq(BaseModel):
    paths: list[str]
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS


class DownloadRepoReq(BaseModel):
    repo_id: str = ""
    branch: str = ""
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS


class RateLimitReq(BaseModel):
    qps: int = DEFAULT_REQUEST_QPS


class CommitsReq(BaseModel):
    issue_key: str = ""
    local_mode: bool = False


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
    """设置连接配置并测试连通性。"""
    cfg = ConnectConfig(
        jira_url=req.jira_url.rstrip("/"),
        username=req.username,
        mode=req.mode,
        pat=req.pat,
        cookie=req.cookie,
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
    if req.mode == "cookie" and req.cookie:
        if result.get("cookieOk"):
            save_session(req.cookie, req.jira_url, req.username)
            result["cookieSaved"] = True
        else:
            result["cookieSaved"] = False
            result["cookieWarning"] = (
                "Cookie 验证失败，可能已过期。请重新从浏览器获取 Cookie 后再试。"
            )

    return result


@app.get("/api/repos")
async def api_discover_repos():
    """发现全部仓库（Cookie 模式）。"""
    if not client.config.cookie:
        return {"repos": [], "error": "未配置 Cookie"}
    try:
        repos = await asyncio.to_thread(client.discover_repos)
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

class DiffFileReq(_BM):
    local_dir: str
    path: str
    use_cache: bool = True

class MergeReq(_BM):
    local_dir: str
    path: str
    use_cache: bool = True

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
        try:
            # 阶段 1：本地扫描
            _broadcast("scan_stage", {"stage": "local", "message": "正在扫描本地文件…"})
            local_files = _differ.scan_local_cached(req.local_dir, use_cache=req.use_cache)
            _broadcast("scan_stage", {
                "stage": "remote", "message": "正在扫描远程文件…",
                "local_count": len(local_files),
            })

            # 阶段 2：远程扫描（带进度回调 + 看门狗）
            def _on_remote_progress(scanned, pending):
                _broadcast("scan_progress", {
                    "done": scanned,
                    "total": 0,  # 远程文件总数预先未知
                    "pct": 0,
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
            })

            # 阶段 3：差异计算
            result = _differ.compute_diff(local_files, remote_files)
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

    return {
        "path": req.path,
        "diff": diff_text,
        "local_content": local_content,
        "remote_content": remote_content or "",
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
async def api_diff_merge_batch(reqs: list[MergeReq]):
    """批量合并多个文件（缓存优先，并行抓取 + 写入）。

    通过 SSE 推送合并进度：
    - merge_start: {total}
    - merge_progress: {done, total, pct, path, ok, error?}
    - merge_done: {ok_count, fail_count, fails: [{path, error}]}

    并发策略：用 asyncio.Semaphore 限制同时进行的远程抓取（默认 6），
    避免一次性提交数百个请求把 Jira 服务器压垮。每完成一个文件即推送进度。
    """
    namespace = str(client.repo_id) if client.repo_id else "default"
    total = len(reqs)
    _broadcast("merge_start", {"total": total})

    sem = asyncio.Semaphore(6)  # 并发上限
    done_counter = 0
    counter_lock = asyncio.Lock()

    async def _merge_one(req: MergeReq):
        nonlocal done_counter
        err = None
        ok = False
        async with sem:
            try:
                remote_content = await asyncio.to_thread(
                    _differ.get_file_cached,
                    client, namespace, req.path,
                    86400, req.use_cache,
                )
                ok = _differ.merge_to_local(req.local_dir, req.path, remote_content or "")
            except Exception as ex:
                ok = False
                err = str(ex)
        if not ok and err is None:
            err = "写入失败（可能权限不足）"
        # 原子递增计数器并推送进度
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
        return {"path": req.path, "ok": ok, "error": err}

    results = await asyncio.gather(*[_merge_one(r) for r in reqs])
    ok_count = sum(1 for r in results if r["ok"])
    fail_count = total - ok_count
    fails = [{"path": r["path"], "error": r["error"]}
             for r in results if not r["ok"]][:50]
    _broadcast("merge_done", {"ok_count": ok_count, "fail_count": fail_count, "fails": fails})
    return {"results": results}


@app.post("/api/diff/invalidate")
async def api_diff_invalidate():
    """使当前仓库的缓存失效（强制下次重新拉取）。"""
    if not client.repo_id:
        raise HTTPException(400, "请先选择仓库")
    n = _cache.invalidate(str(client.repo_id))
    return {"ok": True, "cleared": n}


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
#  SSE 事件流
# --------------------------------------------------------------------------- #
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
#  静态前端（web/ 目录）
# --------------------------------------------------------------------------- #
WEB_DIR = _PROJECT_ROOT / "web"
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


@app.get("/")
async def index():
    """默认返回 Web 前端首页。"""
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
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


if __name__ == "__main__":
    main()
