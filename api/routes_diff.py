# -*- coding: utf-8 -*-
"""目录差异扫描 / 文件预览 / 批量合并 / 缓存失效 / 本地目录发现 路由。

业务实现基本 1:1 搬运自原 server.py 的差异对比 & 合并段，仅将 @app.* 改为
router.*，并收敛 import 到 api.common。行为、SSE 事件、/api/* 路径均保持不变。
"""
import asyncio
import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from fastapi import APIRouter
from api.common import (
    app, client, logger, broadcast,
    download_cancel, task_status,
    make_should_cancel, DEFAULT_DOWNLOAD_WORKERS,
    NetworkWatchdog,
)
from core import differ as _differ
from core import cache as _cache
from core.config import load_merge_config

router = APIRouter()


class DiffScanReq(BaseModel):
    local_dir: str
    repo_name: str = ""
    use_cache: bool = True
    ignore_line_endings: bool = True


class DiffFileReq(BaseModel):
    local_dir: str
    path: str
    use_cache: bool = True


class MergeReq(BaseModel):
    local_dir: str
    path: str
    use_cache: bool = True
    status: str = ""  # 对应 DiffStatus，供批量合并时按状态过滤


@router.post("/api/diff/scan")
async def api_diff_scan(req: DiffScanReq):
    """扫描本地目录和远程仓库，返回差异列表（缓存优先）。"""
    import os
    if not os.path.isdir(req.local_dir):
        broadcast("scan_error", {"message": f"本地目录不存在：{req.local_dir}"})
        raise HTTPException(400, f"本地目录不存在：{req.local_dir}")
    if not client.repo_id:
        broadcast("scan_error", {"message": "请先选择远程仓库"})
        raise HTTPException(400, "请先选择远程仓库")

    namespace = str(client.repo_id)
    scan_cancel = threading.Event()
    watchdog = NetworkWatchdog(threshold=5)
    client._watchdog = watchdog
    should_cancel = make_should_cancel(scan_cancel, watchdog, "差异扫描")

    def _scan():
        import core.throttle as _th
        saved_qps = _th.get_rate_limiter().qps
        try:
            _th.set_global_rate_limit(max(saved_qps, 20))
            return _scan_inner()
        finally:
            _th.set_global_rate_limit(saved_qps)

    def _scan_inner():
        try:
            broadcast("scan_stage", {"stage": "local", "message": "正在扫描本地文件…", "pct": 5})
            local_files = _differ.scan_local_cached(req.local_dir, use_cache=req.use_cache)
            broadcast("scan_stage", {
                "stage": "remote", "message": "正在扫描远程文件…",
                "local_count": len(local_files), "pct": 10,
            })
            mkdirs = [0]

            def _on_remote_progress(scanned, pending, processed, dirs_seen):
                ratio = min(1.0, processed / max(dirs_seen, 1))
                pct = round(10 + 70 * ratio)
                broadcast("scan_progress", {
                    "done": scanned, "total": dirs_seen, "pct": pct,
                    "pending_dirs": pending,
                    "message": f"已扫描 {scanned} 个文件，{pending} 个目录待扫",
                })

            remote_files = _differ.scan_remote_cached(
                client, namespace, max_workers=3, tree_ttl=3600,
                on_progress=_on_remote_progress, use_cache=req.use_cache,
                should_cancel=should_cancel,
            )
            broadcast("scan_stage", {
                "stage": "diff", "message": "正在计算差异…",
                "local_count": len(local_files), "remote_count": len(remote_files), "pct": 90,
            })
            result = _differ.compute_diff(
                local_files, remote_files, ignore_line_endings=req.ignore_line_endings)
            broadcast("scan_done", {"summary": result.summary()})
            return result
        finally:
            client._watchdog = None

    try:
        result = await asyncio.to_thread(_scan)
    except Exception as ex:
        broadcast("scan_error", {"message": str(ex)})
        raise HTTPException(500, f"扫描失败：{ex}")

    entries = [
        {"path": e.path, "status": e.status.value,
         "local_size": e.local_size, "remote_size": e.remote_size}
        for e in result.entries if e.status != _differ.DiffStatus.SAME
    ]
    return {"summary": result.summary(), "entries": entries, "cached": req.use_cache}


@router.post("/api/diff/file")
async def api_diff_file(req: DiffFileReq):
    """获取单个文件的 unified diff（远程内容缓存优先）。"""
    import os
    local_path = os.path.join(req.local_dir, req.path)
    if not os.path.isfile(local_path):
        local_content = ""
    else:
        local_content = Path(local_path).read_text(encoding="utf-8", errors="replace")

    namespace = str(client.repo_id) if client.repo_id else "default"
    # ⚠️ 签名是 get_file_cached(client, path, namespace, ttl, use_cache)。
    # 之前把 namespace 与 path 写反：拿 namespace（如 "1596"）当文件路径去抓取，
    # 必然失败 → remote_content 为 None → 退化成空字符串与本地比对
    # → 本地内容被判为「整篇删除」，差异对比显示全是差异。
    remote_content = await asyncio.to_thread(
        _differ.get_file_cached, client, req.path, namespace, 86400, req.use_cache)

    if remote_content is None:
        # 远端内容取不到时不要伪装成空内容参与比对（会被判为全量差异），
        # 明确抛出错误让前端提示「远端内容获取失败」。
        raise HTTPException(502, f"远端内容获取失败：{req.path}"
                                 f"（请检查仓库/分支选择与 Cookie 是否有效）")

    diff_text = _differ.file_diff(local_path, remote_content)
    normalized_same = _differ.is_whitespace_only_diff(local_path, remote_content or "")
    show_local = _differ.canonical_text(local_path, local_content) if local_content else ""
    show_remote = _differ.canonical_text(req.path, remote_content or "") if remote_content else ""

    return {
        "path": req.path, "diff": diff_text,
        "local_content": show_local, "remote_content": show_remote,
        "normalized_same": normalized_same, "cached": req.use_cache,
    }


@router.post("/api/diff/merge")
async def api_diff_merge(req: MergeReq):
    """将远程文件合并到本地（远程内容缓存优先）。"""
    namespace = str(client.repo_id) if client.repo_id else "default"
    # ⚠️ 签名是 get_file_cached(client, path, namespace, ttl, use_cache)。
    # 之前把 namespace 与 path 写反：拿 namespace（如 "1596"）当文件路径去抓取，
    # 必然失败 → remote_content 为 None → 退化成空字符串写入本地
    # → merge_to_local 的 open(target, "w") 会把本地文件清空（静默数据丢失！）。
    remote_content = await asyncio.to_thread(
        _differ.get_file_cached, client, req.path, namespace, 86400, req.use_cache)

    if remote_content is None:
        # 远端取不到内容时绝不能写入：空串会把本地文件截断为 0 字节。
        raise HTTPException(502, f"远端内容获取失败：{req.path}"
                                 f"（已中止合并，本地文件未被修改）")

    ok = _differ.merge_to_local(req.local_dir, req.path, remote_content)
    if not ok:
        raise HTTPException(500, f"写入本地失败：{req.path}（可能权限不足）")
    return {"ok": ok, "path": req.path}


@router.post("/api/diff/merge-batch")
async def api_diff_merge_batch(reqs: list[MergeReq], status_filter: str = ""):
    """批量合并多个文件（缓存优先，并行抓取 + 并行写入）。"""
    if status_filter:
        filters = {s.strip() for s in status_filter.split(",") if s.strip()}
        reqs = [r for r in reqs if r.status in filters]

    namespace = str(client.repo_id) if client.repo_id else "default"
    total = len(reqs)
    broadcast("merge_start", {"total": total})
    _differ.clear_dir_cache()

    FETCH_WORKERS = min(12, max(4, DEFAULT_DOWNLOAD_WORKERS * 2))
    WRITE_WORKERS = 20

    pipe: "asyncio.Queue[tuple[int, MergeReq, str, Optional[str]]]" = asyncio.Queue(
        maxsize=FETCH_WORKERS * 2)
    done_counter = 0
    counter_lock = asyncio.Lock()

    async def _fetch(idx: int, req: MergeReq):
        err: Optional[str] = None
        content: Optional[bytes] = None
        try:
            # ⚠️ 签名是 get_file_cached(client, path, namespace, ttl, use_cache)，
            # 顺序写反会把 namespace 当路径抓取，详见 api_diff_file 处的说明。
            content = await asyncio.to_thread(
                _differ.get_file_cached, client, req.path, namespace, 86400, req.use_cache)
            if content is None:
                err = f"远端内容获取失败：{req.path}"
        except Exception as ex:
            err = str(ex)
        await pipe.put((idx, req, content, err))

    async def _writer():
        nonlocal done_counter
        while True:
            item = await pipe.get()
            try:
                (idx, req, content, fetch_err) = item
                if req is None:
                    break
                ok = False
                err = fetch_err
                if err is None and content is None:
                    # 防御：content 为 None 意味着「没取到」，不是「远端就是空文件」。
                    # 若退化成 "" 写入，open(target,"w") 会把本地文件截断成 0 字节。
                    err = f"远端内容获取失败：{req.path}（已跳过写入）"
                if err is None:
                    try:
                        ok = _differ.merge_to_local(req.local_dir, req.path, content)
                    except Exception as ex:
                        ok = False
                        err = str(ex)
                if not ok and err is None:
                    err = "写入失败（可能权限不足）"
                async with counter_lock:
                    done_counter += 1
                    cur = done_counter
                broadcast("merge_progress", {
                    "done": cur, "total": total,
                    "pct": (cur * 100 // total) if total > 0 else 100,
                    "path": req.path, "ok": ok, "error": err,
                })
                results[idx] = {"path": req.path, "ok": ok, "error": err}
            finally:
                pipe.task_done()

    results: list[dict] = [None] * total  # type: ignore[list-item]
    writer_tasks = [asyncio.create_task(_writer()) for _ in range(WRITE_WORKERS)]
    fetch_sem = asyncio.Semaphore(FETCH_WORKERS)

    async def _fetch_limited(idx, req):
        async with fetch_sem:
            await _fetch(idx, req)

    fetch_tasks = [asyncio.create_task(_fetch_limited(i, r)) for i, r in enumerate(reqs)]
    try:
        await asyncio.gather(*fetch_tasks)
    except Exception as ex:
        logger.warning("merge batch gather(fetch) exception: %s", ex)

    await pipe.join()
    for _ in writer_tasks:
        await pipe.put((-1, None, None, None))
    await asyncio.gather(*writer_tasks, return_exceptions=True)

    ok_count = sum(1 for r in results if r and r["ok"])
    fail_count = total - ok_count
    fails = [{"path": r["path"], "error": r["error"]}
             for r in results if r and not r["ok"]][:50]
    broadcast("merge_done", {"ok_count": ok_count, "fail_count": fail_count, "fails": fails})
    return {"results": results}


@router.post("/api/diff/invalidate")
async def api_diff_invalidate():
    """使当前仓库的缓存失效（强制下次重新拉取）。"""
    if not client.repo_id:
        raise HTTPException(400, "请先选择仓库")
    n = _cache.invalidate(str(client.repo_id))
    return {"ok": True, "cleared": n}


@router.get("/api/diff/repo-mappings")
async def api_diff_repo_mappings():
    """返回 .env 中 MERGE_REPO_* 配置的远程仓库 → 本地目录映射。"""
    cfg = load_merge_config()
    mappings = [
        {"repo_name": name, "local_dir": local_dir}
        for name, local_dir in cfg["repo_map"].items()
    ]
    return {"mappings": mappings}


@router.get("/api/diff/discover-local-dirs")
async def api_diff_discover_local_dirs(repo_name: str = ""):
    """根据仓库名自动扫描本地候选目录（深度最多 2 层）。"""
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
                try:
                    for sub in os.scandir(entry):
                        if sub.is_dir():
                            sc2 = _score(sub.name)
                            if sc2:
                                candidates.append((sc2 - 10, sub.path))
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            continue

    seen: set[str] = set()
    result: list[str] = []
    for score, path in sorted(candidates, key=lambda x: -x[0]):
        if path not in seen:
            seen.add(path)
            result.append(path)
    return {"repo_name": repo_name, "candidates": result[:10]}
