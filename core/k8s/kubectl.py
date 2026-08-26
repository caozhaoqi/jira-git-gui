# -*- coding: utf-8 -*-
"""K8s 快照子模块（由 core/k8s_snapshot.py 拆分，保持 import 兼容）。"""
import asyncio
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from core.errors import UserError

SEV_COLOR = {"HIGH": "#c0392b", "MED": "#d97706", "OK": "#16a34a"}
LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
LOG_LEVEL_DEFAULT = "INFO"

def _resolve_kubectl_binary():
    binary = shutil.which("kubectl")
    if binary:
        return binary
    for candidate in (
        "/opt/homebrew/bin/kubectl",
        "/usr/local/bin/kubectl",
        "/usr/bin/kubectl",
        "/bin/kubectl",
    ):
        if os.path.exists(candidate):
            return candidate
    return "kubectl"


def _kubectl_env():
    env = os.environ.copy()
    resolved = _resolve_kubectl_binary()
    if resolved and os.path.dirname(resolved):
        bin_dir = os.path.dirname(resolved)
        entries = [bin_dir] + [p for p in env.get("PATH", "").split(os.pathsep) if p]
        env["PATH"] = os.pathsep.join(dict.fromkeys(entries))
    return env


def run_kubectl(args, kubeconfig=None, timeout=60):
    cmd = [_resolve_kubectl_binary()]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_kubectl_env())
        return proc.stdout, proc.returncode, proc.stderr
    except subprocess.TimeoutExpired:
        return "", 124, "kubectl timed out"
    except FileNotFoundError:
        return "", 127, "kubectl 不在 PATH 中（请先安装 kubectl 并加入 PATH）"


async def stream_kubectl(args, kubeconfig=None):
    """异步流式执行 kubectl（用于 ``logs -f`` 等持续输出场景）。

    返回 ``asyncio.subprocess.Process``，调用方负责读取 ``proc.stdout``
    并在不再需要时 ``proc.kill()`` 回收子进程，避免泄漏。
    """
    cmd = [_resolve_kubectl_binary()]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += args
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_kubectl_env(),
    )


def _current_context(kubeconfig=None):
    out, rc, _ = run_kubectl(["config", "current-context"], kubeconfig)
    return out.strip() if rc == 0 else "?"


