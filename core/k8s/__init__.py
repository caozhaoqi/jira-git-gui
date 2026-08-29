"""core.k8s 子包 —— 由拆分后的子模块聚合而成，对外符号保持兼容。"""

# ---- 以下来自原 core/k8s_manager.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""Kubernetes 多环境运维核心逻辑（聚合兼容层）。

实现已按职责拆分到子模块：
  - core.k8s.env   : 多环境配置管理（增删改查 / 导入导出 kubeconfig）
  - core.k8s.pods   : Pod / 资源 YAML / 事件 / top / 网络探测
  - core.k8s.exec   : Pod 内命令执行与文件读写

本文件仅做重导出，确保 `from core import k8s_manager` / `from core.k8s import X` 不变。
"""

from .env import (
    ENV_CONFIG_PATH, DEFAULT_ENV_SEED,
load_envs,
    save_envs,
    list_envs,
    get_env,
    add_or_update_env,
    set_current_env,
    import_kubeconfig,
    export_envs,
    delete_env,
)

from .pods import (
run_kubectl_env,
    list_pods,
    clean_manifest_obj,
    get_resource_yaml,
    apply_yaml_content,
    list_events,
    describe_resource,
    get_top,
    detect_network,
)

from .exec import (
    exec_command,
    list_dir,
    read_file,
    write_file,
    delete_path,
    mkdir_path,
    resolve_env_kubeconfig,
    # 交互式终端（PTY）：vim / top 等全屏程序需要真实 TTY
    PtySession,
    build_pty_argv,
    build_pty_script,
    spawn_kubectl_pty,
    interactive_command_hint,
    kubectl_available,
    INTERACTIVE_COMMANDS,
    READY_MARKER,
    READY_MARKER_RE,
    DEFAULT_COLS,
    DEFAULT_ROWS,
    DEFAULT_TERM,
)

__all__ = [
    "ENV_CONFIG_PATH",
    "DEFAULT_ENV_SEED",
    "load_envs",
    "save_envs",
    "list_envs",
    "get_env",
    "add_or_update_env",
    "set_current_env",
    "import_kubeconfig",
    "export_envs",
    "delete_env",
    "run_kubectl_env",
    "list_pods",
    "clean_manifest_obj",
    "get_resource_yaml",
    "apply_yaml_content",
    "list_events",
    "describe_resource",
    "get_top",
    "detect_network",
    "exec_command",
    "list_dir",
    "read_file",
    "write_file",
    "delete_path",
    "mkdir_path",
    "resolve_env_kubeconfig",
]


# ---- 以下来自原 core/k8s_snapshot.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""Kubernetes Pod 状态 / 日志快照核心逻辑（聚合兼容层）。

实现已拆分到子模块：k8s_kubectl(基础) / k8s_snapshot_fetch(抓取+编排) /
k8s_snapshot_render(HTML 渲染)。本文件仅重导出，外部 `from core.k8s import X` 不变。
"""

from .kubectl import (
    run_kubectl, stream_kubectl,
)

from .snapshot_fetch import (
    parse_pod,
    compute_age,
    classify,
    fetch_logs,
    run_snapshot,
)

from .snapshot_render import (
    parse_pod,
    compute_age,
    classify,
    render_html,
)

__all__ = [
    "run_kubectl",
    "parse_pod",
    "compute_age",
    "classify",
    "fetch_logs",
    "run_snapshot",
    "parse_pod",
    "compute_age",
    "classify",
    "render_html",
]

