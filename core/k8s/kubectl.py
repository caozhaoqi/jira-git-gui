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


def run_kubectl(args, kubeconfig=None, timeout=60, input=None):
    """执行 kubectl。

    ``kubeconfig`` 为 ``ssh://<env_name>`` 标记时委托 SSH 远程执行
    （见 core/k8s/ssh_exec.py，远端使用远端自身 kubeconfig）。

    ``input`` 为非 None 时作为 stdin 传入（用于 ``kubectl exec -i ... cat > file``
    等写场景）。由于 stdin 可能是字节流（如二进制上传），此时走字节模式并在返回前
    将 stdout/stderr 解码为字符串，避免 ``text=True`` 与 bytes 输入冲突。
    """
    from .ssh_exec import is_ssh_target, marker_env_name, run_kubectl_ssh
    if is_ssh_target(kubeconfig):
        return run_kubectl_ssh(marker_env_name(kubeconfig), args,
                               timeout=timeout, input=input)
    # fail-fast：既没指定环境/kubeconfig，本机也没有默认 ~/.kube/config 时，
    # kubectl 会去连 http://localhost:8080 并挂满整个 timeout（表现为
    # "kubectl timed out"）——直接给出可行动的提示，不再干等。
    if not kubeconfig and not os.environ.get("KUBECONFIG") \
            and not Path.home().joinpath(".kube", "config").is_file():
        return "", 1, ("未找到任何 kubeconfig：请先在 K8s 面板选择环境"
                       "（SSH 远程环境无需本机 kubeconfig），或在环境管理中配置。")
    cmd = [_resolve_kubectl_binary()]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += args
    try:
        if input is None:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_kubectl_env())
            return proc.stdout, proc.returncode, proc.stderr
        # 有 stdin：字节模式，兼容二进制内容
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_kubectl_env(), input=input)
        out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else proc.stdout
        err = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else proc.stderr
        return out, proc.returncode, err
    except subprocess.TimeoutExpired:
        return "", 124, "kubectl timed out"
    except FileNotFoundError:
        return "", 127, "kubectl 不在 PATH 中（请先安装 kubectl 并加入 PATH）"


async def run_kubectl_async(args, kubeconfig=None, timeout=60, input=None):
    """异步版：把同步的 ``run_kubectl`` 放进线程池，避免阻塞事件循环。

    HTTP 路由里直接调同步 ``run_kubectl``（subprocess.run 超时 30~60s）会卡住整个
    asyncio 事件循环 —— SSE 心跳断流、所有并发请求一起停滞。统一走这里即可。
    同步上下文（core 内的辅助函数）仍用 ``run_kubectl`` 即可。
    """
    from .ssh_exec import is_ssh_target, marker_env_name, run_kubectl_ssh_async
    if is_ssh_target(kubeconfig):
        return await run_kubectl_ssh_async(marker_env_name(kubeconfig), args,
                                           timeout=timeout, input=input)
    return await asyncio.to_thread(run_kubectl, args, kubeconfig, timeout=timeout, input=input)


async def stream_kubectl(args, kubeconfig=None):
    """异步流式执行 kubectl（用于 ``logs -f`` 等持续输出场景）。

    返回 ``asyncio.subprocess.Process``（SSH 环境时返回 ``SshKubectlStream``
    适配器，接口兼容），调用方负责读取 ``proc.stdout``
    并在不再需要时 ``proc.kill()`` 回收子进程，避免泄漏。
    """
    from .ssh_exec import is_ssh_target, marker_env_name, SshKubectlStream
    if is_ssh_target(kubeconfig):
        return await SshKubectlStream(marker_env_name(kubeconfig), args).start()
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


