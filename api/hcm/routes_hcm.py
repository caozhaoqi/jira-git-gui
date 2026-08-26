# -*- coding: utf-8 -*-
"""HCM 对象浏览器 —— 路由层（薄 HTTP 适配）。"""
from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel

from fastapi import APIRouter
from api.common import app
from api.hcm.hcm_core import hcm_envs, hcm_proxy, hcm_direct

router = APIRouter()


class HcmDirectReq(BaseModel):
    """直连请求：由后端直接用与前端一致的加解密直连 HCM 网关并解密响应。"""
    api_name: str = "hcm.paas.object.list"
    params: dict = {}
    token: str = ""
    model: str = ""


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
