# -*- coding: utf-8 -*-
"""K8s 运维路由（由 api/server.py 拆分而来）。

SSE 事件总线统一走 ``api.eventbus``（全局唯一实例，避免 api.server 因
``python -m`` 启动产生 __main__ / api.server 双实例导致事件总线分叉）；
任务状态等本地全局在本模块内维护。
"""
import asyncio
import json
import logging
import threading
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket

import os
import re
import time
from pathlib import Path
from fastapi.responses import FileResponse, PlainTextResponse
from core import k8s_manager as _k8s_mgr
from core.k8s_manager import run_kubectl as _k8s_run_kubectl
from core.k8s_snapshot import run_snapshot as _k8s_run_snapshot, fetch_logs as _k8s_fetch_logs
from api.schemas import K8sEnvReq, K8sExecReq, K8sFileDeleteReq, K8sFileListReq, K8sFileMkdirReq, K8sFileReadReq, K8sFileSearchReq, K8sFileUploadReq, K8sFileWriteReq, K8sNetworkReq, K8sSnapshotReq, K8sYamlReq
from api.eventbus import broadcast

logger = logging.getLogger("api.routes_k8s")
router = APIRouter()

_k8s_cancel = threading.Event()
_k8s_running = False
_k8s_out_dir = {"dir": None}   # 最近一次输出目录，用于提供日志 / 报告下载
# 最近一次快照使用的 kubeconfig / namespace，供「查看日志」实时回退到集群拉取
_k8s_snap_meta = {"kubeconfig": None, "namespace": None}


@router.post("/api/k8s/snapshot")
async def api_k8s_snapshot(req: K8sSnapshotReq):
    """触发一次 K8s Pod 状态 / 日志快照（后台线程执行，SSE 推送进度）。"""
    global _k8s_running
    if _k8s_running:
        raise HTTPException(status_code=409,
                            detail="已有快照任务在运行中，请先取消或等待完成。")
    opts = req.model_dump()
    # 若指定环境，解析其 kubeconfig / 命名空间（覆盖裸参数）
    if opts.get("env"):
        try:
            kc, ns = _k8s_mgr.resolve_env_kubeconfig(opts["env"])
            opts["kubeconfig"] = kc or opts.get("kubeconfig")
            if ns and not opts.get("namespace"):
                opts["namespace"] = ns
        except Exception as ex:
            return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}

    def _do() -> None:
        global _k8s_running
        _k8s_running = True
        _k8s_cancel.clear()
        try:
            # 记录本次快照的 kubeconfig / namespace，供日志查看接口实时回退使用
            _k8s_snap_meta["kubeconfig"] = opts.get("kubeconfig")
            _k8s_snap_meta["namespace"] = opts.get("namespace")
            result = _k8s_run_snapshot(
                opts,
                on_log=lambda m: broadcast(
                    "k8s_log", {"msg": m, "ts": time.strftime("%H:%M:%S")}
                ),
                on_progress=lambda done, total, name: broadcast(
                    "k8s_progress",
                    {
                        "done": done,
                        "total": total,
                        "pct": round(done / total * 100) if total else 0,
                        "name": name,
                    },
                ),
                should_cancel=_k8s_cancel.is_set,
            )
            _k8s_out_dir["dir"] = result["out_dir"]
            broadcast(
                "k8s_done",
                {
                    "summary": result["summary"],
                    "records": result["records"],
                    "out_dir": result["out_dir"],
                    "report": result["report"],
                },
            )
            logger.info("K8s 快照完成: %s", result["out_dir"])
        except Exception as ex:  # 含 UserError（配置类错误）
            msg = getattr(ex, "message", None) or str(ex)
            broadcast("k8s_error", {"message": msg})
            logger.error("K8s 快照失败: %s", ex)
        finally:
            _k8s_running = False
            broadcast("k8s_finished", {"running": False})

    threading.Thread(target=_do, name="k8s-snapshot", daemon=True).start()
    return {"ok": True, "msg": "快照任务已启动"}


@router.post("/api/k8s/cancel")
async def api_k8s_cancel():
    """取消正在进行的快照任务。"""
    _k8s_cancel.set()
    return {"ok": True, "msg": "已发送取消信号"}


@router.get("/api/k8s/report")
async def api_k8s_report(download: bool = False):
    """打开 / 下载最近一次生成的 report.html。"""
    d = _k8s_out_dir["dir"]
    if not d:
        raise HTTPException(status_code=404, detail="尚未生成任何快照报告。")
    path = Path(d) / "report.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件不存在。")
    return FileResponse(
        str(path),
        media_type="text/html",
        filename="report.html" if download else None,
    )


@router.get("/api/k8s/log")
async def api_k8s_log(
    name: str,
    env: str = "",
    container: str = "",
    tail: int = 200,
    previous: bool = False,
):
    """读取某个 Pod 的日志（供主面板与独立日志查看页共用）。

    - 优先读取快照落盘文件（按 container 名匹配 ``{name}__{container}.log``）；
    - 落盘无则实时 ``kubectl logs`` 向集群拉取，避免直接 404。
    - ``env`` 优先用于解析 kubeconfig / 命名空间，回退到快照上下文。
    """
    d = _k8s_out_dir["dir"]
    # 1) 快照落盘文件优先
    if d:
        logs_dir = Path(d) / "logs"
        if logs_dir.exists():
            if container:
                f = logs_dir / ("%s__%s.log" % (name, container))
                if f.exists():
                    return PlainTextResponse(f.read_text(encoding="utf-8", errors="replace"))
            else:
                parts = []
                for f in sorted(logs_dir.glob(f"{name}*.log")):
                    parts.append(
                        f"===== {f.name} =====\n"
                        + f.read_text(encoding="utf-8", errors="replace")
                    )
                if parts:
                    return PlainTextResponse("\n\n".join(parts))

    # 2) 解析 kubeconfig / 命名空间：env 优先，回退快照上下文
    kc, ns = (None, None)
    if env:
        kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if not kc:
        kc = _k8s_snap_meta.get("kubeconfig")
        ns = ns or _k8s_snap_meta.get("namespace")
    if not kc:
        raise HTTPException(
            status_code=404,
            detail="未找到该 Pod 的快照日志，且尚未连接集群（请先在当前环境运行一次快照或指定环境）。",
        )
    try:
        tail = max(1, min(int(tail), 5000))
    except Exception:
        tail = 200
    ns_args = ["-n", ns] if ns else []

    # 3) 指定容器 → 直接实时抓取（单容器场景）
    if container:
        log = _k8s_fetch_logs(name, container, kc, ns, tail, previous, timeout=30)
        return PlainTextResponse(log)

    # 4) 未指定容器：先试单容器，再试多容器
    out, rc, err = _k8s_run_kubectl(
        ["logs", name] + ns_args + ["--tail", str(tail)] + (["--previous"] if previous else []),
        kc, timeout=30,
    )
    if rc == 0 and out.strip():
        return PlainTextResponse(out)

    # 4b) 多容器场景：列出容器分别抓取
    if "container name must be specified" in err or "a container name must be specified" in err:
        po, prc, perr = _k8s_run_kubectl(
            ["get", "pod", name, "-o", "json"] + ns_args, kc, timeout=30
        )
        containers = []
        if prc == 0:
            try:
                obj = json.loads(po)
                containers = [c.get("name") for c in obj.get("spec", {}).get("containers", [])]
            except Exception:
                containers = []
        parts = []
        for c in containers:
            log = _k8s_fetch_logs(name, c, kc, ns, tail, previous, timeout=30)
            parts.append("===== container: %s =====\n%s" % (c, log))
        if parts:
            return PlainTextResponse("\n\n".join(parts))

    raise HTTPException(
        status_code=404,
        detail=f"实时获取 Pod {name} 日志失败：{err.strip()[:300]}",
    )


@router.get("/api/k8s/pod-containers")
async def api_k8s_pod_containers(name: str, env: str = "", namespace: str = ""):
    """列出某 Pod 的容器名，供独立日志查看页的容器选择器使用。"""
    kc, ns = (None, None)
    if env:
        kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if not kc:
        kc = _k8s_snap_meta.get("kubeconfig")
        ns = ns or _k8s_snap_meta.get("namespace")
    if not kc:
        raise HTTPException(status_code=404, detail="尚未连接集群，无法获取容器列表。")
    ns_args = ["-n", namespace] if namespace else (["-n", ns] if ns else [])
    out, rc, err = _k8s_run_kubectl(
        ["get", "pod", name, "-o", "json"] + ns_args, kc, timeout=30
    )
    if rc != 0:
        raise HTTPException(status_code=502, detail=f"kubectl 获取 Pod 失败：{err.strip()[:300]}")
    try:
        obj = json.loads(out)
        containers = [c.get("name") for c in obj.get("spec", {}).get("containers", [])]
        ns = obj.get("metadata", {}).get("namespace", ns or "")
    except Exception as ex:
        raise HTTPException(status_code=502, detail=f"解析 Pod 失败：{ex}")
    return {"ok": True, "name": name, "namespace": ns, "containers": containers}


# --------------------------------------------------------------------------- #
#  K8s 多环境 / Pod YAML / 网络检测
# --------------------------------------------------------------------------- #
@router.get("/api/k8s/env")
async def api_k8s_env_list():
    """返回环境列表与当前环境。"""
    data = _k8s_mgr.load_envs()
    return {
        "environments": [
            {"name": n, **e, "is_current": n == data.get("current")}
            for n, e in data["environments"].items()
        ],
        "current": data.get("current"),
    }


@router.post("/api/k8s/env")
async def api_k8s_env_save(req: K8sEnvReq):
    """新增 / 更新一个环境。"""
    data = _k8s_mgr.add_or_update_env(
        req.name, req.label, req.kubeconfig, req.context, req.namespace, req.intranet_hosts
    )
    return {"ok": True, "current": data.get("current")}


@router.post("/api/k8s/env/switch")
async def api_k8s_env_switch(name: str = ""):
    """切换当前环境（同时记录 kubeconfig，供「查看日志」实时回退使用）。"""
    data = _k8s_mgr.set_current_env(name)
    try:
        kc, ns = _k8s_mgr.resolve_env_kubeconfig(name or data.get("current"))
        _k8s_snap_meta["kubeconfig"] = kc
        _k8s_snap_meta["namespace"] = ns
    except Exception:
        pass
    return {"ok": True, "current": data.get("current")}


@router.post("/api/k8s/env/delete")
async def api_k8s_env_delete(name: str = ""):
    data = _k8s_mgr.delete_env(name)
    return {"ok": True, "current": data.get("current")}


@router.post("/api/k8s/yaml")
async def api_k8s_yaml(req: K8sYamlReq):
    """获取资源 YAML（get）或修改后上传（apply）。"""
    try:
        if req.action == "get":
            if not req.name:
                raise _UserError("请填写资源名称。")
            yaml_text = _k8s_mgr.get_resource_yaml(
                req.env, req.kind, req.name, req.namespace or None,
                clean=req.clean)
            return {"ok": True, "yaml": yaml_text}
        elif req.action == "apply":
            out, err = _k8s_mgr.apply_yaml_content(
                req.env, req.content, req.namespace or None)
            return {"ok": True, "stdout": out, "stderr": err}
        raise _UserError("未知 action：%s" % req.action)
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.get("/api/k8s/pods")
async def api_k8s_pods(env: str = "", namespace: str = "", selector: str = ""):
    """列出指定环境的 Pod（用于 YAML 管理界面快速选择并自动获取）。"""
    try:
        items = _k8s_mgr.list_pods(env or "dev", selector or None, namespace or None)
        return {"ok": True, "pods": items}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/network")
async def api_k8s_network(req: K8sNetworkReq):
    """检测当前到指定环境的网络状况（含内网探测）。"""
    try:
        result = _k8s_mgr.detect_network(req.env, req.extra_hosts or None)
        return {"ok": True, **result}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


# --------------------------------------------------------------------------- #
#  K8s 事件 / 描述 / Top（只读可观测性，均支持 env 参数）
# --------------------------------------------------------------------------- #
@router.get("/api/k8s/events")
async def api_k8s_events(
    env: str = "",
    namespace: str = "",
    kind: str = "",
    name: str = "",
    limit: int = 200,
    all_ns: bool = False,
):
    """列出集群事件（按时间倒序，Warning 置顶标红由前端处理）。"""
    try:
        d = _k8s_mgr.list_events(
            env or "dev", namespace or None, kind or None, name or None,
            int(limit) if limit else 200, bool(all_ns))
        return {"ok": True, **d}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.get("/api/k8s/describe")
async def api_k8s_describe(
    env: str = "",
    kind: str = "",
    name: str = "",
    namespace: str = "",
):
    """kubectl describe 资源，返回原始文本 + 相关事件。"""
    if not kind or not name:
        return {"ok": False, "error": "请指定资源类型(kind)与名称(name)。"}
    try:
        d = _k8s_mgr.describe_resource(env or "dev", kind, name, namespace or None)
        return {"ok": True, **d}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.get("/api/k8s/top")
async def api_k8s_top(env: str = "", scope: str = "pods", namespace: str = ""):
    """kubectl top pods/nodes，按内存消耗降序。"""
    try:
        d = _k8s_mgr.get_top(env or "dev", scope or "pods", namespace or None)
        return {"ok": True, **d}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


# --------------------------------------------------------------------------- #
#  K8s 交互式终端（Xshell 式）/ 文件浏览器（Xftp 式）
# --------------------------------------------------------------------------- #
@router.post("/api/k8s/exec")
async def api_k8s_exec(req: K8sExecReq):
    """在 Pod 内一次性执行命令（管道模式，不带 -t）。"""
    try:
        output, new_cwd = _k8s_mgr.exec_command(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.command, req.cwd or None)
        resp = {"ok": True, "output": output}
        if new_cwd:
            resp["cwd"] = new_cwd
        return resp
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/file/list")
async def api_k8s_file_list(req: K8sFileListReq):
    """列出 Pod 内目录（Xftp 式文件浏览器）。"""
    try:
        entries = _k8s_mgr.list_dir(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.path or "/")
        return {"ok": True, "path": req.path or "/", "entries": entries}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/file/read")
async def api_k8s_file_read(req: K8sFileReadReq):
    """读取 Pod 内文本 / 二进制文件内容。"""
    try:
        max_bytes = int(req.max_bytes) if req.max_bytes else 200000
        content, is_binary = _k8s_mgr.read_file(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.path, max_bytes)
        resp = {"ok": True, "content": content, "is_binary": is_binary}
        # 通过 stat 判断真实大小，确定是否截断
        try:
            size = _k8s_mgr._file_size_bytes(
                req.env, req.pod, req.container or None, req.namespace or None,
                req.path)
            if size is not None and size > max_bytes:
                resp["truncated"] = True
        except Exception:
            pass
        return resp
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/file/search")
async def api_k8s_file_search(req: K8sFileSearchReq):
    """在 Pod 容器内按路径递归搜索文件内容（kubectl exec grep）。

    返回 [{path, line, snippet}]；最多 200 条，超时 30s。
    """
    q = (req.q or "").strip()
    if not q:
        return {"ok": False, "error": "搜索关键词不能为空"}
    path = (req.path or "/").strip() or "/"
    try:
        # 容器内 grep 兼容性：busybox/gnu 都支持 -r -n -I（跳过二进制）；-m 5 限制单文件条数
        script = (
            f'grep -rnI -m 5 -- "{q}" "{path}" 2>/dev/null '
            f"| head -n 200"
        )
        out, _cwd = _k8s_mgr.exec_command(
            req.env, req.pod, req.container or None, req.namespace or None,
            script, cwd=None, timeout=30)
        results = []
        seen = set()
        for line in (out or "").splitlines():
            line = line.rstrip("\r")
            if not line:
                continue
            # 格式：/path:lineno:snippet（Windows/带冒号路径时取第一个冒号后数字）
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not m:
                continue
            fp, ln, sn = m.group(1), int(m.group(2)), m.group(3)
            key = (fp, ln)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "path": fp,
                "line": ln,
                "snippet": sn[:300],
            })
            if len(results) >= 200:
                break
        return {"ok": True, "results": results, "total": len(results)}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}
    """写文本 / 二进制文件到 Pod（二进制：base64 文本经容器内 base64 -d 解码）。"""
    try:
        if req.encoding == "base64":
            # 传入的是 base64 文本，必须原样交给容器内 `base64 -d` 解码，
            # 若先在本机 b64decode 再喂给 base64 -d 会造成「双重解码」损坏文件。
            payload = req.content
            binary = True
        else:
            payload = req.content
            binary = False
        _k8s_mgr.write_file(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.path, payload, binary=binary)
        return {"ok": True}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/file/upload")
async def api_k8s_file_upload(req: K8sFileUploadReq):
    """上传二进制文件到 Pod（data 始终为 base64 文本，交由容器内 base64 -d 解码写入）。"""
    try:
        # data 是 base64 文本：直接透传给容器内 `base64 -d > path`，
        # 不要在本机先解码，否则会造成双重解码。
        _k8s_mgr.write_file(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.path, req.data, binary=True)
        return {"ok": True}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/file/delete")
async def api_k8s_file_delete(req: K8sFileDeleteReq):
    """删除 Pod 内文件或目录。"""
    try:
        _k8s_mgr.delete_path(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.path, is_dir=bool(req.is_dir))
        return {"ok": True}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.post("/api/k8s/file/mkdir")
async def api_k8s_file_mkdir(req: K8sFileMkdirReq):
    """在 Pod 内创建目录（含父级）。"""
    try:
        _k8s_mgr.mkdir_path(
            req.env, req.pod, req.container or None, req.namespace or None,
            req.path)
        return {"ok": True}
    except Exception as ex:
        return {"ok": False, "error": getattr(ex, "message", None) or str(ex)}


@router.websocket("/ws/k8s/exec")
async def ws_k8s_exec(websocket: WebSocket):
    """交互式 Shell（Xshell 式）。

    连接参数来自 query（env/pod/container/namespace/cwd）或首条 JSON 消息；
    之后客户端发送 ``{type:'cmd', data}`` 与 ``{type:'disconnect'}``。
    服务端回 ``{type:'ready', cwd}`` / ``{type:'output', data}`` /
    ``{type:'cwd', cwd}`` / ``{type:'error', msg}``。
    """
    await websocket.accept()
    q = websocket.query_params
    env_name = q.get("env")
    pod = q.get("pod")
    container = q.get("container") or None
    namespace = q.get("namespace") or None
    cwd = q.get("cwd") or "/"

    # 若 query 未提供完整连接参数，等待首条 JSON（init）
    if not (env_name and pod):
        try:
            init = await websocket.receive_json()
        except Exception:
            await websocket.close()
            return
        if isinstance(init, dict):
            env_name = init.get("env") or env_name
            pod = init.get("pod") or pod
            container = init.get("container") or container
            namespace = init.get("namespace") or namespace
            cwd = init.get("cwd") or cwd

    if not (env_name and pod):
        await websocket.send_json({"type": "error", "msg": "缺少 env 或 pod 参数"})
        await websocket.close()
        return

    # 解析环境：复用 k8s_manager 的环境解析结果（前缀 + KUBECONFIG）
    try:
        prefix, _ = _k8s_mgr._exec_base_args(env_name, pod, container, namespace)
        kc, _ = _k8s_mgr.resolve_env_kubeconfig(env_name)
    except Exception as ex:
        await websocket.send_json(
            {"type": "error", "msg": getattr(ex, "message", None) or str(ex)})
        await websocket.close()
        return

    # 注入 kubectl 所在目录到 PATH（GUI/IDE 启动的进程 PATH 常缺 Homebrew 目录）
    sub_env = _k8s_mgr._kubectl_subprocess_env(dict(os.environ))
    if kc:
        sub_env["KUBECONFIG"] = kc

    await websocket.send_json({"type": "ready", "cwd": cwd})

    proc = None

    def _terminate():
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass

    async def _run_cmd(cmd):
        nonlocal proc, cwd
        script = _k8s_mgr._build_exec_script(cmd, cwd, track_cwd=True)
        argv = list(prefix) + ["--", "sh", "-c", script]
        # 用自动定位的 kubectl 二进制，避免进程 PATH 缺失导致 FileNotFoundError。
        # 注意：prefix 首项已是字面 "kubectl"，必须替换而非再前置，否则会变成
        # `kubectl kubectl ...` 而报 "unknown command kubectl for kubectl"。
        kubectl_bin = _k8s_mgr._resolve_kubectl_binary()
        if argv and argv[0] == "kubectl":
            argv[0] = kubectl_bin
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=sub_env,
        )
        assert proc.stdout is not None
        buf = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            buf.append(text)
            await websocket.send_json({"type": "output", "data": text})
        await proc.wait()
        merged = "".join(buf)
        new_cwd, _ = _k8s_mgr._split_pwd(merged)
        if new_cwd:
            cwd = new_cwd
            await websocket.send_json({"type": "cwd", "cwd": cwd})

    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except Exception:
                break
            if not isinstance(msg, dict):
                continue
            t = msg.get("type")
            if t == "disconnect":
                break
            if t == "cmd":
                # 先结束上一条仍在运行的命令
                if proc is not None and proc.returncode is None:
                    _terminate()
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                await _run_cmd(msg.get("data", ""))
            # 其他 type 忽略
    finally:
        _terminate()
        try:
            await websocket.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  SSE 事件流
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
#  共享全局：事件总线统一走 api.eventbus（全局唯一），任务状态见文件顶部。
# --------------------------------------------------------------------------- #
