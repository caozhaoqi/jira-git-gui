# -*- coding: utf-8 -*-
"""K8s Pod / 资源运维 —— 基础封装与聚合兼容层。

实现已按职责拆分到子模块：
  - core.k8s.yaml       : Pod 列表 / 资源 YAML 获取 / 清理 / 应用
  - core.k8s.events     : 事件 / describe / top
  - core.k8s.netdetect  : 网络连通性检测

本文件保留以指定环境身份执行 kubectl 的基础封装（``run_kubectl_env``），
并对上述子模块做 re-export，保证 ``from core.k8s.pods import X`` /
``from core.k8s import X`` 的既有调用方式不变。
"""
from .env import get_env
from .kubectl import run_kubectl


def _env_kubectl_prefix(env):
    args = []
    if env.get("kubeconfig"):
        args += ["--kubeconfig", env["kubeconfig"]]
    if env.get("context"):
        args += ["--context", env["context"]]
    return args


def run_kubectl_env(env_name, args, timeout=60):
    """以指定环境身份执行 kubectl。返回 (stdout, rc, stderr)。"""
    _, env = get_env(env_name)
    # 合并 kubectl 前缀参数和子命令参数，传给 run_kubectl
    full_args = _env_kubectl_prefix(env) + list(args)
    return run_kubectl(full_args, kubeconfig=None, timeout=timeout)


# ---- re-export 子模块实现（保持 import 路径兼容） ----
from .yaml import (  # noqa: E402,F401
    list_pods,
    clean_manifest_obj,
    get_resource_yaml,
    apply_yaml_content,
)
from .events import (  # noqa: E402,F401
    list_events,
    describe_resource,
    get_top,
)
from .netdetect import (  # noqa: E402,F401
    detect_network,
    _api_server_host,
    _split_host,
    _tcp_probe,
)

__all__ = [
    "get_env",
    "run_kubectl_env",
    "list_pods",
    "clean_manifest_obj",
    "get_resource_yaml",
    "apply_yaml_content",
    "list_events",
    "describe_resource",
    "get_top",
    "detect_network",
    "_api_server_host",
    "_split_host",
    "_tcp_probe",
]
