# -*- coding: utf-8 -*-
"""K8s 路由聚合入口。

原先 1059 行的单体路由文件已按业务子域拆分为：
- ``routes_k8s_snapshot``：快照 / 报告 / 日志（含流式跟随）
- ``routes_k8s_env``：环境管理 + Pod / YAML / 网络探测
- ``routes_k8s_observe``：events / describe / top
- ``routes_k8s_exec``：命令执行 + 交互式 WebSocket 终端
- ``routes_k8s_files``：容器内文件操作

本模块只负责把这些子路由合并到统一 ``router`` 上，供 ``server.py`` /
``routes_settings.py`` 通过 ``from api.k8s.routes_k8s import router`` 引用——**对外路由路径
与挂载点保持不变**。
"""
from fastapi import APIRouter

from api.k8s.routes_k8s_snapshot import router as _snapshot_router
from api.k8s.routes_k8s_env import router as _env_router
from api.k8s.routes_k8s_observe import router as _observe_router
from api.k8s.routes_k8s_exec import router as _exec_router
from api.k8s.routes_k8s_files import router as _files_router
# 向后兼容：部分测试直接引用原单体模块顶层的纯函数/常量
from api.k8s.routes_k8s_observe import _k8s_normalize_time_arg

router = APIRouter()
router.include_router(_snapshot_router)
router.include_router(_env_router)
router.include_router(_observe_router)
router.include_router(_exec_router)
router.include_router(_files_router)
