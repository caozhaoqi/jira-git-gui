# -*- coding: utf-8 -*-
"""K8s Pod 内命令执行与文件读写 —— 聚合兼容层。

实现已按职责拆分到子模块：
  - core.k8s.exec_cmd : kubectl 二进制解析 / exec 基础参数 / 命令执行
  - core.k8s.exec_fs  : 容器内文件浏览 / 读写 / 删除 / 建目录

本文件对子模块做 re-export，保证 ``from core.k8s.exec import X`` /
``from core.k8s import X`` 的既有调用方式不变。
"""
from .env import get_env

from .exec_cmd import (  # noqa: E402,F401
    _exec_base_args,
    _resolve_kubectl_binary,
    _kubectl_subprocess_env,
    _run_kubectl_bytes,
    _build_exec_script,
    _split_pwd,
    exec_command,
)
from .exec_fs import (  # noqa: E402,F401
    _parse_ls,
    _file_size_bytes,
    list_dir,
    read_file,
    write_file,
    delete_path,
    mkdir_path,
)


def resolve_env_kubeconfig(env_name):
    """返回 (kubeconfig_path, namespace) 供快照/日志使用。"""
    _, env = get_env(env_name)
    return env.get("kubeconfig") or None, env.get("namespace") or None


__all__ = [
    "exec_command",
    "list_dir",
    "read_file",
    "write_file",
    "delete_path",
    "mkdir_path",
    "resolve_env_kubeconfig",
    "_exec_base_args",
    "_resolve_kubectl_binary",
    "_kubectl_subprocess_env",
    "_run_kubectl_bytes",
    "_build_exec_script",
    "_split_pwd",
    "_parse_ls",
    "_file_size_bytes",
]
