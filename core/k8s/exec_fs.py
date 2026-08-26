# -*- coding: utf-8 -*-
"""K8s Pod 内文件浏览 / 读写 / 删除 / 建目录。

依赖于 ``core.k8s.exec_cmd`` 的执行基础设施（``_exec_base_args`` / ``_run_kubectl_bytes`` /
``_resolve_kubectl_binary`` / ``_kubectl_subprocess_env``）。
"""
import base64
import re
import shlex
import subprocess as _subprocess

from core.errors import UserError
from .exec_cmd import (
    _exec_base_args,
    _run_kubectl_bytes,
    _kubectl_subprocess_env,
)


_LS_RE = re.compile(
    r"^(?P<mode>[dl-][rwxST\-]{9})\s+"
    r"(?P<link>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<mdate>\S+\s+\S+\s+\S+)\s+"
    r"(?P<name>.+?)\s*$"
)

_TOTAL_RE = re.compile(r"^total\s+\d+")


def _parse_ls(text):
    """解析 ``ls -la`` 输出为统一条目列表。"""
    entries = []
    for line in text.splitlines():
        if _TOTAL_RE.match(line) or not line.strip():
            continue
        m = _LS_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            continue
        mode = m.group("mode")
        entries.append({
            "name": name,
            "type": "dir" if mode.startswith("d") else "file",
            "size": int(m.group("size")),
            "mode": mode,
            "modtime": m.group("mdate").strip(),
        })
    return entries


def list_dir(env, pod, container, namespace, path, timeout=60):
    """列出 Pod 内某路径下的文件 / 目录。"""
    if not pod:
        raise UserError("缺少 pod 参数。")
    path = path or "/"
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = "ls -la %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    data, rc, err = _run_kubectl_bytes(argv, timeout=timeout)
    if rc != 0:
        raise UserError(
            "列目录失败(%s)：%s" % (
                path, err.decode("utf-8", "replace").strip()[:400] or "未知错误")
        )
    return _parse_ls(data.decode("utf-8", "replace"))


def read_file(env, pod, container, namespace, path, max_bytes=200000, timeout=60):
    """读取 Pod 内文本文件内容（默认上限 200KB）。

    返回 ``(content, is_binary)``：
    - 文本：``content`` 为解码后的字符串（已截断到 ``max_bytes``），``is_binary=False``。
    - 二进制（含 NUL 或不可解码 UTF-8）：``content`` 为 base64 字符串，``is_binary=True``。
    """
    if not pod:
        raise UserError("缺少 pod 参数。")
    if max_bytes is None or max_bytes <= 0:
        max_bytes = 200000
    base, _ = _exec_base_args(env, pod, container, namespace)
    # 多取 1 字节用于判断截断
    script = "head -c %d %s" % (int(max_bytes) + 1, shlex.quote(path))
    argv = base + ["--", "sh", "-c", script]
    data, rc, err = _run_kubectl_bytes(argv, timeout=timeout)
    if rc != 0:
        raise UserError(
            "读取文件失败(%s)：%s" % (
                path, err.decode("utf-8", "replace").strip()[:400] or "未知错误")
        )
    if b"\x00" in data:
        return base64.b64encode(data[:max_bytes]).decode("ascii"), True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(data[:max_bytes]).decode("ascii"), True
    return text[:max_bytes], False


def _file_size_bytes(env, pod, container, namespace, path, timeout=60):
    """返回 Pod 内文件字节数（供读取端点判断 truncated）。"""
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = "wc -c < %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    data, rc, _ = _run_kubectl_bytes(argv, timeout=timeout)
    if rc != 0:
        return None
    try:
        return int(data.decode("utf-8", "replace").strip())
    except ValueError:
        return None


def write_file(env, pod, container, namespace, path, content, binary=False, timeout=60):
    """将内容写入 Pod 内文件。

    - 文本：``content`` 为 str，经 stdin 送入 ``cat > <path>``。
    - 二进制：``content`` 为 bytes，经 ``base64 -d > <path>`` 解码写入。
    """
    if not pod:
        raise UserError("缺少 pod 参数。")
    payload = content.encode("utf-8") if isinstance(content, str) else content
    base, _ = _exec_base_args(env, pod, container, namespace)
    if binary:
        script = "base64 -d > %s" % shlex.quote(path)
    else:
        script = "cat > %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    proc = _subprocess.run(
        list(argv), input=payload, capture_output=True, timeout=timeout,
        env=_kubectl_subprocess_env()
    )
    if proc.returncode != 0:
        raise UserError(
            "写入文件失败(%s)：%s" % (
                path, (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
                or "未知错误")
        )


def delete_path(env, pod, container, namespace, path, is_dir=False, timeout=60):
    """删除 Pod 内文件或目录。"""
    if not pod:
        raise UserError("缺少 pod 参数。")
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = ("rm -rf %s" if is_dir else "rm -f %s") % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    proc = _subprocess.run(
        list(argv), capture_output=True, timeout=timeout,
        env=_kubectl_subprocess_env())
    if proc.returncode != 0:
        raise UserError(
            "删除失败(%s)：%s" % (
                path, (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
                or "未知错误")
        )


def mkdir_path(env, pod, container, namespace, path, timeout=60):
    """在 Pod 内创建目录（含父级）。"""
    if not pod:
        raise UserError("缺少 pod 参数。")
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = "mkdir -p %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    proc = _subprocess.run(
        list(argv), capture_output=True, timeout=timeout,
        env=_kubectl_subprocess_env())
    if proc.returncode != 0:
        raise UserError(
            "创建目录失败(%s)：%s" % (
                path, (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
                or "未知错误")
        )
