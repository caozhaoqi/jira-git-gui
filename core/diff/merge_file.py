# -*- coding: utf-8 -*-
"""单文件合并（远程 → 本地）：内容比对、写入、权限修复。

拆分自 ``core/diff_merge.py``。批量并行合并见 ``core.diff.merge_entries``。
文本/二进制判断与规范化比较复用 ``core.diff.diff_core`` 的辅助函数。
"""
import os
import subprocess
from pathlib import Path

from .models import _DIR_CACHE, _DIR_CACHE_LOCK, _log
from .diff_core import _is_text_file, _is_same_normalized


def _force_writable(path: Path) -> None:
    """尝试清除 quarantine 属性并修改权限为可写（文件或目录）。"""
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


def merge_to_local(local_dir: str, rel_path: str, remote_content) -> bool:
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
