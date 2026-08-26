# -*- coding: utf-8 -*-
"""并行批量合并（远程 → 本地）。

拆分自 ``core/diff_merge.py``。单个文件的写入逻辑见 ``core.diff.merge_file``。
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .scan_remote import get_file_cached
from .merge_file import merge_to_local
from .models import _log


def merge_entries(
    local_dir: str,
    entries: list,
    client,
    namespace: str,
    file_ttl: int = 86400,
    use_cache: bool = True,
    max_workers: int = 4,
    on_progress: Optional[Callable[[int, int, int, int, str, bool, Optional[str]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple:
    """并行抓取 + 写入多个文件（CLI / 批量合并加速）。

    每个任务：``get_file_cached`` 抓取远程内容 → ``merge_to_local`` 写入本地。
    并发受两重约束：本函数 ``max_workers`` 限制同时在途任务数；底层 ``client`` 的
    全局令牌桶（``throttle``）钳制对 Jira 服务器的稳态请求速率，因此**无论并发多大
    都不会打崩服务器**。

    Args:
        local_dir: 本地目录
        entries:   DiffEntry 列表（仅含待合并的 MODIFIED / REMOTE_ONLY）
        client:    已选中仓库的 JiraGitClient
        namespace: 缓存命名空间（repo_id）
        file_ttl:  文件内容缓存 TTL
        use_cache: 是否启用内容缓存
        max_workers: 并发任务数（默认 4，建议 4~8）
        on_progress: 进度回调 (done, ok, fail, total, path, success, error)
        should_cancel: 取消回调

    Returns:
        (ok, fail, merged_list, failed_list)
    """
    total = len(entries)
    if total == 0:
        return 0, 0, [], []

    state = {"ok": 0, "fail": 0, "done": 0}
    lock = threading.Lock()

    def _work(entry):
        if should_cancel and should_cancel():
            return
        try:
            content = get_file_cached(
                client, entry.path, namespace,
                ttl=file_ttl, use_cache=use_cache,
            )
            success = merge_to_local(local_dir, entry.path, content if content is not None else "")
            err: Optional[str] = None
        except Exception as e:  # noqa: BLE001
            success = False
            err = str(e)

        with lock:
            if success:
                state["ok"] += 1
                merged_list.append({"path": entry.path, "status": entry.status.value})
            else:
                state["fail"] += 1
                failed_list.append({"path": entry.path, "error": err or "merge_failed"})
            state["done"] += 1
            cur_done = state["done"]
            cur_ok = state["ok"]
            cur_fail = state["fail"]

        if on_progress:
            on_progress(cur_done, cur_ok, cur_fail, total, entry.path, success, err)

    merged_list: list = []
    failed_list: list = []
    # 拆分为小批量提交，便于在取消信号到达时尽快停止提交新任务
    batch_size = max(64, max_workers * 16)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        submitted = 0
        while submitted < total:
            if should_cancel and should_cancel():
                break
            batch = entries[submitted: submitted + batch_size]
            submitted += len(batch)
            futures = [pool.submit(_work, e) for e in batch]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    pass

    return state["ok"], state["fail"], merged_list, failed_list
