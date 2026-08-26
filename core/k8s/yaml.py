# -*- coding: utf-8 -*-
"""K8s 资源 YAML：Pod 列表 / 获取 / 清理 / 应用。

依赖 ``core.k8s.env.get_env`` 与 ``core.k8s.pods.run_kubectl_env``（基础封装在本包内）。
"""
import json
import os
import tempfile

import yaml
from core.errors import UserError
from .env import get_env
from .pods import run_kubectl_env


# 顶层始终剔除（运行时状态，不可被 apply 覆盖）
_TOP_DENY_KEYS = {"status"}

# metadata 下由服务端托管的字段（每次 get 都会变，编辑/回传时应剔除，避免无意义的 diff 与冲突）
_META_DENY_KEYS = {
    "resourceVersion", "uid", "creationTimestamp", "generation",
    "selfLink", "managedFields", "ownerReferences",
}

# metadata.annotations 中由 kubectl apply 写入、不应再次 apply 的字段
_META_DENY_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
}

# 部分资源在 spec 下由集群自动分配、不可直接 apply 的字段（按 kind 清理）
_SPEC_DENY_BY_KIND = {
    "Service": {"clusterIP", "clusterIPs", "healthCheckNodePort"},
    "Endpoints": {"subset"},          # 端点由控制器维护
    "Pod": {"nodeName"},              # 调度后由 kubelet 写入
    "PersistentVolumeClaim": {"volumeName"},
    "PodDisruptionBudget": set(),
}


def list_pods(env_name, selector=None, namespace=None):
    """列出 pod（精简信息），用于 YAML 管理界面的快速选择。"""
    args = ["get", "pods", "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    if selector:
        args += ["-l", selector]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("列出 Pod 失败：%s" % err.strip()[:400])
    try:
        items = json.loads(out).get("items", [])
    except Exception:
        raise UserError("kubectl 返回非 JSON（可能未连接集群）。")
    res = []
    for it in items:
        meta = it.get("metadata", {})
        st = it.get("status", {})
        cs = st.get("containerStatuses", []) or []
        res.append({
            "name": meta.get("name", "?"),
            "namespace": meta.get("namespace", ""),
            "phase": st.get("phase", ""),
            "restarts": max((c.get("restartCount", 0) for c in cs), default=0),
            "node": it.get("spec", {}).get("nodeName", ""),
        })
    return res


def clean_manifest_obj(obj):
    """递归清理从 ``kubectl get`` 出来的资源对象，剔除服务端托管字段，
    使其变成可直接编辑 / ``kubectl apply`` 的干净清单。

    仅删除「服务端写入、客户端不应管控」的字段，保留 spec / labels /
    annotations（除 last-applied 外）等用户侧内容，因此清理后仍可安全回传。
    """
    if not isinstance(obj, dict):
        return obj
    # 顶层运行时状态
    for k in _TOP_DENY_KEYS:
        obj.pop(k, None)
    # metadata
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        for k in list(meta.keys()):
            if k in _META_DENY_KEYS:
                meta.pop(k, None)
        ann = meta.get("annotations")
        if isinstance(ann, dict):
            for a in list(ann.keys()):
                if a in _META_DENY_ANNOTATIONS:
                    ann.pop(a, None)
            if not ann:
                meta.pop("annotations", None)
    # spec 按 kind 清理自动分配字段
    kind = obj.get("kind")
    spec = obj.get("spec")
    if isinstance(spec, dict) and kind in _SPEC_DENY_BY_KIND:
        for k in _SPEC_DENY_BY_KIND[kind]:
            spec.pop(k, None)
    return obj


def get_resource_yaml(env_name, kind, name, namespace=None, clean=True, raw=False):
    """获取资源 YAML。

    * ``raw=True`` 或 ``clean=False``：直接返回 ``kubectl get ... -o yaml``
      原始文本（含 status / 服务端字段）。
    * 默认 ``clean=True``：走 ``-o json`` 解析后剔除服务端托管字段，再以
      **稳定顺序**（sort_keys=False，保留 apiVersion/kind/metadata/spec 原序）
      重新序列化为「可编辑、可二次 apply」的干净 YAML。
    """
    if raw or not clean:
        args = ["get", kind, name, "-o", "yaml"]
    else:
        args = ["get", kind, name, "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("获取 %s/%s 失败：%s" % (kind, name, err.strip()[:400]))
    if raw or not clean:
        return out
    try:
        obj = json.loads(out)
    except Exception:
        raise UserError("kubectl 返回非 JSON（可能未连接集群或资源不存在）。")
    clean_manifest_obj(obj)
    return yaml.safe_dump(
        obj, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def apply_yaml_content(env_name, content, namespace=None):
    """把 YAML 内容修改后上传（kubectl apply -f）。返回 (stdout, stderr)。"""
    if not content or not content.strip():
        raise UserError("YAML 内容为空，无法上传。")
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="k8s_apply_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        args = ["apply", "-f", path, "--record=false"]
        if namespace:
            args += ["-n", namespace]
        elif (env := get_env(env_name)[1]).get("namespace"):
            args += ["-n", env["namespace"]]
        out, rc, err = run_kubectl_env(env_name, args, timeout=60)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    if rc != 0:
        raise UserError("kubectl apply 失败：%s" % err.strip()[:600])
    return out, err
