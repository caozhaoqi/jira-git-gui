# -*- coding: utf-8 -*-
"""K8s 环境管理 + 资源（Pod / YAML / 网络探测）路由。

拆分自 ``api/routes_k8s.py``，业务子域：多环境 kubeconfig 管理、Pod 列表、
资源 YAML 查看与网络连通性探测。

符号映射说明：原 ``routes_k8s.py`` 依赖的 ``save_env`` / ``switch_env`` /
``import_kubeconfig_env`` / ``export_env`` 在 core 拆分后已更名为
``add_or_update_env`` / ``set_current_env`` / ``import_kubeconfig`` / ``export_envs``；
当前环境读取改用 ``get_env(None)``（返回 ``(name, dict)``）。
"""
import asyncio
import logging

import json

from fastapi import APIRouter
from pydantic import BaseModel

from core import k8s_manager as _k8s_mgr
from core.k8s import (
    run_kubectl_async as _k8s_run_kubectl_async,
    list_envs as _k8s_list_envs,
    add_or_update_env as _k8s_add_or_update_env,
    set_current_env as _k8s_set_current_env,
    delete_env as _k8s_delete_env,
    import_kubeconfig as _k8s_import_kubeconfig,
    export_envs as _k8s_export_envs,
    get_env as _k8s_get_env,
)

logger = logging.getLogger("api.routes_k8s_env")
router = APIRouter()


class K8sEnvSave(BaseModel):
    name: str
    kubeconfig: str
    namespace: str = ""


class K8sYamlReq(BaseModel):
    env: str = ""
    kind: str = ""
    name: str = ""
    namespace: str = ""
    action: str = "get"      # 'get' | 'apply'
    clean: bool = True       # 仅 action='get' 使用（剔除服务端托管字段）
    content: str = ""        # 仅 action='apply' 使用


class K8sNetworkReq(BaseModel):
    env: str = ""
    extra_hosts: list = []   # 用户自定义内网探测目标（每行 host[:port]）
    target: str = ""         # 兼容老字段（已废弃，detect_network 不使用）


@router.get("/api/k8s/env")
async def api_k8s_env():
    """列出所有已保存的 K8s 环境与当前生效环境。"""
    envs = _k8s_list_envs()
    try:
        current = _k8s_get_env(None)[0]
    except Exception:
        current = None
    return {"environments": envs, "current": current}


@router.post("/api/k8s/env")
async def api_k8s_env_save(body: K8sEnvSave):
    """保存（新增 / 覆盖）一个 K8s 环境配置。"""
    try:
        _k8s_add_or_update_env(body.name, kubeconfig=body.kubeconfig,
                               namespace=body.namespace or None)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True}


@router.post("/api/k8s/env/switch")
async def api_k8s_env_switch(body: dict):
    """切换当前生效环境。"""
    name = body.get("name")
    try:
        _k8s_set_current_env(name)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True, "current": _k8s_get_env(None)[0]}


@router.post("/api/k8s/env/delete")
async def api_k8s_env_delete(body: dict):
    """删除一个已保存环境。"""
    name = body.get("name")
    try:
        _k8s_delete_env(name)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True}


@router.post("/api/k8s/env/import-kubeconfig")
async def api_k8s_env_import_kubeconfig(body: dict):
    """从 kubeconfig 内容导入环境（自动识别 context）。"""
    path = body.get("path", "")
    content = body.get("content", "")
    try:
        imported = _k8s_import_kubeconfig(path, content)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True, "imported": imported}


@router.get("/api/k8s/env/export")
async def api_k8s_env_export(name: str = ""):
    """导出某个环境的 kubeconfig 内容。"""
    try:
        envs = _k8s_export_envs(with_content=True)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if name:
        return {"ok": True, "kubeconfig": envs.get(name, {}).get("kubeconfig", "")}
    return {"ok": True, "envs": envs}


@router.get("/api/k8s/pods")
async def api_k8s_pods(env: str = "", namespace: str = ""):
    """列出指定环境的 Pod（带状态摘要），返回与前端 K8sPodsResp.pods 兼容的结构。"""
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    out, rc, err = await _k8s_run_kubectl_async(
        ["get", "pods"] + (["-n", namespace] if namespace else ["-A"]) + ["-o", "json"],
        kc, timeout=30,
    )
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    try:
        raw = json.loads(out)
        items = raw.get("items", []) if isinstance(raw, dict) else []
    except Exception:
        return {"ok": False, "error": "kubectl 返回非 JSON（可能未配置 kubeconfig / 上下文）"}
    pods = []
    for it in items:
        md = it.get("metadata", {}) or {}
        st = it.get("status", {}) or {}
        cs = st.get("containerStatuses", []) or []
        restarts = sum(c.get("restartCount", 0) for c in cs) if cs else 0
        pods.append({
            "name": md.get("name", ""),
            "namespace": md.get("namespace", ""),
            "phase": st.get("phase", ""),
            "restarts": restarts,
        })
    return {"ok": True, "pods": pods}


@router.post("/api/k8s/yaml")
async def api_k8s_yaml(body: K8sYamlReq):
    """获取 / 应用某资源的 YAML 清单（POST，与前端 apiPost 对齐）。

    - action='get'  → 返回干净/原始 YAML 文本（d.yaml）
    - action='apply' → kubectl apply -f，返回 (d.stdout, d.stderr)
    """
    try:
        if body.action == "apply":
            if not body.content or not body.content.strip():
                return {"ok": False, "error": "YAML 内容为空，无法上传。"}
            out, err = await asyncio.to_thread(
                _k8s_mgr.apply_yaml_content,
                body.env, body.content, body.namespace or None)
            return {"ok": True, "stdout": out, "stderr": err}
        # 默认 action='get'
        if not body.kind or not body.name:
            return {"ok": False, "error": "kind 与 name 不能为空。"}
        text = await asyncio.to_thread(
            _k8s_mgr.get_resource_yaml,
            body.env, body.kind, body.name, body.namespace or None, clean=body.clean)
        return {"ok": True, "yaml": text}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/network")
async def api_k8s_network(body: K8sNetworkReq):
    """探测本机到指定环境的网络连通性（POST，与前端 apiPost 对齐）。"""
    try:
        res = await asyncio.to_thread(
            _k8s_mgr.detect_network, body.env, extra_hosts=body.extra_hosts or None)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    return {"ok": True, **res}
