# -*- coding: utf-8 -*-
"""HCM 对象浏览器 —— 路由层（薄 HTTP 适配）。"""
from fastapi import Request
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel

from fastapi import APIRouter
from api.common import app, _PROJECT_ROOT
from api.hcm.hcm_core import hcm_envs, hcm_proxy, hcm_direct, hcm_decrypt_param, hcm_save_data, hcm_temp_download

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
