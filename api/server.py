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
import re
import asyncio

from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from api.common import app, logger, broadcast, capture_loop, _PROJECT_ROOT
from api.cf.cf_core import cf_autologin_all
from core.errors import UserError

# --------------------------------------------------------------------------- #
#  全局异常处理：任何未捕获的 500 都把完整 traceback 写入日志，并向前端
#  返回结构化 detail（含异常类型与消息），避免前端只看到「Internal Server Error」。
# --------------------------------------------------------------------------- #
# 响应压缩：650KB 的 JS 压缩后仅 195KB（实测）。GZipMiddleware 的默认排除列表
# 已包含 text/event-stream，因此不会破坏 /api/events 的实时推送，也不会重复压缩
# 图片/字体等已压缩资源。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    import traceback as _tb
    tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("未捕获异常: %s %s\n%s", request.method, request.url.path, tb_text)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "traceback": tb_text[-2000:]},
    )


@app.exception_handler(UserError)
async def _user_error_handler(request, exc):
    """用户可预期错误（缺配置 / 输入不合法 / 会话过期等）→ 400，而非 500。

    覆盖 core/k8s 等通过 UserError 表达「用户操作层面问题」的全部接口
    （env 未配置、kubectl 不可用、资源不存在等），避免这些情况被全局
    Exception 兜底成 500。消息直接透传给前端展示。
    """
    logger.warning("用户可预期错误: %s %s -> %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
#  启动：首次启动后台遍历 cf_accounts 自动登录获取 token（尽力执行，不阻塞）
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def _startup_cf_autologin():
    # 事件总线必须捕获主事件循环，否则 broadcast() 在工作线程里只能走
    # q.put_nowait 分支（不会唤醒 await q.get() 的消费者），导致 scan_progress /
    # merge_progress 等 SSE 事件要等到 15s 心跳才被推送给前端 —— 进度条表现为
    # 「卡住→突跳→消失」。在启动钩子里拿到运行中的 loop 即可让 call_soon_threadsafe
    # 立即唤醒消费者，进度条实时更新。
    try:
        capture_loop()
    except Exception as e:
        logger.warning(f"[SSE] 事件总线主循环捕获失败: {e}")
    try:
        asyncio.create_task(cf_autologin_all())
    except Exception as e:
        logger.warning(f"[CF] 启动自动登录任务创建失败: {e}")
    try:
        asyncio.create_task(_cache_evict_loop())
    except Exception as e:
        logger.warning(f"[cache] 缓存清理定时任务创建失败: {e}")


# --------------------------------------------------------------------------- #
#  后台缓存清理：evict_expired() 已写好但此前从未被调度，导致 cache/ 目录
#  TTL 过期却从不被再次访问的文件无限堆积（实测 7130 文件 / 49M 且持续增长）。
#  启动时先扫一次，之后每 10 分钟扫一次，回收过期条目与残留 .tmp。
# --------------------------------------------------------------------------- #
async def _cache_evict_loop():
    from core.cache import evict_expired
    try:
        n = evict_expired()
        if n:
            logger.info(f"[cache] 启动清理回收 {n} 条过期缓存")
    except Exception as e:
        logger.warning(f"[cache] 启动清理失败: {e}")
    while True:
        await asyncio.sleep(600)
        try:
            evict_expired()
        except Exception as e:
            logger.warning(f"[cache] 周期清理失败: {e}")


# --------------------------------------------------------------------------- #
#  静态前端（优先 frontend/web-react/dist，回退 web/）
#  React 版产物（vite build --base /web/）输出到 frontend/web-react/dist，
#  与原生 web/ 一样经 app.mount("/web", ...) 提供，路径语义完全一致。
# --------------------------------------------------------------------------- #
WEB_DIR = _PROJECT_ROOT / "frontend" / "web-react" / "dist"
if not WEB_DIR.exists():
    WEB_DIR = _PROJECT_ROOT / "web"
if WEB_DIR.exists():
    # Vite 产物带内容哈希（assets/index-Ab12Cd34.js），内容变则文件名变，
    # 因此可以安全地长缓存；而未带哈希的文件（index.html 等）必须禁缓存，
    # 否则「前端改完还加载旧文件」。此前一刀切 no-store，导致每次刷新都要
    # 重新下载 650KB JS + 157KB CSS。
    _HASHED_ASSET_RE = re.compile(
        r"[.-][A-Za-z0-9_-]{8,}\.(?:js|mjs|css|woff2?|ttf|otf|png|jpe?g|gif|svg|webp|ico|map)$"
    )

    class _CachingStaticFiles(StaticFiles):
        """带内容哈希的资源长缓存（immutable）；其余保持 no-store。"""

        def file_response(self, *a, **kw):
            resp = super().file_response(*a, **kw)
            path = str(kw.get("path") or (a[0] if a else ""))
            if _HASHED_ASSET_RE.search(path):
                # 一年 + immutable：浏览器不会在有效期内发条件请求
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                # 注意：starlette 的 MutableHeaders 没有 .pop()，用 del 并容错
                for h in ("Pragma", "Expires"):
                    try:
                        del resp.headers[h]
                    except KeyError:
                        pass
            else:
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
            return resp

    app.mount("/web", _CachingStaticFiles(directory=str(WEB_DIR), html=True), name="web")


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
from api.cfdebug.routes_cfdebug import router as cfdebug_router    # noqa: E402
from api.hcm.routes_hcm import router as hcm_router                # noqa: E402
from api.routes_settings import router as settings_router      # noqa: E402
from api.k8s.routes_k8s import router as k8s_router                # noqa: E402
from api.kibana.routes_kibana import router as kibana_router          # noqa: E402
from api.clash.routes_clash import router as clash_router            # noqa: E402
from api.routes_services_config import router as services_config_router  # noqa: E402
from api.routes_jira_issue import router as jira_issue_router            # noqa: E402
from api.routes_unified_diagnose import router as unified_diagnose_router  # noqa: E402

# 按业务域分组挂载（顺序无关，仅便于阅读）：
#   仓库/下载/差异/缓存/同步/事件 → CF/HCM 平台 → 设置(汇总) → K8s/Clash 聚合域
for _r in (
    # 仓库 / 下载 / 差异 / 缓存 / 同步历史 / 事件
    repos_router, download_router, diff_router,
    cache_router, sync_history_router, events_router,
    # CF / HCM 平台
    cf_router, cfdebug_router, hcm_router,
    # 设置（聚合汇总各域 router）
    settings_router,
    # 聚合域（自身再 include 子模块）：K8s / Kibana / Clash
    k8s_router, kibana_router, clash_router,
    # 服务配置管理（云函数 / HCM 账号与代理配置）
    services_config_router,
    # Jira 建单（供 HCM 云函数错误定位面板把结论转成 issue）
    jira_issue_router,
    # 统一诊断（CF + K8s 联合诊断）
    unified_diagnose_router,
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
