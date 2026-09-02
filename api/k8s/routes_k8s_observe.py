# -*- coding: utf-8 -*-
"""K8s 可观测性路由：events / describe / top。

拆分自 ``api/routes_k8s.py``，业务子域：事件列表、资源详情、节点 / Pod 资源占用排名。
"""
import logging
import re

from fastapi import APIRouter

from core import k8s_manager as _k8s_mgr
from core.k8s import run_kubectl_async as _k8s_run_kubectl_async

logger = logging.getLogger("api.routes_k8s_observe")
router = APIRouter()

# ------------------------------------------------------------------- 时间参数校验
# kubectl --since / --until 接受的格式：
#   --since  仅相对时长（如 30m / 1h / 2d），不接受绝对时间；
#   --until  相对时长或 RFC3339 绝对时间（如 2026-08-25T10:00:00Z）。
# 任意无法解析的字符串（例如误填的 "error"）若直接透传给 kubectl，会让 `kubectl logs`
# 以 `invalid argument "error" for "--since" flag` 失败，进而被后端包成 404 抛给用户。
# 这里做容错归一化：非法值忽略该筛选并告警，而不是让整次查询崩溃。
_K8S_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?(ns|us|µs|ms|s|m|h|d)$")
_K8S_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


def _k8s_normalize_time_arg(name: str, value, allow_abs: bool):
    """归一化 since/until 时间参数。

    返回 ``(normalized_or_None, warning_or_None)``：
    - 空值        → (None, None)，表示不使用该筛选；
    - 合法值      → (去空格后的原值, None)；
    - 非法值      → (None, 警告文本)，容错忽略，避免 kubectl 崩溃。
    """
    if value is None or not str(value).strip():
        return None, None
    v = str(value).strip()
    if _K8S_DURATION_RE.match(v):
        return v, None
    if allow_abs and _K8S_RFC3339_RE.match(v):
        return v, None
    hint = (
        "应为相对时长如 30m/1h/2d" + ("，或 RFC3339 时间如 2026-08-25T10:00:00Z" if allow_abs else "")
    )
    return None, f"参数 {name}={v!r} 不是合法的 kubectl 时间格式（{hint}），已忽略该筛选"


@router.get("/api/k8s/events")
async def api_k8s_events(env: str = "", namespace: str = "", kind: str = "", name: str = ""):
    """列出事件（可按资源过滤）。"""
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    out, rc, err = await _k8s_run_kubectl_async(
        ["get", "events"] + (["-n", namespace] if namespace else ["-A"]) + ["-o", "json"],
        kc, timeout=30,
    )
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True, "raw": out}


@router.get("/api/k8s/describe")
async def api_k8s_describe(env: str = "", kind: str = "", name: str = "", namespace: str = ""):
    """describe 某个资源（返回纯文本）。"""
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    out, rc, err = await _k8s_run_kubectl_async(
        ["describe", kind, name] + (["-n", namespace] if namespace else []),
        kc, timeout=30,
    )
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True, "text": out}


@router.get("/api/k8s/top")
async def api_k8s_top(env: str = "", scope: str = "pods", namespace: str = ""):
    """节点 / Pod 资源占用排名（top）。"""
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    args = ["top", scope] + (["-n", namespace] if namespace and scope == "pods" else [])
    out, rc, err = await _k8s_run_kubectl_async(args, kc, timeout=30)
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True, "text": out}
