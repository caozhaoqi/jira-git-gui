# -*- coding: utf-8 -*-
"""统一诊断路由 —— CF 云函数错误诊断 + K8s 基础设施诊断。

POST /api/diagnose：接收错误文本 + K8s 环境信息，一次调用拿到联合诊断上下文。
"""
from fastapi import APIRouter, HTTPException

from api.common import logger
from api.schemas import FullDiagnoseReq, UnifiedDiagnoseReq
from api.full_diagnose import full_diagnose
from api.unified_diagnose import unified_diagnose, k8s_collect_diagnostics

router = APIRouter()


def _http_error(e: Exception) -> HTTPException:
    msg = str(e)
    if isinstance(e, (PermissionError,)):
        return HTTPException(401, msg)
    if isinstance(e, (ConnectionError,)):
        return HTTPException(502, msg)
    if isinstance(e, (TimeoutError,)):
        return HTTPException(504, msg)
    if isinstance(e, (ValueError,)):
        return HTTPException(400, msg)
    return HTTPException(500, msg)


@router.post("/api/diagnose/full")
async def api_full_diagnose(req: FullDiagnoseReq):
    """一键诊断：CF + K8s + 远程 dynamic_log + JSON 元数据 + 代码规范。"""
    try:
        return await full_diagnose(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/diagnose")
async def api_unified_diagnose(req: UnifiedDiagnoseReq):
    """统一诊断：CF 云函数错误 + K8s 基础设施联合诊断。

    一次调用同时采集应用层（CF 解析/词典/Wiki/源码/日志）和基础设施层
    （Pod状态/事件/崩溃日志/资源Top）的诊断素材，返回合并后的诊断上下文，
    让 AI 能同时看到两层证据，快速判断是代码问题还是环境问题。
    """
    try:
        return unified_diagnose(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.get("/api/diagnose/k8s")
async def api_k8s_diagnose(env: str = "", namespace: str = "", pod_filter: str = "", tail: int = 100):
    """仅 K8s 诊断：采集集群异常 Pod + Warning 事件 + 崩溃日志。

    用于前端单独查看 K8s 侧诊断信息，或排查非 CF 相关的基础设施问题。
    """
    try:
        return k8s_collect_diagnostics(
            env=env, namespace=namespace,
            pod_filter=pod_filter, tail=tail,
        )
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.get("/api/diagnose/k8s/errdict")
async def api_k8s_errdict():
    """返回 K8s 错误模式词典（前端展示用）。"""
    from api.unified_diagnose import _load_k8s_errdict
    d = _load_k8s_errdict()
    return {"ok": bool(d), "data": d}
