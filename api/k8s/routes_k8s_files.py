# -*- coding: utf-8 -*-
"""K8s 容器内文件操作路由：列表 / 读取 / 搜索 / 写入 / 上传 / 删除 / 建目录。

拆分自 ``api/routes_k8s.py``，业务子域：通过 ``kubectl exec`` 在容器内做文件系统操作，
支撑「日志 / 配置落地」等排障场景。

契约对齐说明（与 frontend/web-react K8sFiles 对齐，避免再次漂移）：
  - file/list   响应字段 ``entries``（每项含 ``type: 'dir'|'file'``），非 ``items``
  - file/read   请求字段 ``max_bytes``；响应含 ``is_binary`` / ``truncated``
  - file/search 请求字段 ``q``（兼容老 ``pattern``）；响应 ``results``（对象数组，非原始 grep 行）
  - file/write  接收明文 ``content``（前端保存编辑器内容）
  - file/upload 接收 base64 ``data``（兼容老 ``content``）
"""
import base64
import logging
import re
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
    pattern: str = ""        # 兼容老字段
    q: str = ""              # 搜索关键词（前端使用）
    max_lines: int = 2000
    max_bytes: int = 0       # 读取上限（前端使用）


def _resolve(env, namespace):
    """返回 (kc, namespace)，namespace 可被环境默认命名空间补全。"""
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    return kc, namespace


def _exec(env, pod, container, namespace, args, timeout=30, input=None):
    """在容器内执行命令，返回 (out, rc, err)。

    当 ``input`` 非 None（写文件/上传）时自动加 ``-i``，使 kubectl exec 真正消费 stdin。
    """
    kc, ns = _resolve(env, namespace)
    cmd = (["exec"] + (["-i"] if input is not None else []) + [pod]
           + (["-c", container] if container else [])
           + (["-n", ns] if ns else []) + ["--"] + args)
    try:
        return _k8s_run_kubectl(cmd, kc, timeout=timeout, input=input)
    except Exception as ex:
        return "", 1, getattr(ex, "message", None) or str(ex)


@router.post("/api/k8s/file/list")
async def api_k8s_file_list(body: K8sFileReq):
    """列出容器内某路径下的文件（含目录标记）。"""
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["ls", "-la", body.path])
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    lines = out.splitlines()[1:]  # 去掉 total 行
    entries = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 9:
            continue
        perm = parts[0]
        name = " ".join(parts[8:])
        size_raw = parts[4] if len(parts) > 4 else ""
        size = int(size_raw) if size_raw.isdigit() else size_raw
        entries.append({
            "name": name,
            "type": "dir" if perm.startswith("d") else "file",
            "size": size,
            "modtime": " ".join(parts[5:8]),
        })
    return {"ok": True, "entries": entries}


@router.post("/api/k8s/file/read")
async def api_k8s_file_read(body: K8sFileReq):
    """读取容器内文件内容（文本）。"""
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["cat", body.path], timeout=30)
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    is_binary = "\x00" in out
    if is_binary:
        return {"ok": True, "content": "", "is_binary": True, "truncated": False}
    truncated = False
    max_bytes = body.max_bytes or 0
    max_lines = body.max_lines or 0
    if max_bytes and len(out) > max_bytes:
        out = out[:max_bytes] + f"\n... (truncated, total {len(out)} bytes)"
        truncated = True
    elif max_lines and len(out.splitlines()) > max_lines:
        kept = out.splitlines()[:max_lines]
        out = "\n".join(kept) + f"\n... (truncated, total {len(out.splitlines())} lines)"
        truncated = True
    return {"ok": True, "content": out, "is_binary": False, "truncated": truncated}


@router.post("/api/k8s/file/search")
async def api_k8s_file_search(body: K8sFileReq):
    """在容器内按文本模式搜索文件内容（grep -rn），结果解析为对象数组。"""
    pattern = body.pattern or body.q or ""
    if not pattern:
        return {"ok": False, "error": "pattern(q) 为必填"}
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["grep", "-rn", "--", pattern, body.path], timeout=30)
    # grep 无匹配返回 rc=1，属正常结果
    if rc != 0 and rc != 1:
        return {"ok": False, "error": err.strip()[:300]}
    results = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        m = re.match(r"^(.+?):(\d+):(.*)$", ln)
        if m:
            results.append({"path": m.group(1), "line": int(m.group(2)), "snippet": m.group(3)})
        else:
            results.append({"path": body.path, "line": 0, "snippet": ln})
    return {"ok": True, "results": results, "total": len(results)}


@router.post("/api/k8s/file/write")
async def api_k8s_file_write(body: dict):
    """把编辑器中的明文内容写回容器文件（kubectl exec cat > path）。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    content = body.get("content", "")
    if not (pod and path):
        return {"ok": False, "error": "pod / path 均为必填"}
    if content is None:
        return {"ok": False, "error": "content 为必填"}
    out, rc, err = _exec(env, pod, container, namespace,
                         ["sh", "-c", f"cat > {shlex.quote(path)}"],
                         timeout=60, input=content.encode("utf-8"))
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


@router.post("/api/k8s/file/upload")
async def api_k8s_file_upload(body: dict):
    """上传本地内容到容器内文件（base64 编码内容经 stdin 写入）。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    # 前端上传走 data（base64），兼容老 content 字段
    content_b64 = body.get("data")
    if not content_b64:
        content_b64 = body.get("content", "")
    if not (pod and path and content_b64):
        return {"ok": False, "error": "pod / path / data(content) 均为必填"}
    try:
        raw = base64.b64decode(content_b64)
    except Exception as ex:
        return {"ok": False, "error": f"content 不是合法 base64：{ex}"}
    out, rc, err = _exec(env, pod, container, namespace,
                         ["sh", "-c", f"cat > {shlex.quote(path)}"],
                         timeout=60, input=raw)
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
    out, rc, err = _exec(env, pod, container, namespace, ["rm", "-f", path])
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
    out, rc, err = _exec(env, pod, container, namespace, ["mkdir", "-p", path])
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}
