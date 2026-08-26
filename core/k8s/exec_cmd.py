# -*- coding: utf-8 -*-
"""K8s Pod 内命令执行：kubectl 二进制解析 / exec 基础参数 / 命令执行。

依赖 ``core.k8s.env.get_env`` 与 ``core.k8s.pods._env_kubectl_prefix``。
"""
import os
import shlex
import shutil
import subprocess as _subprocess

from core.errors import UserError
from .env import get_env
from .pods import _env_kubectl_prefix


def _exec_base_args(env_name, pod, container, namespace):
    """构造 ``kubectl exec`` 的基础参数（含 env 前缀 / namespace / container）。

    返回 ``(args, ns)``，其中 ``args`` 形如
    ``['kubectl', '--kubeconfig', <kc>, 'exec', <pod>, ('-n', <ns>)?, ('-c', <c>)?]``，
    调用方需自行补上 ``-- sh -c <script>``。env 不存在时抛 ``UserError``。
    """
    _, env = get_env(env_name)
    args = ["kubectl"] + _env_kubectl_prefix(env) + ["exec", pod]
    ns = namespace or env.get("namespace")
    if ns:
        args += ["-n", ns]
    if container:
        args += ["-c", container]
    return args, ns


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


def _kubectl_subprocess_env(sub_env=None):
    env = (sub_env or os.environ).copy()
    resolved = _resolve_kubectl_binary()
    if resolved and os.path.dirname(resolved):
        bin_dir = os.path.dirname(resolved)
        entries = [bin_dir] + [p for p in env.get("PATH", "").split(os.pathsep) if p]
        env["PATH"] = os.pathsep.join(dict.fromkeys(entries))
    return env


def _run_kubectl_bytes(argv, timeout=60, sub_env=None):
    """以字节模式执行 kubectl（用于二进制安全的 exec / 文件读写）。"""
    args = list(argv)
    resolved = _resolve_kubectl_binary()
    if not args or args[0] != resolved:
        if args and args[0] == "kubectl":
            args[0] = resolved
        else:
            args.insert(0, resolved)
    try:
        proc = _subprocess.run(
            args, capture_output=True, timeout=timeout, env=_kubectl_subprocess_env(sub_env)
        )
        return proc.stdout or b"", proc.returncode, proc.stderr or b""
    except _subprocess.TimeoutExpired:
        return b"", 124, b"kubectl timed out"
    except FileNotFoundError:
        return b"", 127, b"kubectl not found in PATH (install kubectl and add to PATH)"


def _build_exec_script(command, cwd=None, track_cwd=False):
    """构造传给 ``sh -c`` 的脚本。

    - ``cwd``：前置 ``cd '<cwd>' && ``（路径经 shlex.quote，避免注入）。
    - 用户命令按 shell 语义执行（保留管道 / 重定向）。
    - ``track_cwd=True``：追加 ``__PWD__`` 标记 + ``pwd``，并保留用户命令退出码
      （``exit $__EX__``），便于上层解析新工作目录且不掩盖命令真实失败。
    """
    script = "cd %s && %s" % (shlex.quote(cwd), command) if cwd else command
    if track_cwd:
        script = (
            "set +e\n" + script +
            "\n__EX__=$?\nprintf '\\n__PWD__\\n'\npwd\nexit $__EX__"
        )
    return script


def _split_pwd(merged):
    """从带 ``__PWD__`` 标记的输出中解析新工作目录，并返回去掉标记后的内容。

    返回 ``(new_cwd, clean_output)``；无标记时 ``new_cwd=None``。
    """
    idx = merged.rfind("__PWD__")
    if idx == -1:
        return None, merged
    head = merged[:idx]
    tail = merged[idx + len("__PWD__"):]
    new_cwd = None
    for ln in tail.splitlines():
        if ln.strip():
            new_cwd = ln.strip()
            break
    return new_cwd, head.rstrip("\n")


def exec_command(env, pod, container, namespace, command, cwd=None, timeout=60):
    """在 Pod 内一次性执行命令。

    返回 ``(output, new_cwd)``：``output`` 为合并后的 stdout/stderr（已剔除
    ``__PWD__`` 标记），``new_cwd`` 为命令执行后的工作目录（``cd`` 未触发则回退
    到传入的 ``cwd``）。kubectl 层错误（Pod 不存在 / 未连接等）抛 ``UserError``。
    """
    if not pod:
        raise UserError("缺少 pod 参数。")
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = _build_exec_script(command, cwd, track_cwd=True)
    argv = base + ["--", "sh", "-c", script]
    data, rc, err = _run_kubectl_bytes(argv, timeout=timeout)
    out = data.decode("utf-8", "replace")
    err_s = err.decode("utf-8", "replace")
    merged = out
    if err_s:
        merged = (merged + "\n" + err_s) if merged else err_s
    if "__PWD__" not in merged:
        raise UserError(
            "在 Pod(%s) 中执行命令失败：%s" % (pod, err_s.strip()[:400] or "未知错误")
        )
    new_cwd, clean = _split_pwd(merged)
    if not new_cwd and cwd:
        new_cwd = cwd
    return clean, new_cwd
