# -*- coding: utf-8 -*-
"""克隆 / 下载 / 取消 / 限速 / 会话 / 断点续传 路由。

后台任务统一在 daemon 线程中执行，通过 SSE（api.eventbus.broadcast）推送进度，
主线程立即返回 {"started": True}，避免阻塞前端请求。
"""
import asyncio
import threading

from fastapi import HTTPException

from fastapi import APIRouter
from api.common import (
    app, client, logger,
    download_cancel, task_status,
    log_callback, progress_callback, make_should_cancel,
)
from core.config import clear_session
from core.constants import DOWNLOAD_DIR
from core.client import NetworkWatchdog
from api.schemas import (
    CloneReq, DownloadReq, DownloadRepoReq, RateLimitReq,
)

router = APIRouter()


@router.post("/api/clone")
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
        task_status["running"] = True
        task_status["type"] = "clone"
        try:
            ok, msg, path = client.clone_repo(
                repo_id, repo_name, branch,
                client.config.pat, client.config.username,
                on_log=log_callback,
            )
            broadcast("clone_done", {"ok": ok, "msg": msg, "path": path})
        except Exception as ex:
            broadcast("clone_done", {"ok": False, "msg": str(ex), "path": None})
            logger.error("克隆异常", exc_info=True)
        finally:
            task_status["running"] = False
            task_status["type"] = None

    threading.Thread(target=_do_clone, daemon=True).start()
    return {"started": True}


@router.post("/api/download")
async def api_download(req: DownloadReq):
    """下载选中文件（Cookie 模式）。通过 SSE 推送进度。"""
    if not client.config.cookie:
        raise HTTPException(400, "下载功能仅 Cookie 模式可用")
    if not req.paths:
        raise HTTPException(400, "未勾选任何文件")

    download_cancel.clear()
    watchdog = NetworkWatchdog(threshold=5)
    client._watchdog = watchdog
    should_cancel = make_should_cancel(download_cancel, watchdog, "下载")

    def _do_download():
        task_status["running"] = True
        task_status["type"] = "download"
        try:
            ok_list, fail_list, dest, skipped = client.download(
                req.paths,
                max_workers=req.max_workers,
                on_log=log_callback,
                on_progress=progress_callback,
                should_cancel=should_cancel,
            )
            broadcast("download_done", {
                "ok_count": len(ok_list),
                "fail_count": len(fail_list),
                "dest": str(dest) if dest else None,
                "skipped": skipped,
                "fails": fail_list,
            })
        except Exception as ex:
            broadcast("download_done", {
                "ok_count": 0, "fail_count": 0, "dest": None,
                "skipped": 0, "error": str(ex),
            })
            logger.error("下载异常", exc_info=True)
        finally:
            client._watchdog = None
            task_status["running"] = False
            task_status["type"] = None

    threading.Thread(target=_do_download, daemon=True).start()
    return {"started": True}


@router.post("/api/download/repo")
async def api_download_repo(req: DownloadRepoReq):
    """下载整个仓库（Cookie 模式）。通过 SSE 推送进度。"""
    if not client.config.cookie:
        raise HTTPException(400, "整库下载仅 Cookie 模式可用")
    repo_id = req.repo_id or client.repo_id
    if not repo_id:
        raise HTTPException(400, "请先指定仓库")

    download_cancel.clear()
    watchdog = NetworkWatchdog(threshold=5)
    client._watchdog = watchdog
    should_cancel = make_should_cancel(download_cancel, watchdog, "整库下载")

    def _do_download():
        task_status["running"] = True
        task_status["type"] = "download_repo"
        try:
            ok_count, fail_list, dest, skipped = client.download_repo(
                repo_id, req.branch or client.branch,
                max_workers=req.max_workers,
                on_log=log_callback,
                on_progress=progress_callback,
                should_cancel=should_cancel,
            )
            broadcast("download_done", {
                "ok_count": ok_count,
                "fail_count": len(fail_list),
                "dest": str(dest) if dest else None,
                "skipped": skipped,
                "fails": fail_list[:20],
                "total_fails": len(fail_list),
            })
        except Exception as ex:
            broadcast("download_done", {
                "ok_count": 0, "fail_count": 0, "dest": None,
                "skipped": 0, "error": str(ex),
            })
            logger.error("整库下载异常", exc_info=True)
        finally:
            client._watchdog = None
            task_status["running"] = False
            task_status["type"] = None

    threading.Thread(target=_do_download, daemon=True).start()
    return {"started": True}


@router.post("/api/download/cancel")
async def api_cancel_download():
    """取消当前下载。"""
    download_cancel.set()
    return {"ok": True}


@router.post("/api/rate-limit")
async def api_set_rate_limit(req: RateLimitReq):
    """设置请求速率上限。"""
    client.set_rate_limit(req.qps)
    return {"ok": True, "qps": req.qps}


@router.delete("/api/session")
async def api_clear_session():
    """清除本地保存的 Cookie 会话。"""
    clear_session()
    return {"ok": True, "msg": "已清除本地 Cookie 会话"}


@router.delete("/api/resume")
async def api_clear_resume():
    """清空断点续传清单。"""
    if not client.repo_id:
        raise HTTPException(400, "请先选择仓库")
    mp = DOWNLOAD_DIR / str(client.repo_id) / client._MANIFEST_NAME
    if mp.exists():
        try:
            mp.unlink()
            return {"ok": True, "msg": f"已清空断点续传清单：{mp}"}
        except Exception as ex:
            raise HTTPException(500, f"清空断点失败：{ex}")
    return {"ok": True, "msg": "当前没有断点续传清单（无需清空）"}
