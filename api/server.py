# -*- coding: utf-8 -*-
"""Jira Git GUI —— API 聚合入口。

本文件只负责：
  - 全局异常处理
  - 挂载静态前端（/web）
  - include 各业务域路由模块（api/routes_*.py）
  - 启动入口 main()

所有路由实现已下沉到对应业务模块，保持 /api/* 路径与行为完全不变。
"""
import sys
import asyncio

from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.common import app, logger, broadcast, _PROJECT_ROOT
from api.cf.cf_core import cf_autologin_all

# --------------------------------------------------------------------------- #
#  全局异常处理：任何未捕获的 500 都把完整 traceback 写入日志，并向前端
#  返回结构化 detail（含异常类型与消息），避免前端只看到「Internal Server Error」。
# --------------------------------------------------------------------------- #
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    import traceback as _tb
    tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("未捕获异常: %s %s\n%s", request.method, request.url.path, tb_text)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "traceback": tb_text[-2000:]},
    )


# --------------------------------------------------------------------------- #
#  启动：首次启动后台遍历 cf_accounts 自动登录获取 token（尽力执行，不阻塞）
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def _startup_cf_autologin():
    try:
        asyncio.create_task(cf_autologin_all())
    except Exception as e:
        logger.warning(f"[CF] 启动自动登录任务创建失败: {e}")


# --------------------------------------------------------------------------- #
#  静态前端（优先 frontend/web-react/dist，回退 web/）
#  React 版产物（vite build --base /web/）输出到 frontend/web-react/dist，
#  与原生 web/ 一样经 app.mount("/web", ...) 提供，路径语义完全一致。
# --------------------------------------------------------------------------- #
WEB_DIR = _PROJECT_ROOT / "frontend" / "web-react" / "dist"
if not WEB_DIR.exists():
    WEB_DIR = _PROJECT_ROOT / "web"
if WEB_DIR.exists():
    class _NoCacheStaticFiles(StaticFiles):
        """禁用浏览器/中间缓存的静态文件提供器，避免前端改完还加载旧文件。"""
        def file_response(self, *a, **kw):
            resp = super().file_response(*a, **kw)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
    app.mount("/web", _NoCacheStaticFiles(directory=str(WEB_DIR), html=True), name="web")


@app.get("/")
async def index():
    """默认返回 Web 前端首页。"""
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        resp = FileResponse(str(index_path))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    return JSONResponse({"msg": "Web frontend not found. API is running at /api/"})


# --------------------------------------------------------------------------- #
#  业务域路由模块（必须在 main() 之前 include）
# --------------------------------------------------------------------------- #
from api.routes_repos import router as repos_router            # noqa: E402
from api.routes_download import router as download_router      # noqa: E402
from api.routes_diff import router as diff_router              # noqa: E402
from api.routes_cache import router as cache_router            # noqa: E402
from api.routes_sync_history import router as sync_history_router  # noqa: E402
from api.routes_events import router as events_router          # noqa: E402
from api.cf.routes_cf import router as cf_router                  # noqa: E402
from api.hcm.routes_hcm import router as hcm_router                # noqa: E402
from api.routes_settings import router as settings_router      # noqa: E402
from api.k8s.routes_k8s import router as k8s_router                # noqa: E402
from api.clash.routes_clash import router as clash_router            # noqa: E402
from api.routes_services_config import router as services_config_router  # noqa: E402

# 按业务域分组挂载（顺序无关，仅便于阅读）：
#   仓库/下载/差异/缓存/同步/事件 → CF/HCM 平台 → 设置(汇总) → K8s/Clash 聚合域
for _r in (
    # 仓库 / 下载 / 差异 / 缓存 / 同步历史 / 事件
    repos_router, download_router, diff_router,
    cache_router, sync_history_router, events_router,
    # CF / HCM 平台
    cf_router, hcm_router,
    # 设置（聚合汇总各域 router）
    settings_router,
    # 聚合域（自身再 include 子模块）：K8s / Clash
    k8s_router, clash_router,
    # 服务配置管理（云函数 / HCM 账号与代理配置）
    services_config_router,
):
    app.include_router(_r)


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Jira Git GUI API Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("API Server 启动")
    logger.info("Python  : %s", sys.version.replace("\n", " "))
    logger.info("监听    : http://%s:%d", args.host, args.port)
    logger.info("=" * 60)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
