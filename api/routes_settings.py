# -*- coding: utf-8 -*-
"""系统设置 / 接口文档（/api/docs 自省）路由。

/api/docs 动态扫描所有 /api/* 路由（含已 include 的 router），按业务域分组，
供前端「系统设置 → API 接口文档」页消费。
"""
import inspect

from fastapi.responses import JSONResponse

from fastapi import APIRouter
from api.common import app, logger, dt
from api.k8s.routes_k8s import router as k8s_router
from api.clash.routes_clash import router as clash_router
from api.routes_repos import router as repos_router
from api.routes_download import router as download_router
from api.routes_diff import router as diff_router
from api.routes_cache import router as cache_router
from api.routes_sync_history import router as sync_history_router
from api.routes_events import router as events_router
from api.cf.routes_cf import router as cf_router
from api.hcm.routes_hcm import router as hcm_router

router = APIRouter()

_MODULE_META = {
    "auth":        ("认证 / 账号", "登录态、授权检测与账号管理"),
    "repos":       ("仓库 / 分支", "仓库列表、分支、提交与文件浏览"),
    "download":    ("下载", "文件下载与断点续传"),
    "diff":        ("差异 / 合并", "目录差异扫描与文件合并"),
    "cache":       ("缓存", "缓存统计与清理"),
    "sync-history":("同步历史", "类 git log 的同步记录"),
    "cf":          ("云函数日志", "CF 账号与日志查询"),
    "k8s":         ("K8s 运维", "Pod 状态、快照与日志查询"),
    "clash":       ("Clash 分流", "分流配置检测与命令生成"),
    "hcm":         ("HCM 对象", "HCM 对象浏览与直连查询"),
    "settings":    ("系统设置", "配置读写与系统信息"),
    "misc":        ("其它", "事件流与杂项接口"),
}

_PREFIX_TO_MODULE = (
    ("/api/auth", "auth"),
    ("/api/repos", "repos"),
    ("/api/download", "download"),
    ("/api/diff", "diff"),
    ("/api/cache", "cache"),
    ("/api/sync-history", "sync-history"),
    ("/api/cf", "cf"),
    ("/api/k8s", "k8s"),
    ("/api/clash", "clash"),
    ("/api/hcm", "hcm"),
    ("/api/settings", "settings"),
)


def _classify_module(path: str) -> str:
    for prefix, mod in _PREFIX_TO_MODULE:
        if path.startswith(prefix):
            return mod
    return "misc"


def _clean_doc(doc: str) -> str:
    if not doc:
        return ""
    lines = [ln.strip() for ln in doc.strip().splitlines() if ln.strip()]
    return " ".join(lines[:3]).strip()


def _collect_docs() -> dict:
    """自省所有 /api 路由，按模块分组生成结构化接口文档。

    直接遍历各业务域 APIRouter 实例（路径信息明确），避免依赖
    app.routes 在 FastAPI 新版 include 后的内部结构差异。
    """
    routers = (
        repos_router, download_router, diff_router, cache_router,
        sync_history_router, events_router, cf_router, k8s_router,
        clash_router, hcm_router, router,
    )
    all_routes = []
    for _r in routers:
        all_routes.extend(getattr(_r, "routes", []) or [])

    seen: set[tuple[str, str]] = set()
    endpoints: dict[str, list[dict]] = {}
    for route in all_routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/"):
            continue
        if path in ("/api/docs", "/api/openapi.json"):
            continue
        methods = getattr(route, "methods", None) or set()
        methods = sorted(m for m in methods if m not in ("HEAD", "OPTIONS"))
        if not methods:
            continue
        dedup_key = (path, methods[0])
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        handler = getattr(route, "endpoint", None)
        summary = _clean_doc(getattr(handler, "__doc__", "") or "")
        params: list[str] = []
        try:
            sig = inspect.signature(handler)
            for name, p in sig.parameters.items():
                if name in ("request", "background_tasks", "websocket"):
                    continue
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                    anno = p.annotation
                    anno_name = getattr(anno, "__name__", str(anno))
                    if anno_name.startswith("list") or anno_name.startswith("List"):
                        anno_name = "array"
                    params.append(name if p.default is p.empty else f"{name}={p.default!r}")
        except (ValueError, TypeError):
            pass

        mod = _classify_module(path)
        endpoints.setdefault(mod, []).append({
            "method": methods[0],
            "methods": methods,
            "path": path,
            "summary": summary or "(无描述)",
            "params": params,
        })

    groups: list[dict] = []
    for key, (title, desc) in _MODULE_META.items():
        eps = endpoints.get(key, [])
        if not eps:
            continue
        eps.sort(key=lambda e: e["path"])
        groups.append({
            "key": key,
            "title": title,
            "description": desc,
            "count": len(eps),
            "endpoints": eps,
        })

    total = sum(g["count"] for g in groups)
    return {
        "title": "Jira Git GUI API",
        "version": "1.0",
        "base_url": "/",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "groups": groups,
    }


@router.get("/api/docs")
async def api_docs():
    """返回自省得到的接口文档（按模块分组的结构化 JSON）。"""
    return JSONResponse(_collect_docs())
