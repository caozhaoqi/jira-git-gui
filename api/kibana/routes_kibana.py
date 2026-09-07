# -*- coding: utf-8 -*-
"""Kibana 路由聚合入口（对齐 ``api/k8s/routes_k8s.py`` 的拆法）。

- ``routes_kibana_sites``：站点管理 + 连通性测试
- ``routes_kibana_meta`` ：字段枚举 + Pod 概览（带 TTL 缓存）
- ``routes_kibana_logs`` ：日志查询 / 上下文 / 直方图 / 导出

对外只暴露本模块的 ``router``，由 ``api/server.py`` include。
"""
from fastapi import APIRouter

from api.kibana.routes_kibana_sites import router as _sites_router
from api.kibana.routes_kibana_meta import router as _meta_router
from api.kibana.routes_kibana_logs import router as _logs_router

router = APIRouter()
router.include_router(_sites_router)
router.include_router(_meta_router)
router.include_router(_logs_router)
