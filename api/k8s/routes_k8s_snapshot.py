# -*- coding: utf-8 -*-
"""K8s 快照 / 报告 / 日志（含流式跟随）路由。

拆分自 ``api/routes_k8s.py``，业务子域：Pod 状态快照、任务取消、报告下载、
单/多 Pod 日志读取与 ``kubectl logs -f`` 流式跟随。共享状态见 ``routes_k8s_state``。
"""
import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from core import k8s_manager as _k8s_mgr
from core.k8s import run_kubectl as _k8s_run_kubectl
from core.k8s import (
    run_snapshot as _k8s_run_snapshot,
    fetch_logs as _k8s_fetch_logs,
    stream_kubectl as _k8s_stream_kubectl,
)
from api.schemas import K8sSnapshotReq
from api.k8s.routes_k8s_state import state, normalize_time_arg
from api.eventbus import broadcast

logger = logging.getLogger("api.routes_k8s_snapshot")
router = APIRouter()


@router.post("/api/k8s/snapshot")
async def api_k8s_snapshot(req: K8sSnapshotReq):
    """触发一次 K8s Pod 状态 / 日志快照（后台线程执行，SSE 推送进度）。"""
    if state.running:
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
        state.running = True
        state.cancel.clear()
        try:
            # 记录本次快照的 kubeconfig / namespace，供日志查看接口实时回退使用
            state.snap_meta["kubeconfig"] = opts.get("kubeconfig")
            state.snap_meta["namespace"] = opts.get("namespace")
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
                should_cancel=state.cancel.is_set,
            )
            state.out_dir["dir"] = result["out_dir"]
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
            state.running = False
            broadcast("k8s_finished", {"running": False})

    threading.Thread(target=_do, name="k8s-snapshot", daemon=True).start()
    return {"ok": True, "msg": "快照任务已启动"}


@router.post("/api/k8s/cancel")
async def api_k8s_cancel():
    """取消正在进行的快照任务。"""
    state.cancel.set()
    return {"ok": True, "msg": "已发送取消信号"}


@router.get("/api/k8s/report")
async def api_k8s_report(download: bool = False):
    """打开 / 下载最近一次生成的 report.html。"""
    d = state.out_dir["dir"]
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
    name: str = "",
    env: str = "",
    container: str = "",
    tail: int = 200,
    previous: bool = False,
    timestamps: bool = True,
    since: str = "",
    until: str = "",
    label: str = "",
):
    """读取某个 Pod 的日志（供主面板与独立日志查看页共用）。

    - 优先读取快照落盘文件（按 container 名匹配 ``{name}__{container}.log``）；
    - 落盘无则实时 ``kubectl logs`` 向集群拉取，避免直接 404。
    - ``env`` 优先用于解析 kubeconfig / 命名空间，回退到快照上下文。
    - ``since`` / ``until`` 透传给 ``kubectl logs`` 做时间范围筛选；
    - ``label`` 提供时转为「按 label 聚合多 Pod 日志」模式（微服务排障）。
    """
    # 容错归一化 since/until：非法值忽略该筛选并告警，避免 kubectl 崩溃式 404
    since, _sw = normalize_time_arg("since", since, False)
    until, _uw = normalize_time_arg("until", until, True)
    for _w in (_sw, _uw):
        if _w:
            logger.warning(f"[K8s] {_w}")

    def _aggregate_pod_log(pod):
        """汇总单个 Pod（含多容器）日志，块头为 ===== pod: <name> =====。"""
        base = ["logs", pod] + ns_args + ["--tail", str(tail)] \
            + (["--previous"] if previous else []) \
            + (["--timestamps"] if timestamps else []) \
            + (["--since", str(since)] if since else []) \
            + (["--until", str(until)] if until else [])
        out, rc, err = _k8s_run_kubectl(base, kc, timeout=30)
        if rc == 0 and out.strip():
            return "===== pod: %s =====\n%s" % (pod, out)
        if "container name must be specified" in err or "a container name must be specified" in err:
            po, prc, perr = _k8s_run_kubectl(["get", "pod", pod, "-o", "json"] + ns_args, kc, timeout=30)
            containers = []
            if prc == 0:
                try:
                    obj = json.loads(po)
                    containers = [c.get("name") for c in obj.get("spec", {}).get("containers", [])]
                except Exception:
                    containers = []
            parts = []
            for c in containers:
                log = _k8s_fetch_logs(c, c, kc, ns, tail, previous, timeout=30,
                                      timestamps=timestamps, since=since, until=until)
                parts.append("===== container: %s =====\n%s" % (c, log))
            if parts:
                return "===== pod: %s =====\n%s" % (pod, "\n\n".join(parts))
        return "===== pod: %s =====\n# 获取失败: %s" % (pod, err.strip()[:300])

    d = state.out_dir["dir"]
    # 1) 快照落盘文件优先（仅单 Pod 模式；label 聚合 / 时间戳 / 时间范围均为实时场景，跳过）
    if d and name and not label and not timestamps and not since and not until:
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
        kc = state.snap_meta.get("kubeconfig")
        ns = ns or state.snap_meta.get("namespace")
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

    # 2.5) 按 label 聚合多 Pod 日志（微服务排障刚需）
    if label:
        po, prc, perr = _k8s_run_kubectl(
            ["get", "pods", "-l", label, "-o", "json"] + ns_args, kc, timeout=30
        )
        if prc != 0:
            raise HTTPException(status_code=502, detail=f"按 label 列举 Pod 失败：{perr.strip()[:300]}")
        try:
            objs = json.loads(po)
            pod_names = [it["metadata"]["name"] for it in objs.get("items", [])]
        except Exception as ex:
            raise HTTPException(status_code=502, detail=f"解析 Pod 列表失败：{ex}")
        if not pod_names:
            return PlainTextResponse("# 未找到匹配 label=%s 的 Pod" % label)
        blocks = [_aggregate_pod_log(pn) for pn in pod_names]
        return PlainTextResponse("\n\n".join(blocks))

    # 3) 指定容器 → 直接实时抓取（单容器场景）
    if container:
        log = _k8s_fetch_logs(name, container, kc, ns, tail, previous, timeout=30,
                              timestamps=timestamps, since=since, until=until)
        return PlainTextResponse(log)

    # 4) 未指定容器：先试单容器，再试多容器
    out, rc, err = _k8s_run_kubectl(
        ["logs", name] + ns_args + ["--tail", str(tail)]
        + (["--previous"] if previous else [])
        + (["--timestamps"] if timestamps else [])
        + (["--since", str(since)] if since else [])
        + (["--until", str(until)] if until else []),
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
            log = _k8s_fetch_logs(name, c, kc, ns, tail, previous, timeout=30,
                                  timestamps=timestamps, since=since, until=until)
            parts.append("===== container: %s =====\n%s" % (c, log))
        if parts:
            return PlainTextResponse("\n\n".join(parts))

    raise HTTPException(
        status_code=404,
        detail=f"实时获取 Pod {name} 日志失败：{err.strip()[:300]}",
    )


@router.get("/api/k8s/pod-containers")
async def api_k8s_pod_containers(name: str = "", env: str = ""):
    """列出指定 Pod 内的容器名（供日志 / 文件操作前选择容器）。

    重构前随 ``api/routes_k8s.py`` 提供，分目录拆分时该端点被遗漏（历史日志中曾稳定返回
    200）。此处补回以保证「前端调用路径 ↔ 后端路由」契约不变。响应结构对齐前端：
    ``{"ok": true, "containers": [...], "namespace": "..."}``。
    """
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name 参数（Pod 名称）。")
    # env 优先解析 kubeconfig / 命名空间，回退快照上下文
    kc, ns = (None, None)
    if env:
        try:
            kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
        except Exception as ex:
            return {"ok": False, "error": getattr(ex, "message", None) or str(ex),
                    "containers": []}
    if not kc:
        kc = state.snap_meta.get("kubeconfig")
        ns = ns or state.snap_meta.get("namespace")
    if not kc:
        return {"ok": False,
                "error": "尚未连接集群（请先在当前环境运行一次快照或指定 env）。",
                "containers": []}
    ns_args = ["-n", ns] if ns else []
    out, rc, err = _k8s_run_kubectl(
        ["get", "pod", name, "-o", "json"] + ns_args, kc, timeout=30
    )
    if rc != 0:
        return {"ok": False, "error": f"获取 Pod 失败：{err.strip()[:300]}",
                "containers": []}
    try:
        obj = json.loads(out)
        containers = [c.get("name") for c in obj.get("spec", {}).get("containers", [])]
    except Exception as ex:
        return {"ok": False, "error": f"解析 Pod JSON 失败：{ex}", "containers": []}
    return {"ok": True, "containers": containers, "namespace": ns or ""}


@router.get("/api/k8s/log/stream")
async def api_k8s_log_stream(
    request: Request,
    name: str = "",
    env: str = "",
    container: str = "",
    tail: int = 200,
    previous: bool = False,
    timestamps: bool = True,
    since: str = "",
    until: str = "",
    namespace: str = "",
):
    """流式跟随 Pod 日志（``kubectl logs -f``）。

    - 仅支持**单 Pod**（label 聚合为批量一次性拉取，不适用流式）；
    - 客户端断开连接时立即 kill 子进程，避免 kubectl 泄漏；
    - 返回 ``text/plain`` 分块流，前端用 fetch + ReadableStream 逐块追加。
    """
    # 容错归一化 since/until：非法值忽略该筛选并告警，避免 kubectl 崩溃式失败
    since, _sw = normalize_time_arg("since", since, False)
    until, _uw = normalize_time_arg("until", until, True)
    for _w in (_sw, _uw):
        if _w:
            logger.warning(f"[K8s] {_w}")

    if not name:
        raise HTTPException(status_code=400, detail="流式跟随需要指定 name（单 Pod）。")
    # 解析 kubeconfig / 命名空间（与 /api/k8s/log 一致）
    kc, ns = (None, None)
    if env:
        kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if not kc:
        kc = state.snap_meta.get("kubeconfig")
        ns = ns or state.snap_meta.get("namespace")
    if not kc:
        raise HTTPException(
            status_code=404,
            detail="未连接集群（请先在当前环境运行一次快照或指定环境）。",
        )
    try:
        tail = max(1, min(int(tail), 5000))
    except Exception:
        tail = 200
    ns_args = ["-n", ns] if ns else []

    args = ["logs", name] + ns_args + ["--tail", str(tail)] \
        + (["--previous"] if previous else []) \
        + (["--timestamps"] if timestamps else []) \
        + (["--since", str(since)] if since else []) \
        + (["--until", str(until)] if until else []) \
        + ["-f"]
    if container:
        args += ["--container", container]
    else:
        # -f 必须明确容器；未指定时拉全部容器（kubectl 要求 --all-containers）
        args += ["--all-containers"]

    proc = await _k8s_stream_kubectl(args, kc)

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")
