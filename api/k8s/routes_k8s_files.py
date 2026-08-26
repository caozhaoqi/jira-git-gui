# -*- coding: utf-8 -*-
"""K8s 容器内文件操作路由：列表 / 读取 / 搜索 / 上传 / 删除 / 建目录。

拆分自 ``api/routes_k8s.py``，业务子域：通过 ``kubectl exec`` 在容器内做文件系统操作，
支撑「日志 / 配置落地」等排障场景。

注：原 ``routes_k8s.py`` 存在一处 bug —— ``/api/k8s/file/write`` 的路由装饰器与函数签名
被意外删除（仅残留函数体），导致该端点从未注册。此处**保留原行为**（不新增未测试的写端点），
将残留函数体以注释隔离，待后续明确需求后再补齐。
"""
import logging
import shlex

from fastapi import APIRouter
from pydantic import BaseModel

from core import k8s_manager as _k8s_mgr
from core.k8s import run_kubectl as _k8s_run_kubectl

logger = logging.getLogger("api.routes_k8s_files")
router = APIRouter()


class K8sFileReq(BaseModel):
    """容器内文件操作统一请求体（list/read/search 共用）。"""
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = "/"
    pattern: str = ""
    max_lines: int = 2000


@router.post("/api/k8s/file/list")
async def api_k8s_file_list(body: K8sFileReq):
    """列出容器内某路径下的文件（含目录标记）。"""
    env, pod, container, namespace, path = body.env, body.pod, body.container, body.namespace, body.path
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    try:
        out, rc, err = _k8s_run_kubectl(
            ["exec", pod] + (["-c", container] if container else [])
            + (["-n", namespace] if namespace else [])
            + ["--", "ls", "-la", path], kc, timeout=30,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    lines = out.splitlines()[1:]  # 去掉 total 行
    items = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 9:
            continue
        perm = parts[0]
        name = " ".join(parts[8:])
        items.append({
            "name": name,
            "is_dir": perm.startswith("d"),
            "size": parts[4] if len(parts) > 4 else "",
            "mtime": " ".join(parts[5:8]),
        })
    return {"ok": True, "items": items}


@router.post("/api/k8s/file/read")
async def api_k8s_file_read(body: K8sFileReq):
    """读取容器内文件内容（文本）。"""
    env, pod, container, namespace, path, max_lines = (
        body.env, body.pod, body.container, body.namespace, body.path, body.max_lines)
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    try:
        out, rc, err = _k8s_run_kubectl(
            ["exec", pod] + (["-c", container] if container else [])
            + (["-n", namespace] if namespace else [])
            + ["--", "cat", path], kc, timeout=30,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    lines = out.splitlines()
    if max_lines and len(lines) > max_lines:
        out = "\n".join(lines[:max_lines]) + f"\n... (truncated, total {len(lines)} lines)"
    return {"ok": True, "content": out}


@router.post("/api/k8s/file/search")
async def api_k8s_file_search(body: K8sFileReq):
    """在容器内按文本模式搜索文件内容（grep -rn）。"""
    env, pod, container, namespace, path, pattern, max_lines = (
        body.env, body.pod, body.container, body.namespace, body.path, body.pattern, body.max_lines)
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    if not pattern:
        return {"ok": False, "error": "pattern 为必填"}
    try:
        out, rc, err = _k8s_run_kubectl(
            ["exec", pod] + (["-c", container] if container else [])
            + (["-n", namespace] if namespace else [])
            + ["--", "grep", "-rn", "--", pattern, path], kc, timeout=30,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    # grep 无匹配返回 rc=1，属正常结果
    if rc != 0 and rc != 1:
        return {"ok": False, "error": err.strip()[:300]}
    lines = [l for l in out.splitlines() if l.strip()]
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append(f"... (truncated, total {len(out.splitlines())} matches)")
    return {"ok": True, "matches": lines}


@router.post("/api/k8s/file/upload")
async def api_k8s_file_upload(body: dict):
    """上传本地内容到容器内文件（base64 编码内容经 stdin 写入）。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    content_b64 = body.get("content", "")
    if not (pod and path and content_b64):
        return {"ok": False, "error": "pod / path / content 均为必填"}
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    try:
        import base64
        raw = base64.b64decode(content_b64)
    except Exception as ex:
        return {"ok": False, "error": f"content 不是合法 base64：{ex}"}
    # 用 tar 流式写入避免超大参数；这里走简单 stdin 方案
    try:
        out, rc, err = _k8s_run_kubectl(
            ["exec", pod] + (["-c", container] if container else [])
            + (["-n", namespace] if namespace else [])
            + ["--", "sh", "-c", f"cat > {shlex.quote(path)}"],
            kc, timeout=60, input=raw,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


@router.post("/api/k8s/file/delete")
async def api_k8s_file_delete(body: dict):
    """删除容器内文件。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    if not (pod and path):
        return {"ok": False, "error": "pod / path 均为必填"}
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    try:
        out, rc, err = _k8s_run_kubectl(
            ["exec", pod] + (["-c", container] if container else [])
            + (["-n", namespace] if namespace else [])
            + ["--", "rm", "-f", path], kc, timeout=30,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


@router.post("/api/k8s/file/mkdir")
async def api_k8s_file_mkdir(body: dict):
    """在容器内创建目录。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    if not (pod and path):
        return {"ok": False, "error": "pod / path 均为必填"}
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    try:
        out, rc, err = _k8s_run_kubectl(
            ["exec", pod] + (["-c", container] if container else [])
            + (["-n", namespace] if namespace else [])
            + ["--", "mkdir", "-p", path], kc, timeout=30,
        )
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


# --- 以下为原 routes_k8s.py 中遗留的孤立代码：/api/k8s/file/write 端点因装饰器与
#     函数签名缺失从未注册。保留以溯源，待明确需求后补齐（不建议直接启用，缺少权限/覆盖确认）。
# async def api_k8s_file_write(body: dict):
#     env = body.get("env", "")
#     pod = body.get("pod", "")
#     container = body.get("container", "")
#     path = body.get("path", "")
#     content = body.get("content", "")
#     kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
#     try:
#         _k8s_run_kubectl(["exec", pod] + (["-c", container] if container else [])
#                          + (["-n", ns] if ns else []) + ["--", "sh", "-c",
#                          f"cat > {path}"], kc, timeout=30, input=content.encode())
#     except Exception as ex:
#         return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
#     return {"ok": True}
