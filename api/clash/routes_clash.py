# -*- coding: utf-8 -*-
"""Clash 分流配置助手路由聚合入口。

原先 977 行的单体路由文件已按业务子域拆分为：
- ``clash_base``：公共头部（常量 / 日志 / 底层工具函数 / Pydantic 模型）
- ``routes_clash_probe``：只读探测（接口 / 路由状态 / 连通性 / 代理端口）
- ``routes_clash_rules``：规则生成 / 一键应用 / 撤销 / 服务顺序修复
- ``routes_clash_config``：诊断 / 配置路径探测 / 默认值 / 批量写入

本模块只负责把这些子路由合并到统一 ``router`` 上，供 ``server.py`` 通过
``from api.clash.routes_clash import router`` 引用——**对外路由路径与挂载点保持不变**。
"""
from fastapi import APIRouter

from api.clash.clash_base import _CLASH_DEFAULTS_FILE, _resolve_clash_defaults_source
from api.clash.routes_clash_probe import router as _probe_router
from api.clash.routes_clash_rules import router as _rules_router
from api.clash.routes_clash_config import router as _config_router


def _load_clash_defaults(project_root=None):
    # 向后兼容：test_clash_defaults.py 通过 mock.patch 打桩 ``rc._CLASH_DEFAULTS_FILE``；
    # 直接委托 common 的实现（已对齐 _env_search_roots + 示例回退 + 告警）。
    res, _, _ = _resolve_clash_defaults_source(_CLASH_DEFAULTS_FILE, project_root)
    return res


router = APIRouter()
router.include_router(_probe_router)
router.include_router(_rules_router)
router.include_router(_config_router)
