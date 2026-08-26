# -*- coding: utf-8 -*-
"""K8s 面板的后台任务包装。

把 ``core.k8s`` 的函数包装成「可被 Worker 注入回调」的薄函数，
供 ``main_window`` 在子线程中调用（日志/进度信号由 Worker 桥接到 UI）。
"""
from core import k8s_manager as km


# Worker 按参数名注入 on_log/on_progress/should_cancel；这些包装让 core.k8s
# 的函数能以统一方式在子线程跑，并把日志信号接到 UI。
def yaml_get_task(env, kind, name, namespace, clean=True):
    return km.get_resource_yaml(env, kind, name, namespace or None, clean=clean)


def yaml_apply_task(env, kind, name, namespace, content):
    out, err = km.apply_yaml_content(env, content, namespace or None)
    return {"stdout": out, "stderr": err}


def net_task(env, extra_hosts, on_log=None):
    return km.detect_network(env, extra_hosts or None, on_log=on_log)
