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
import json as _json
import os
import threading
import time
from xml.dom import minidom as _minidom
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
# 并发合并（merge_entries / Web 批量合并）下保护 _DIR_CACHE 的读写
_DIR_CACHE_LOCK = threading.Lock()

def clear_dir_cache() -> None:
    """清空父目录缓存（建议每个批量合并任务开始前调用一次）。"""
    with _DIR_CACHE_LOCK:
        _DIR_CACHE.clear()

# 扫描时跳过的目录名
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", ".trae", ".qoder", ".playwright-cli", ".workbuddy",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
}

# 扫描时跳过的文件名
SKIP_FILES = {".DS_Store", "Thumbs.db", ".gitignore"}

# 通常按行尾符差异处理的文本文件扩展名（用于 compute_diff 的 whitespace_only 启发）
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".scala", ".groovy",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".go", ".rs", ".swift",
    ".rb", ".php", ".pl", ".pm", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".html", ".htm", ".xml", ".xhtml", ".svg",
    ".css", ".scss", ".sass", ".less", ".styl",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".config",
    ".sql", ".lua", ".vim", ".el", ".clj", ".erl", ".ex", ".exs",
    ".dockerfile", ".gitignore", ".gitattributes", ".editorconfig", "Makefile",
}


class DiffStatus(str, Enum):
    SAME = "same"           # 内容相同
    MODIFIED = "modified"   # 双方都有但内容不同
    WHITESPACE_ONLY = "whitespace_only"  # 仅行尾符/空白差异（文本文件归一化后相同）
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
        # 注意：diffState 里的 modified 计数已包含 WHITESPACE_ONLY，
        # 这里额外给出 whitespace_only 便于前端单独展示。
        return {
            "total": self.total,
            "same": self.same,
            "modified": self.modified,
            "whitespace_only": sum(1 for e in self.entries if e.status == DiffStatus.WHITESPACE_ONLY),
            "local_only": self.local_only,
            "remote_only": self.remote_only,
        }


# --------------------------------------------------------------------------- #
#  本地文件扫描
# --------------------------------------------------------------------------- #
def scan_local(local_dir: str, prev: Optional[dict] = None) -> dict[str, dict]:
    """递归扫描本地目录，返回 {relative_path: {size, mtime, hash, norm_hash, norm_size}}。

    增量优化（prev）：
    - prev 为上次扫描结果（可来自 JSON 缓存）。
    - 若某文件 size 与 mtime（st_mtime_ns）均未变化，且 prev 中已有 hash，
      直接复用旧 hash，跳过 MD5 整文件读取 —— 大仓库重复扫描可省下绝大部分磁盘 I/O。
    - 仅新增 / 修改（mtime 变化）的文件才重新计算 MD5。

    行尾归一化：
    - 对文本文件额外计算 norm_hash（\\r\\n 归一为 \\n 后的 MD5）和 norm_size。
    - 用于 compute_diff 识别 CRLF vs LF 导致的伪修改，避免无意义合并。
    """
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
                size = stat.st_size
                mtime = stat.st_mtime_ns
                is_text = _is_text_file(full)
                if prev:
                    pe = prev.get(rel)
                    # size + mtime 双校验：两者都未变才可安全复用旧 hash
                    if (pe and pe.get("size") == size
                            and pe.get("mtime") == mtime and pe.get("hash")):
                        h = pe["hash"]
                        norm_h = pe.get("norm_hash") if is_text else ""
                        norm_size = pe.get("norm_size") if is_text else size
                    else:
                        h = _file_hash(full)
                        norm_h = _normalized_hash(full) if is_text else ""
                        norm_size = _normalized_size(full) if is_text else size
                else:
                    h = _file_hash(full)
                    norm_h = _normalized_hash(full) if is_text else ""
                    norm_size = _normalized_size(full) if is_text else size
                entry: dict = {"size": size, "mtime": mtime, "hash": h}
                if is_text and norm_h:
                    entry["norm_hash"] = norm_h
                    entry["norm_size"] = norm_size
                result[rel] = entry
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
        {relative_path: {size, mtime, hash, norm_hash?, norm_size?}}
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

    # 未命中/过期：以「最近一次扫描（忽略 TTL）」为增量基线，未变文件复用旧 hash，
    # 跳过 MD5 整文件读取。零 I/O 命中路径（上方）保留，此处仅加速过期后的重扫。
    prev = cache.get(ns, key, ttl=0)
    result = scan_local(local_dir, prev=prev)
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


def _is_text_file(path: Path) -> bool:
    """基于扩展名判断文件是否为文本文件（用于行尾归一化比较）。"""
    ext = path.suffix.lower()
    if not ext and path.name.lower() == "makefile":
        return True
    return ext in _TEXT_EXTENSIONS


def _normalized_hash(path: Path) -> str:
    """计算文件内容的「归一化 MD5」：将 \\r\\n 统一为 \\n 后计算哈希。

    用于识别仅因行尾符（CRLF vs LF）差异导致 raw hash 不同的文本文件。
    读取失败时返回空字符串。
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
        # 只处理看起来像文本的内容：遇到 NUL 字节则放弃归一化
        if b"\x00" in data:
            return ""
        text = data.decode("utf-8", errors="replace")
        normalized = text.replace("\r\n", "\n")
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()
    except (OSError, PermissionError):
        return ""


def _normalized_size(path: Path) -> int:
    """返回文件内容将 \\r\\n 替换为 \\n 后的字节数。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if b"\x00" in data:
            return len(data)
        return len(data.replace(b"\r\n", b"\n"))
    except (OSError, PermissionError):
        return 0


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
def compute_diff(
    local_files: dict,
    remote_files: dict,
    *,
    ignore_line_endings: bool = True,
) -> DiffResult:
    """对比本地和远程文件列表，生成差异结果。

    集合差集/交集实现：先对三类路径各自排序，避免每次迭代都做一次 .get 查找与
    `sorted(union)` 的大列表构建，万级文件下更稳定。

    Args:
        ignore_line_endings: 为 True 时，对文本文件尝试把 CRLF/LF 差异识别为
            WHITESPACE_ONLY（仅行尾/空白差异），避免无意义合并。
    """
    res = DiffResult()
    local_keys = set(local_files)
    remote_keys = set(remote_files)

    # 仅本地
    for p in sorted(local_keys - remote_keys):
        e = local_files[p]
        res.entries.append(DiffEntry(
            path=p,
            status=DiffStatus.LOCAL_ONLY,
            local_size=e["size"],
            local_hash=e.get("hash"),
        ))
        res.local_only += 1
        res.total += 1

    # 仅远程
    for p in sorted(remote_keys - local_keys):
        e = remote_files[p]
        res.entries.append(DiffEntry(
            path=p,
            status=DiffStatus.REMOTE_ONLY,
            remote_size=e["size"],
        ))
        res.remote_only += 1
        res.total += 1

    # 双方都有
    for p in sorted(local_keys & remote_keys):
        local = local_files[p]
        remote = remote_files[p]
        if local.get("hash") and remote.get("hash"):
            same = local["hash"] == remote["hash"]
        else:
            # 远程没有 hash，用 size 快速判断；size 相同则需进一步检查
            same = local["size"] == remote["size"]

        status = DiffStatus.SAME if same else DiffStatus.MODIFIED

        # 行尾/空白差异启发：文本文件 raw 不同但归一化大小一致时，
        # 标为 WHITESPACE_ONLY，避免 1.1 万个 CRLF vs LF 文件被误判为修改。
        if (not same and ignore_line_endings
                and local.get("norm_hash") and local.get("norm_size") is not None):
            # 远程通常只有 size，用归一化大小做高效启发：
            # 若本地归一化后大小 == 远程大小，大概率仅行尾差异
            if local["norm_size"] == remote["size"]:
                status = DiffStatus.WHITESPACE_ONLY

        res.entries.append(DiffEntry(
            path=p,
            status=status,
            local_size=local["size"],
            remote_size=remote["size"],
            local_hash=local.get("hash"),
        ))
        if status == DiffStatus.SAME:
            res.same += 1
        else:
            # 在旧 summary 中把 WHITESPACE_ONLY 也归入 modified（保持 API 语义），
            # 但条目 status 字段独立，便于前端过滤。
            res.modified += 1
        res.total += 1

    return res


# --------------------------------------------------------------------------- #
#  单文件 diff
# --------------------------------------------------------------------------- #
def file_diff(local_path: str, remote_content: str) -> str:
    """生成 unified diff 文本。

    修复：keepends=False + lineterm='\\n'，确保控制行（---/+++/@@）有换行符，
    不会和内容行粘在一起。

    额外处理：
    - 若本地与远程内容仅行尾符（CRLF vs LF）不同，则返回空字符串，
      调用方（前端）会显示"内容实际相同"提示。
    - 对 JSON / XML 等结构化文件，先 ``canonical_text`` 规范化展开再 diff，
      使压缩成单行的文件也能呈现行级差异（仅展示层，不影响相等/合并）。
    """
    try:
        with open(local_path, "r", encoding="utf-8", errors="replace") as f:
            local_content = f.read()
    except (OSError, FileNotFoundError):
        local_content = ""

    # 文本文件：仅行尾差异时跳过 unified_diff 计算（保持旧行为）
    if _is_text_file(Path(local_path)) and _is_same_normalized(local_content, remote_content or ""):
        return ""

    # 结构化文件规范化展开，使 diff 变成行级可读（仅展示层，不影响相等/合并）
    local_canon = canonical_text(local_path, local_content)
    remote_canon = canonical_text(local_path, remote_content or "")

    local_lines = local_canon.splitlines(keepends=False)
    remote_lines = remote_canon.splitlines(keepends=False)

    diff = difflib.unified_diff(
        local_lines,
        remote_lines,
        fromfile=f"a/{os.path.basename(local_path)}",
        tofile=f"b/{os.path.basename(local_path)}",
        lineterm="\n",
    )
    return "".join(diff)


def _is_same_normalized(a: str, b: str) -> bool:
    """判断两段文本在忽略行尾符（\\r\\n 统一为 \\n）后是否相同。"""
    return a.replace("\r\n", "\n") == b.replace("\r\n", "\n")


def is_whitespace_only_diff(local_path: str, remote_content: str) -> bool:
    """判断本地文件与远程内容是否仅因行尾符/空白差异而不同。

    用于 /api/diff/file 和批量合并前的二次确认。
    """
    try:
        with open(local_path, "r", encoding="utf-8", errors="replace") as f:
            local_content = f.read()
    except (OSError, FileNotFoundError):
        return False
    if not _is_text_file(Path(local_path)):
        return False
    return _is_same_normalized(local_content, remote_content or "")


# --------------------------------------------------------------------------- #
#  结构化文件规范化展开（仅用于 diff 展示层）
# --------------------------------------------------------------------------- #
# 这些扩展名按 JSON 解析并展开为缩进多行；解析失败回退原文。
_JSON_EXTENSIONS = {
    ".json", ".jsonc", ".json5", ".geojson", ".tfstate", ".ipynb",
}
# 这些扩展名按 XML 解析并展开（minidom.toprettyxml）。
_XML_EXTENSIONS = {
    ".xml", ".xhtml", ".svg", ".wsdl", ".plist", ".rss", ".atom", ".xsl", ".xslt",
}


def _strip_jsonc_comments(text: str) -> str:
    """去掉 JSONC 的行注释 // 与块注释 /* */，保留字符串字面量内的注释符号。

    用于配置类 JSON（带注释）的兜底解析；仅当严格 JSON 解析失败时才调用。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    str_ch = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == str_ch:
                in_str = False
            i += 1
            continue
        if c in ('"', "'"):
            in_str = True
            str_ch = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def canonical_text(path: str, content: str) -> str:
    """对结构化文本文件（JSON / JSONC / XML 系列）做规范化展开，便于行级 diff。

    **仅用于「展示层」diff**：把压缩成单行的 JSON/XML 展开为多行可读格式，
    使 unified diff 变成行级变化，而不是整行标红。

    - 不影响文件相等判定（compute_diff 仍按原始 MD5/size）。
    - 不改变合并写入的字节（merge_to_local 走原始远程内容）。
    - 解析失败则原样返回原文，绝不抛异常。
    """
    if not content:
        return content
    ext = Path(path).suffix.lower()
    if ext in _JSON_EXTENSIONS:
        try:
            return _json.dumps(_json.loads(content), indent=2, ensure_ascii=False)
        except Exception:
            try:
                return _json.dumps(
                    _json.loads(_strip_jsonc_comments(content)), indent=2, ensure_ascii=False
                )
            except Exception:
                return content
    if ext in _XML_EXTENSIONS:
        try:
            dom = _minidom.parseString(content.encode("utf-8"))
            pretty = dom.toprettyxml(indent="  ")
            # toprettyxml 会在节点间插入多余空行，压缩掉（结构化文件无语义空行）
            return "\n".join(line for line in pretty.splitlines() if line.strip())
        except Exception:
            return content
    return content


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
    2. 文本文件额外比较「归一化内容」（忽略 CRLF vs LF 行尾差异），避免无意义合并
    3. 已创建过的父目录放入集合，避免每个文件都 mkdir(parents=True)
    4. 写入失败再 chmod 重试（不依赖子进程，避免沙箱）
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
                    local_content = f.read()
                    if local_content == content:
                        return True
                    # 文本文件：忽略行尾差异后也相同 → 跳过写入
                    if (_is_text_file(target)
                            and _is_same_normalized(local_content, content)):
                        return True
        except OSError:
            pass
    try:
        parent = target.parent
        # 缓存已存在的父目录，避免每个文件都 mkdir(parents=True)（会产生 N 次 stat）
        key = str(parent)
        with _DIR_CACHE_LOCK:
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
            with _DIR_CACHE_LOCK:
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


# --------------------------------------------------------------------------- #
#  并行批量合并
# --------------------------------------------------------------------------- #
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
    ok = 0
    fail = 0
    merged_list: list = []
    failed_list: list = []

    if total == 0:
        return ok, fail, merged_list, failed_list

    state = {"ok": 0, "fail": 0, "done": 0}
    lock = threading.Lock()

    def _work(entry):
        if should_cancel and should_cancel():
            return
        try:
            content = get_file_cached(
                client, namespace, entry.path,
                file_ttl=file_ttl, use_cache=use_cache,
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
