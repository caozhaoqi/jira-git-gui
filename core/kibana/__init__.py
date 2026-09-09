# -*- coding: utf-8 -*-
"""Kibana 日志子模块。

对标 ``core/k8s`` 的分层：

- ``config``  ：多站点连接配置（地址 / 账号 / 索引模式 / 时间字段）
- ``client``  ：Kibana Console Proxy 客户端（唯一可打 ES 的通道）
- ``queries`` ：ES DSL 构造与响应解析（纯函数，可脱离 FastAPI 单测）

为什么走 Console Proxy 而不是直连 ES：
    目标环境 ES 本体在集群内网（10.233.66.39:9200），本机不可达；
    ``/elasticsearch/`` 看着像反代，实际返回的是 HCM 首页 HTML（nginx SPA
    fallback）。实测唯一可用通道是 Kibana 7.17 的
    ``POST /kibana/api/console/proxy?path=<es 路径>&method=<GET|POST>``，
    配 ``kbn-xsrf: true`` + Basic Auth。
"""
from .config import (  # noqa: F401
    KIBANA_SITES_FILE,
    load_sites,
    save_sites,
    list_sites,
    get_site,
    add_or_update_site,
    set_current_site,
    delete_site,
    sites_source,
    clear_sites_cache,
)
from .client import (  # noqa: F401
    KibanaError,
    KibanaClient,
    get_client,
)
