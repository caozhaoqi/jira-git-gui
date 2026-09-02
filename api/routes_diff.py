# -*- coding: utf-8 -*-
"""目录差异扫描 / 文件预览 / 批量合并 / 缓存失效 / 本地目录发现 路由。

业务实现基本 1:1 搬运自原 server.py 的差异对比 & 合并段，仅将 @app.* 改为
router.*，并收敛 import 到 api.common。行为、SSE 事件、/api/* 路径均保持不变。
"""
import asyncio
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from fastapi import APIRouter
from api.common import (
    app, client, logger, broadcast,
    task_status,
    make_should_cancel, DEFAULT_DOWNLOAD_WORKERS,
    NetworkWatchdog, commit_to_dict,
)
from core import differ as _differ
from core import cache as _cache
from core.config import load_merge_config

router = APIRouter()


class DiffScanReq(BaseModel):
    local_dir: str
    repo_name: str = ""
    repo_id: str = ""        # 对比仓库（来自 /api/repos）；提供则先 set_repo 再扫描
    branch: str = ""
    compare_dir: str = ""    # 限定扫描范围的子目录（远端+本地同时生效）
    use_cache: bool = True
    ignore_line_endings: bool = True
    fast_scan: bool = True   # 默认按大小快扫（不下载内容）；False=精确(下载内容比对)


class DiffFileReq(BaseModel):
    local_dir: str
    path: str
    compare_dir: str = ""    # 远端/本地范围前缀（与扫描时一致）
    use_cache: bool = True


class MergeReq(BaseModel):
    local_dir: str
    path: str
    compare_dir: str = ""    # 远端/本地范围前缀（与扫描时一致）
    use_cache: bool = True
    status: str = ""  # 对应 DiffStatus，供批量合并时按状态过滤


@router.post("/api/diff/scan")
async def api_diff_scan(req: DiffScanReq):
    """扫描本地目录和远程仓库，返回差异列表（缓存优先）。

    支持：
    - ``compare_dir``：同时限定远程(list_level path)与本地(子目录)扫描范围，路径归一为
      相对 compare_dir 的相对路径使两侧对齐；既实现「目录选择」又直接加速扫描。
    - ``fast_scan``：默认按文件大小快扫（不下载内容算 md5），秒级出差异；文件详情/合并时
      再按需下载内容。提供精确开关（fast_scan=False）做内容级比对。
    - ``merged``：每条差异附带 merged 标记（来自 merge_manifest，标识已合并且内容一致）。
    """
    import os
    local_dir = req.local_dir.strip()
    if not os.path.isdir(local_dir):
        broadcast("scan_error", {"message": f"本地目录不存在：{local_dir}"})
        raise HTTPException(400, f"本地目录不存在：{local_dir}")

    # 对比仓库：若前端显式指定 repo_id，则先选定再扫描（不再只依赖全局 selectedRepo）
    compare_dir = req.compare_dir.strip().strip("/")
    if req.repo_id:
        try:
            client.set_repo(req.repo_id, req.repo_name, req.branch)
        except Exception as ex:
            logger.warning("选定对比仓库失败：%s", ex)

    if not client.repo_id:
        broadcast("scan_error", {"message": "请先选择远程仓库"})
        raise HTTPException(400, "请先选择远程仓库")

    # 本地扫描基准目录 = local_dir / compare_dir
    local_base = os.path.join(local_dir, compare_dir) if compare_dir else local_dir
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
            local_files = _differ.scan_local_cached(local_base, use_cache=req.use_cache)
            broadcast("scan_stage", {
                "stage": "remote", "message": "正在扫描远程文件…",
                "local_count": len(local_files), "pct": 10,
            })

            def _on_remote_progress(scanned, pending, processed, dirs_seen):
                ratio = min(1.0, processed / max(dirs_seen, 1))
                pct = round(10 + 70 * ratio)
                broadcast("scan_progress", {
                    "done": scanned, "total": dirs_seen, "pct": pct,
                    "pending_dirs": pending,
                    "message": f"已扫描 {scanned} 个文件，{pending} 个目录待扫",
                })

            # 远端按 compare_dir 范围扫描；fast_hash 决定是否下载内容算 md5
            remote_files = _differ.scan_remote_cached(
                client, namespace, max_workers=3, tree_ttl=3600,
                on_progress=_on_remote_progress, use_cache=req.use_cache,
                should_cancel=should_cancel, path=compare_dir,
                fast_hash=req.fast_scan,
            )
            # 归一化为相对 compare_dir 的相对路径，与本地对齐
            prefix = (compare_dir + "/") if compare_dir else ""
            remote_rel: dict = {}
            for full, meta in remote_files.items():
                if compare_dir and full == compare_dir:
                    continue
                rel = full[len(prefix):] if (prefix and full.startswith(prefix)) else full
                remote_rel[rel] = meta

            broadcast("scan_stage", {
                "stage": "diff", "message": "正在计算差异…",
                "local_count": len(local_files), "remote_count": len(remote_rel), "pct": 90,
            })
            result = _differ.compute_diff(
                local_files, remote_rel, ignore_line_endings=req.ignore_line_endings)

            # 合并记录：标记哪些文件已经合并且本地内容仍一致
            manifest = _differ.load_manifest(local_base)
            broadcast("scan_done", {"summary": result.summary()})
            return result, manifest
        finally:
            client._watchdog = None

    try:
        res = await asyncio.to_thread(_scan)
        result, manifest = res
    except Exception as ex:
        broadcast("scan_error", {"message": str(ex)})
        raise HTTPException(500, f"扫描失败：{ex}")

    def _entry_merged(path: str) -> bool:
        """该文件是否已合并且内容一致（精确模式）；快扫无远端 hash，退化为「曾合并过」标记。"""
        rec = manifest.get(path)
        if not rec or not rec.get("ok"):
            return False
        if req.fast_scan:
            return True
        return _differ.is_already_merged(local_base, path, manifest)

    entries = [
        {"path": e.path, "status": e.status.value,
         "local_size": e.local_size, "remote_size": e.remote_size,
         "merged": _entry_merged(e.path)}
        for e in result.entries if e.status != _differ.DiffStatus.SAME
    ]
    merged_count = sum(1 for e in entries if e["merged"])
    return {
        "summary": result.summary(),
        "entries": entries,
        "cached": req.use_cache,
        "compare_dir": compare_dir,
        "local_base": local_base,
        "fast_scan": req.fast_scan,
        "merged_count": merged_count,
    }


@router.post("/api/diff/file")
async def api_diff_file(req: DiffFileReq):
    """获取单个文件的 unified diff（远程内容缓存优先）。"""
    import os
    compare_dir = req.compare_dir.strip().strip("/")
    local_base = os.path.join(req.local_dir, compare_dir) if compare_dir else req.local_dir
    local_path = os.path.join(local_base, req.path)
    if not os.path.isfile(local_path):
        local_content = ""
    else:
        local_content = Path(local_path).read_text(encoding="utf-8", errors="replace")

    namespace = str(client.repo_id) if client.repo_id else "default"
    # ⚠️ 签名是 get_file_cached(client, path, namespace, ttl, use_cache)。
    # 之前把 namespace 与 path 写反：拿 namespace（如 "1596"）当文件路径去抓取，
    # 必然失败 → remote_content 为 None → 退化成空字符串与本地比对
    # → 本地内容被判为「整篇删除」，差异对比显示全是差异。
    # 远端路径需拼上 compare_dir 前缀，才是相对于仓库根的完整路径。
    remote_full = (compare_dir + "/" + req.path) if compare_dir else req.path
    remote_content = await asyncio.to_thread(
        _differ.get_file_cached, client, remote_full, namespace, 86400, req.use_cache)

    if remote_content is None:
        # 远端内容取不到时不要伪装成空内容参与比对（会被判为全量差异），
        # 明确抛出错误让前端提示「远端内容获取失败」。
        raise HTTPException(502, f"远端内容获取失败：{req.path}"
                                 f"（请检查仓库/分支选择与 Cookie 是否有效）")

    diff_text = _differ.file_diff(local_path, remote_content)
    normalized_same = _differ.is_whitespace_only_diff(local_path, local_content, remote_content)
    show_local = _differ.canonical_text(local_path, local_content) if local_content else ""
    show_remote = _differ.canonical_text(req.path, remote_content or "") if remote_content else ""

    return {
        "path": req.path, "diff": diff_text,
        "local_content": show_local, "remote_content": show_remote,
        "normalized_same": normalized_same, "cached": req.use_cache,
    }


@router.post("/api/diff/merge")
async def api_diff_merge(req: MergeReq):
    """将远程文件合并到本地（远程内容缓存优先）。

    单文件合并同样支持合并记录跳过：若 manifest 记录该文件已合并且本次远端内容
    与记录一致，则直接返回 ok(skipped)，不再覆盖本地（实现「只获取需要合并的内容」）。
    """
    import os
    compare_dir = req.compare_dir.strip().strip("/")
    local_base = os.path.join(req.local_dir, compare_dir) if compare_dir else req.local_dir
    namespace = str(client.repo_id) if client.repo_id else "default"
    # ⚠️ 签名是 get_file_cached(client, path, namespace, ttl, use_cache)。
    # 之前把 namespace 与 path 写反：拿 namespace（如 "1596"）当文件路径去抓取，
    # 必然失败 → remote_content 为 None → 退化成空字符串写入本地
    # → merge_to_local 的 open(target, "w") 会把本地文件清空（静默数据丢失！）。
    remote_full = (compare_dir + "/" + req.path) if compare_dir else req.path
    remote_content = await asyncio.to_thread(
        _differ.get_file_cached, client, remote_full, namespace, 86400, req.use_cache, "", True)

    if remote_content is None:
        # 远端取不到内容时绝不能写入：空串会把本地文件截断为 0 字节。
        raise HTTPException(502, f"远端内容获取失败：{remote_full}"
                                 f"（已中止合并，本地文件未被修改）")

    # manifest 跳过：仅当本地仍与已合并记录一致时才跳过（不覆盖本地）。
    # 若本地被改动（local hash != remote_hash），即便远端没变也要重新抓取并覆盖，
    # 把本地拉回远端状态 —— 这正是「断点续传只跳过仍一致文件」的语义。
    manifest = _differ.load_manifest(local_base)
    if _differ.is_already_merged(local_base, req.path, manifest):
        return {"ok": True, "path": req.path, "skipped": True, "reason": "已合并且本地内容一致"}

    ok = _differ.merge_to_local(local_base, req.path, remote_content)
    if not ok:
        raise HTTPException(500, f"写入本地失败：{req.path}（可能权限不足）")
    # 落盘合并记录，供下次扫描/合并识别「已同步」
    manifest[req.path] = {"ok": True, "remote_hash": _differ.content_hash(remote_content)}
    _differ.save_manifest(local_base, manifest)
    return {"ok": ok, "path": req.path}


@router.post("/api/diff/merge-batch")
async def api_diff_merge_batch(reqs: list[MergeReq], status_filter: str = ""):
    """批量合并多个文件（缓存优先，并行抓取 + 并行写入，支持断点续传）。

    断点续传：合并前加载上次落盘的 manifest（按 local_dir 隔离，存于应用数据目录
    sidecar，不写入用户仓库），过滤掉「manifest 标记成功且本地内容仍与记录 hash 一致」
    的文件——这些文件**不重新抓取、不重写**，直接计入已完成（省掉「重抓缓存」那一步）。
    每次合并完成后把每条结果（ok / 远端内容 hash）写回 manifest，供下次续传。
    """
    if status_filter:
        filters = {s.strip() for s in status_filter.split(",") if s.strip()}
        reqs = [r for r in reqs if r.status in filters]

    if not reqs:
        broadcast("merge_done", {"ok_count": 0, "fail_count": 0, "fails": [], "skipped": 0})
        return {"results": []}

    namespace = str(client.repo_id) if client.repo_id else "default"
    # 前端所有 req 共享同一 local_dir / compare_dir，取首个即可
    local_dir = reqs[0].local_dir
    compare_dir = (reqs[0].compare_dir or "").strip().strip("/")
    local_base = os.path.join(local_dir, compare_dir) if compare_dir else local_dir

    # ---- 断点续传：筛出可跳过的条目（仅 use_cache 且本地内容仍一致）----
    manifest = _differ.load_manifest(local_base)
    skipped: list[str] = []
    pending: list[MergeReq] = []
    for r in reqs:
        if r.use_cache and _differ.is_already_merged(local_base, r.path, manifest):
            skipped.append(r.path)
        else:
            pending.append(r)

    orig_total = len(reqs)
    skipped_count = len(skipped)
    total = len(pending)

    broadcast("merge_start", {"total": orig_total, "skipped": skipped_count})
    _differ.clear_dir_cache()

    # 先把续传命中的条目广播为已完成，使进度条直接推进
    done_counter = skipped_count
    for p in skipped:
        broadcast("merge_progress", {
            "done": done_counter, "total": orig_total,
            "pct": (done_counter * 100 // orig_total) if orig_total else 100,
            "path": p, "ok": True, "skipped": True,
        })

    if total == 0:
        # 全部命中续传，无需抓取/写入
        _differ.save_manifest(local_base, manifest)
        broadcast("merge_done", {"ok_count": orig_total, "fail_count": 0, "fails": [], "skipped": skipped_count})
        return {"results": [{"path": p, "ok": True, "skipped": True} for p in skipped], "skipped": skipped_count}

    FETCH_WORKERS = min(12, max(4, DEFAULT_DOWNLOAD_WORKERS * 2))
    WRITE_WORKERS = 20

    pipe: "asyncio.Queue[tuple[int, MergeReq, str, Optional[str]]]" = asyncio.Queue(
        maxsize=FETCH_WORKERS * 2)
    counter_lock = asyncio.Lock()

    async def _fetch(idx: int, req: MergeReq):
        err: Optional[str] = None
        content: Optional[bytes] = None
        try:
            # ⚠️ 签名是 get_file_cached(client, path, namespace, ttl, use_cache)，
            # 顺序写反会把 namespace 当路径抓取，详见 api_diff_file 处的说明。
            # 远端路径需拼上 compare_dir 前缀，才是相对仓库根的完整路径。
            remote_full = (compare_dir + "/" + req.path) if compare_dir else req.path
            content = await asyncio.to_thread(
                _differ.get_file_cached, client, remote_full, namespace, 86400, req.use_cache, "", True)
            if content is None:
                err = f"远端内容获取失败：{remote_full}"
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
                        # 合并记录跳过：仅当本地仍与已合并记录一致时才跳过（不覆盖本地）；
                        # 本地被改动则重新抓取并覆盖，把本地拉回远端状态。
                        if _differ.is_already_merged(local_base, req.path, manifest):
                            ok = True
                        else:
                            ok = _differ.merge_to_local(local_base, req.path, content)
                    except Exception as ex:
                        ok = False
                        err = str(ex)
                # 落盘 manifest：成功记录远端内容 hash，失败标记 ok=False（下次重试）
                if ok:
                    manifest[req.path] = {
                        "ok": True,
                        "remote_hash": _differ.content_hash(content),
                    }
                else:
                    manifest[req.path] = {"ok": False, "remote_hash": ""}
                if not ok and err is None:
                    err = "写入失败（可能权限不足）"
                async with counter_lock:
                    done_counter += 1
                    cur = done_counter
                broadcast("merge_progress", {
                    "done": cur, "total": orig_total,
                    "pct": (cur * 100 // orig_total) if orig_total > 0 else 100,
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

    fetch_tasks = [asyncio.create_task(_fetch_limited(i, r)) for i, r in enumerate(pending)]
    try:
        await asyncio.gather(*fetch_tasks)
    except Exception as ex:
        logger.warning("merge batch gather(fetch) exception: %s", ex)

    await pipe.join()
    for _ in writer_tasks:
        await pipe.put((-1, None, None, None))
    await asyncio.gather(*writer_tasks, return_exceptions=True)

    # 续传命中项也并入结果，便于前端把已合并条目从列表移除
    for p in skipped:
        results.append({"path": p, "ok": True, "error": None, "skipped": True})

    ok_count = sum(1 for r in results if r and r["ok"])
    fail_count = len(results) - ok_count
    fails = [{"path": r["path"], "error": r["error"]}
             for r in results if r and not r["ok"]][:50]
    _differ.save_manifest(local_base, manifest)
    broadcast("merge_done", {"ok_count": ok_count, "fail_count": fail_count, "fails": fails, "skipped": skipped_count})
    return {"results": results, "skipped": skipped_count}


@router.post("/api/diff/invalidate")
async def api_diff_invalidate():
    """使当前仓库的缓存失效（强制下次重新拉取）。"""
    if not client.repo_id:
        raise HTTPException(400, "请先选择仓库")
    n = _cache.invalidate(str(client.repo_id))
    return {"ok": True, "cleared": n}


@router.get("/api/diff/commits")
async def api_diff_commits(path: str = "", limit: int = 30):
    """返回对比目录（或整个仓库）的最近更新记录（git log 风格）。

    底层走 ``client.get_commits``（Cookie 模式走插件 REST，本地克隆模式走 git log），
    支持 ``path`` 过滤——即「对比目录」范围内的最近提交，用于快速判断该目录是否有新改动。
    """
    if not client.repo_id:
        raise HTTPException(400, "请先选择远程仓库")
    try:
        commits = await asyncio.to_thread(
            client.get_commits, client.repo_id, client.branch, path, limit)
        return {"commits": [commit_to_dict(c) for c in commits]}
    except Exception as ex:
        logger.warning("获取提交记录失败：%s", ex)
        return {"commits": [], "error": str(ex)}


@router.get("/api/diff/merge-manifest")
async def api_diff_merge_manifest(local_dir: str = "", compare_dir: str = ""):
    """返回某本地目录（含对比子目录）的已合并记录（git 风格的「已同步」清单）。

    记录来自 merge_manifest（按 local_dir/compare_dir 隔离，存于应用数据目录 sidecar），
    供前端在扫描结果上标记「已合并」徽标，以及确认「只获取需要合并的内容」的续传状态。
    """
    import os
    cd = (compare_dir or "").strip().strip("/")
    base = os.path.join(local_dir, cd) if cd else (local_dir or "")
    manifest = _differ.load_manifest(base)
    return {
        "local_dir": base,
        "compare_dir": cd,
        "count": len(manifest),
        "entries": manifest,
    }


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
