# -*- coding: utf-8 -*-
"""本地目录 vs 远程仓库的差异对比。

设计要点：
- 递归扫描本地目录（排除 .git / node_modules / .venv / __pycache__ 等）
- 递归调用 client.list_level 获取远程文件树
- 按路径 + 大小 快速分类，仅在需要时才取内容做 unified diff
- 支持合并（远程 → 本地）
- 集成 JSON 缓存：远程文件树、文件内容优先走缓存，避免重复拉取
"""
import difflib
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import cache
from .logger import get_logger

_log = get_logger()

# 合并时已确认存在的父目录集合，避免每个文件都 mkdir(parents=True)
_DIR_CACHE: set[str] = set()

def clear_dir_cache() -> None:
    """清空父目录缓存（建议每个批量合并任务开始前调用一次）。"""
    _DIR_CACHE.clear()

# 扫描时跳过的目录名
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", ".trae", ".qoder", ".playwright-cli", ".workbuddy",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
}

# 扫描时跳过的文件名
SKIP_FILES = {".DS_Store", "Thumbs.db", ".gitignore"}


class DiffStatus(str, Enum):
    SAME = "same"           # 内容相同
    MODIFIED = "modified"   # 双方都有但内容不同
    LOCAL_ONLY = "local_only"   # 仅本地存在
    REMOTE_ONLY = "remote_only" # 仅远程存在


@dataclass
class DiffEntry:
    """单文件的差异信息。"""
    path: str               # 相对路径
    status: DiffStatus
    local_size: Optional[int] = None
    remote_size: Optional[int] = None
    local_hash: Optional[str] = None
    remote_hash: Optional[str] = None


@dataclass
class DiffResult:
    """整个扫描的结果。"""
    entries: list[DiffEntry] = field(default_factory=list)
    total: int = 0
    same: int = 0
    modified: int = 0
    local_only: int = 0
    remote_only: int = 0
    error: str = ""

    def summary(self) -> dict:
        return {
            "total": self.total,
            "same": self.same,
            "modified": self.modified,
            "local_only": self.local_only,
            "remote_only": self.remote_only,
        }


# --------------------------------------------------------------------------- #
#  本地文件扫描
# --------------------------------------------------------------------------- #
def scan_local(local_dir: str) -> dict[str, dict]:
    """递归扫描本地目录，返回 {relative_path: {size, hash}}。"""
    result = {}
    base = Path(local_dir)
    if not base.is_dir():
        return result

    for root, dirs, files in os.walk(base):
        # 原地修改 dirs 以跳过不需要的目录
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES:
                continue
            full = Path(root) / f
            try:
                rel = str(full.relative_to(base))
                # 统一为正斜杠
                rel = rel.replace("\\", "/")
                stat = full.stat()
                result[rel] = {"size": stat.st_size, "hash": _file_hash(full)}
            except (OSError, PermissionError):
                continue
    return result


def scan_local_cached(
    local_dir: str,
    tree_ttl: int = 300,
    use_cache: bool = True,
) -> dict[str, dict]:
    """缓存优先的本地扫描（本地变更较快，TTL 默认 5 分钟）。

    Args:
        local_dir: 本地目录
        tree_ttl: 缓存有效期（秒）
        use_cache: 是否启用缓存

    Returns:
        {relative_path: {size, hash}}
    """
    if not use_cache:
        return scan_local(local_dir)

    # 用目录绝对路径作为缓存命名空间与 key
    ns = "local"
    key = str(Path(local_dir).resolve())

    cached = cache.get(ns, key, tree_ttl)
    if cached is not None:
        _log.info("本地文件树命中缓存（%d 文件）", len(cached))
        return cached

    result = scan_local(local_dir)
    if result:
        cache.set(ns, key, result)
    return result


def _file_hash(path: Path) -> str:
    """计算文件 MD5（用于快速判断内容是否一致）。"""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return ""


# --------------------------------------------------------------------------- #
#  远程文件扫描
# --------------------------------------------------------------------------- #
def scan_remote(client, path: str = "") -> dict[str, dict]:
    """递归扫描远程仓库，返回 {relative_path: {size}}。

    利用 client.list_level 递归获取所有文件。
    """
    result = {}
    _scan_remote_dir(client, path, result)
    return result


def _scan_remote_dir(client, path: str, result: dict):
    """递归扫描远程目录。"""
    try:
        entries = client.list_level(path)
    except Exception as e:
        _log.error("扫描远程目录 %s 失败: %s", path, e)
        return

    for e in entries:
        if e.type == "dir":
            _scan_remote_dir(client, e.path, result)
        elif e.type == "file":
            result[e.path] = {"size": e.size}


def scan_remote_parallel(
    client,
    max_workers: int = 3,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict[str, dict]:
    """并行扫描远程仓库（低并发 + SSL 重试）。

    Args:
        client: 已选中仓库的 JiraGitClient
        max_workers: 并发线程数
        on_progress: 进度回调 (scanned_files, pending_dirs)
        should_cancel: 外部取消回调（网络看门狗/用户取消）

    Returns:
        {relative_path: {size}}
    """
    result: dict[str, dict] = {}
    pending_dirs = [""]
    scanned = 0
    failed_dirs: list[str] = []

    def _list_with_retry(c, p, retries=3):
        for attempt in range(retries):
            if should_cancel and should_cancel():
                raise RuntimeError("任务已取消")
            try:
                return c.list_level(p)
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1 + attempt * 2)
                else:
                    raise

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while pending_dirs:
            if should_cancel and should_cancel():
                _log.warning("扫描已取消（网络中断或用户主动取消）")
                break
            batch = pending_dirs[:]
            pending_dirs.clear()
            futures = {pool.submit(_list_with_retry, client, d): d for d in batch}

            for fut in as_completed(futures):
                if should_cancel and should_cancel():
                    break
                dir_path = futures[fut]
                try:
                    entries = fut.result()
                except Exception as e:
                    failed_dirs.append(dir_path)
                    if len(failed_dirs) <= 10:
                        _log.warning("扫描失败: %s → %s", dir_path, e)
                    continue

                for e in entries:
                    if e.type == "dir":
                        pending_dirs.append(e.path)
                    elif e.type == "file":
                        result[e.path] = {"size": e.size}
                        scanned += 1
                        if on_progress and scanned % 500 == 0:
                            on_progress(scanned, len(pending_dirs))

    if failed_dirs:
        _log.warning("%d 个目录扫描失败（SSL/网络错误），已跳过", len(failed_dirs))
    return result


def scan_remote_cached(
    client,
    namespace: str,
    max_workers: int = 3,
    tree_ttl: int = 3600,
    on_progress: Optional[Callable[[int, int], None]] = None,
    use_cache: bool = True,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict[str, dict]:
    """缓存优先的远程扫描。

    1. 先从缓存读取整个仓库的文件树
    2. 未命中则并行扫描并写入缓存
    3. 命中且未过期则直接返回（避免重复拉取）

    Args:
        client: 已选中仓库的客户端
        namespace: 缓存命名空间（建议用 repo_id）
        max_workers: 扫描并发数
        tree_ttl: 文件树缓存有效期（秒）
        on_progress: 进度回调
        use_cache: 是否启用缓存
        should_cancel: 外部取消回调

    Returns:
        {relative_path: {size}}
    """
    cache_key = "tree_full"

    if use_cache:
        cached = cache.get(namespace, cache_key, tree_ttl)
        if cached is not None:
            _log.info("远程文件树命中缓存（%d 文件）", len(cached))
            return cached

    # 未命中，并行扫描
    _log.info("缓存未命中，开始并行扫描远程文件树…")
    result = scan_remote_parallel(client, max_workers=max_workers,
                                   on_progress=on_progress,
                                   should_cancel=should_cancel)

    if use_cache and result:
        cache.set(namespace, cache_key, result)
    return result


def get_file_cached(
    client,
    namespace: str,
    path: str,
    file_ttl: int = 86400,
    use_cache: bool = True,
) -> Optional[str]:
    """缓存优先获取远程文件内容。

    Returns:
        文件内容（str），失败返回 None
    """
    cache_key = f"file:{path}"

    if use_cache:
        cached = cache.get(namespace, cache_key, file_ttl)
        if cached is not None:
            return cached

    try:
        content = client.get_file(path)
        # get_file 可能返回 (content, err) 元组
        if isinstance(content, tuple):
            content = content[0] if content else None
        if content is None:
            return None
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8")
            except UnicodeDecodeError:
                # 二进制不缓存为文本
                return content
        if use_cache and content is not None:
            cache.set(namespace, cache_key, content)
        return content
    except Exception as e:
        _log.error("获取远程文件 %s 失败: %s", path, e)
        return None


# --------------------------------------------------------------------------- #
#  差异计算
# --------------------------------------------------------------------------- #
def compute_diff(local_files: dict, remote_files: dict) -> DiffResult:
    """对比本地和远程文件列表，生成差异结果。"""
    res = DiffResult()
    all_paths = set(local_files.keys()) | set(remote_files.keys())

    for p in sorted(all_paths):
        local = local_files.get(p)
        remote = remote_files.get(p)

        if local and remote:
            # 双方都有
            if local.get("hash") and remote.get("hash"):
                same = local["hash"] == remote["hash"]
            else:
                # 远程没有 hash，用 size 快速判断；size 相同则需进一步检查
                same = local["size"] == remote["size"]

            entry = DiffEntry(
                path=p,
                status=DiffStatus.SAME if same else DiffStatus.MODIFIED,
                local_size=local["size"],
                remote_size=remote["size"],
                local_hash=local.get("hash"),
            )
            if same:
                res.same += 1
            else:
                res.modified += 1
        elif local and not remote:
            entry = DiffEntry(
                path=p,
                status=DiffStatus.LOCAL_ONLY,
                local_size=local["size"],
                local_hash=local.get("hash"),
            )
            res.local_only += 1
        else:
            entry = DiffEntry(
                path=p,
                status=DiffStatus.REMOTE_ONLY,
                remote_size=remote["size"],
            )
            res.remote_only += 1

        res.entries.append(entry)
        res.total += 1

    return res


# --------------------------------------------------------------------------- #
#  单文件 diff
# --------------------------------------------------------------------------- #
def file_diff(local_path: str, remote_content: str) -> str:
    """生成 unified diff 文本。

    修复：keepends=False + lineterm='\\n'，确保控制行（---/+++/@@）有换行符，
    不会和内容行粘在一起。
    """
    try:
        with open(local_path, "r", encoding="utf-8", errors="replace") as f:
            local_content = f.read()
    except (OSError, FileNotFoundError):
        local_content = ""

    local_lines = local_content.splitlines(keepends=False)
    remote_lines = (remote_content or "").splitlines(keepends=False)

    diff = difflib.unified_diff(
        local_lines,
        remote_lines,
        fromfile=f"a/{os.path.basename(local_path)}",
        tofile=f"b/{os.path.basename(local_path)}",
        lineterm="\n",
    )
    return "".join(diff)


# --------------------------------------------------------------------------- #
#  合并
# --------------------------------------------------------------------------- #
def _force_writable(path: Path) -> None:
    """尝试清除 quarantine 属性并修改权限为可写（文件或目录）。"""
    import subprocess
    # 清除 quarantine（macOS 隔离属性，会阻止写入）
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(path)],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    # 修改权限为可读写
    try:
        os.chmod(path, 0o777 if path.is_dir() else 0o666)
    except (OSError, PermissionError):
        pass


def merge_to_local(local_dir: str, rel_path: str, remote_content: str) -> bool:
    """将远程文件内容写入本地路径。

    优化：
    1. 仅当本地文件内容不同时才写入（避免无意义刷盘、mtime 变更）
    2. 已创建过的父目录放入集合，避免每个文件都 mkdir(parents=True)
    3. 写入失败再 chmod 重试（不依赖子进程，避免沙箱）
    """
    target = Path(local_dir) / rel_path
    content = remote_content or ""
    is_bytes = isinstance(content, bytes)
    # 快速跳过：文件存在且内容完全相同 → 视为成功，直接返回
    if target.exists() and target.is_file():
        try:
            if is_bytes:
                with open(target, "rb") as f:
                    if f.read() == content:
                        return True
            else:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    if f.read() == content:
                        return True
        except OSError:
            pass
    try:
        parent = target.parent
        # 缓存已存在的父目录，避免每个文件都 mkdir(parents=True)（会产生 N 次 stat）
        key = str(parent)
        if key not in _DIR_CACHE:
            parent.mkdir(parents=True, exist_ok=True)
            _DIR_CACHE.add(key)
        _write_file(target, content, is_bytes)
        return True
    except (OSError, PermissionError) as e:
        try:
            if target.exists():
                os.chmod(target, 0o666)
            parent = target.parent
            key = str(parent)
            if key not in _DIR_CACHE:
                parent.mkdir(parents=True, exist_ok=True)
                _DIR_CACHE.add(key)
            _write_file(target, content, is_bytes)
            return True
        except (OSError, PermissionError) as e2:
            _log.error("合并文件 %s 失败: %s", rel_path, e2)
            return False


def _write_file(target: Path, content, is_bytes: bool = False) -> None:
    """写入文件内容（直接覆盖，避免删除带来的权限问题）。"""
    if is_bytes:
        with open(target, "wb") as f:
            f.write(content)
    else:
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)


def merge_to_local_bytes(local_dir: str, rel_path: str, remote_bytes: bytes) -> bool:
    """将远程二进制内容写入本地路径。"""
    target = Path(local_dir) / rel_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(remote_bytes)
        return True
    except (OSError, PermissionError) as e:
        _log.error("合并文件 %s 失败: %s", rel_path, e)
        return False
