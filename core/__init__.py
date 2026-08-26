"""核心逻辑层：纯 Python，无任何 GUI 依赖。

包含：
- constants : 路径、代理、超时等运行时常量
- models    : 数据模型（dataclass）
- client    : JiraGitClient，封装所有对 Jira Git 插件的网络/解析/克隆/下载操作
- config    : 配置（连接 / 会话 / 合并 / CF / HCM）
- diff      : 目录差异扫描、差异计算、合并（兼容旧名 differ / diff_scan / diff_diff / diff_merge）
- k8s       : Kubernetes 多环境运维（兼容旧名 k8s_manager / k8s_snapshot）
- sync      : 同步历史（兼容旧名 sync_history）

为向后兼容，下列旧顶层模块名仍可作为属性访问：
    from core import differ, diff_scan, diff_diff, diff_merge, k8s_manager, k8s_snapshot, sync_history
"""

from . import client
from . import config
from . import diff as differ
from . import diff as diff_scan
from . import diff as diff_diff
from . import diff as diff_merge
from . import k8s as k8s_manager
from . import k8s as k8s_snapshot
from . import sync as sync_history

# 旧子模块名兼容（原 core/<name>.py 已迁入对应子包，按旧名仍可访问）。
# 注意：必须用 importlib 拿「模块对象」，因为部分子包 __init__ 会把同名函数
# （如 scan_local / merge_entries）导入并遮蔽模块名。
import importlib

_OLD_MODULES = {
    "client_connection": "core.client.connection",
    "client_repos": "core.client.repos",
    "client_browse": "core.client.browse",
    "client_files": "core.client.files",
    "client_clone": "core.client.clone",
    "config_connect": "core.config.connect",
    "config_session": "core.config.session",
    "config_merge": "core.config.merge",
    "config_cf": "core.config.cf",
    "config_hcm": "core.config.hcm",
    "diff_models": "core.diff.models",
    "diff_scan_local": "core.diff.scan_local",
    "diff_scan_remote": "core.diff.scan_remote",
    "diff_diff_core": "core.diff.diff_core",
    "diff_merge_entries": "core.diff.merge_entries",
    "diff_merge_file": "core.diff.merge_file",
    "diff_normalize": "core.diff.normalize",
    "k8s_env": "core.k8s.env",
    "k8s_events": "core.k8s.events",
    "k8s_pods": "core.k8s.pods",
    "k8s_exec": "core.k8s.exec",
    "k8s_exec_cmd": "core.k8s.exec_cmd",
    "k8s_exec_fs": "core.k8s.exec_fs",
    "k8s_kubectl": "core.k8s.kubectl",
    "k8s_models": "core.k8s.models",
    "k8s_netdetect": "core.k8s.netdetect",
    "k8s_snapshot_fetch": "core.k8s.snapshot_fetch",
    "k8s_snapshot_render": "core.k8s.snapshot_render",
    "k8s_yaml": "core.k8s.yaml",
    "sync_history_store": "core.sync.store",
    "sync_history_view": "core.sync.view",
}
for _old, _new in _OLD_MODULES.items():
    globals()[_old] = importlib.import_module(_new)

__all__ = [
    "client", "config",
    "diff", "differ", "diff_scan", "diff_diff", "diff_merge",
    "k8s", "k8s_manager", "k8s_snapshot",
    "sync", "sync_history",
    "client_connection", "client_repos", "client_browse", "client_files", "client_clone",
    "config_connect", "config_session", "config_merge", "config_cf", "config_hcm",
    "diff_models", "diff_scan_local", "diff_scan_remote", "diff_diff_core",
    "diff_merge_entries", "diff_merge_file", "diff_normalize",
    "k8s_env", "k8s_events", "k8s_pods", "k8s_exec", "k8s_exec_cmd", "k8s_exec_fs",
    "k8s_kubectl", "k8s_models", "k8s_netdetect", "k8s_snapshot_fetch",
    "k8s_snapshot_render", "k8s_yaml",
    "sync_history_store", "sync_history_view",
]
