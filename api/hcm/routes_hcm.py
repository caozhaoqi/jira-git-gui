# -*- coding: utf-8 -*-
"""HCM 对象浏览器 —— 路由层（薄 HTTP 适配）。"""
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel

from fastapi import APIRouter
from api.common import app, logger, _PROJECT_ROOT
from api.hcm.hcm_core import hcm_envs, hcm_proxy, hcm_direct, hcm_decrypt_param, hcm_save_data, hcm_temp_download
from api.hcm.ws_bridge import ws_call, ws_proxy

router = APIRouter()


class HcmDirectReq(BaseModel):
    """直连请求：由后端直接用与前端一致的加解密直连 HCM 网关并解密响应。"""
    api_name: str = "hcm.paas.object.list"
    params: dict = {}
    token: str = ""
    model: str = ""
    target: str = ""  # 可选：覆盖 proxy_target，指向其它可达的 HCM 网关
    sql_debug: bool = False
    profile_debug: bool = False


@router.get("/api/hcm/envs")
async def api_hcm_envs():
    """返回可选服务器环境列表，供前端 HCM 对象浏览器「选择服务器」。"""
    return await hcm_envs()


@router.api_route(
    "/hcm-api/{api_name:path}",
    methods=["GET", "POST", "OPTIONS"],
)
async def api_hcm_proxy(api_name: str, request: Request) -> Response:
    """同源代理：转发到 HCM OpenAPI 网关。"""
    return await hcm_proxy(api_name, request)


@router.post("/api/hcm/direct")
async def api_hcm_direct(req: HcmDirectReq) -> dict:
    """「能直连就走直连」：由后端直接 POST HCM 网关并解密响应。"""
    return await hcm_direct(req)


class HcmDecryptReq(BaseModel):
    """解密抓包参数请求（网关 /api/api_decrypt_param，需 onlyoneowner 等管理角色）。

    hcm_param 为抓包得到的密文；hcm_transfer_strategy / s3h 为抓包同款传输策略与
    签名（s3h 留空时后端自动计算）。后端会把整个信封再加密一层明文直发——该
    handler 自身解两层，不能走 /api/hcm/direct 的信封通道。
    """
    hcm_param: str = ""
    hcm_transfer_strategy: str = ""
    s3h: str = ""
    token: str = ""
    target: str = ""


@router.post("/api/hcm/decrypt")
async def api_hcm_decrypt(req: HcmDecryptReq) -> dict:
    """解密抓包参数：hcm_param → 明文 JSON（供 HCM 运维面板「参数解密」页）。"""
    return await hcm_decrypt_param(req)


class HcmSaveDataReq(BaseModel):
    """保存 HCM 对象数据 JSON 到本地文件。"""
    model: str = ""
    content: str = ""


@router.post("/api/hcm/data/save")
async def api_hcm_data_save(req: HcmSaveDataReq) -> dict:
    """将 HCM 对象数据 JSON 写入本地文件，返回绝对路径（前端复制该路径到剪贴板）。"""
    return hcm_save_data(req)


class HcmTempDownloadReq(BaseModel):
    """下载 HCM 网关临时文件（接口日志 / SQL Profile）。

    网关的 /document/temp/download 需要 cookie:token 登录态且在另一个 origin，
    浏览器直接跳转会 404 / 「请登录后操作」，故由后端带 token 代取后回传字节流。
    """
    index: str = ""
    name: str = ""
    token: str = ""
    target: str = ""


@router.post("/api/hcm/temp-download")
async def api_hcm_temp_download(req: HcmTempDownloadReq) -> Response:
    """代理下载 HCM 网关临时文件（日志 / profile），前端拿到 Blob 后自行落盘。"""
    return await hcm_temp_download(req)


class HcmWsCallReq(BaseModel):
    """WS 调试调用（对齐平台调试页「WS」按钮的一次性语义）。

    与 /api/hcm/direct 的差异：走网关 ``/ws/<api>`` 通道，参数为**明文业务
    参数**（无加密信封），回包为 ``{status, data}``；仅声明 notify 的
    handler 支持（长任务类接口）。
    """
    api_name: str = ""
    params: dict = {}
    token: str = ""
    target: str = ""
    # 长任务（导出 / 批量审批）可能数分钟：默认 300s，前端可按需调大
    recv_timeout: float = 300.0


@router.post("/api/hcm/ws-call")
async def api_hcm_ws_call(req: HcmWsCallReq) -> dict:
    """一次性 WS 调试调用：连接网关 WS → 发参数 → 收包至终态 → 关闭。"""
    if not req.api_name.strip():
        return {"ok": False, "error": "缺少 api_name"}
    try:
        r = await ws_call(
            req.target, req.api_name.strip(), req.params, req.token,
            recv_timeout=req.recv_timeout,
        )
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # 连接层失败 → 502，透传类型便于定位
        logger.warning("[HCM-WS] ws-call 失败 %s: %s", req.api_name, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return r


@router.websocket("/api/hcm/ws")
async def api_hcm_ws(ws: WebSocket) -> None:
    """流式 WS 调试代理：本地浏览器 ↔ 网关 ``/ws/<api>`` 双向桥接。

    查询参数：``api``（接口名）、``target``（可选，覆盖网关）、``token``
    （可选，缺省用预设）。连接后首帧发业务参数（明文 JSON），之后网关的
    notify 进度 / success / error 终态实时转发；任一侧断开即整体结束。
    """
    await ws.accept()
    api_name = (ws.query_params.get("api") or "").strip()
    if not api_name:
        await ws.close(code=4400, reason="missing api")
        return
    try:
        await ws_proxy(ws, ws.query_params.get("target") or "",
                       api_name, ws.query_params.get("token") or "")
    except WebSocketDisconnect:
        pass


# # --------------------------------------------------------------------------- #
# #  HCM 元数据浏览器页面（独立于 React 构建产物，经 FastAPI 路由提供，避免被
# #  /web 的 frontend/web-react/dist 覆盖）。前端通过 /api/hcm/direct 拉取元数据。
# # --------------------------------------------------------------------------- #
# @router.get("/hcm-meta")
# async def hcm_meta_page():
#     """返回 HCM 元数据浏览器页面。"""
#     page = _PROJECT_ROOT / "web" / "hcm-meta.html"
#     if not page.exists():
#         from fastapi.responses import JSONResponse
#         return JSONResponse({"ok": False, "msg": "hcm-meta.html not found"}, status_code=404)
#     return FileResponse(str(page), media_type="text/html", headers={"Cache-Control": "no-store"})
