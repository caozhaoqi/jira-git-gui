# -*- coding: utf-8 -*-
"""「服务器」汇总 + Kibana 站点派生（供 K8s「系统日志汇总」tab 使用）。

背景
----
K8s 面板的「系统日志汇总」tab 需要按「服务器」选日志源：既要列出配置里的**全部服务器**
（cf_accounts / hcm_whitelist，与 `/api/hcm/envs`、HCM 对象浏览器的「服务器」下拉同源），
又要能按所选的服务器直接查它的 Kibana / ES（K8s 容器日志就采集在那里）。

因此本模块做两件事，均为纯计算、不涉及网络：
1. :func:`list_servers`  —— 汇总服务器列表（key/name/server_url/source），去重口径与
   ``api.hcm.hcm_core.hcm_envs`` 保持一致（先 hcm 代理目标，再 cf_accounts，按 url 去重）。
2. :func:`derive_kibana_url` —— 由 HCM 服务器地址派生该服的 Kibana 基址 ``<server>/kibana``。

``core/kibana/config.py`` 再据此把每台服务器映射成一个「派生站点」（name 前缀 ``srv::``），
使既有的 ``site`` 查询链路（``/api/kibana/*?site=...``）无需改动即可按服务器取日志。
"""
from __future__ import annotations

from typing import Dict, List
from urllib.parse import urlsplit, urlunsplit


def derive_kibana_url(server_url: str) -> str:
    """由 HCM 服务器地址派生 Kibana 基址。

    - 地址已带 ``/kibana`` 路径 → 原样返回（去掉末尾斜杠）；
    - 否则拼 ``/kibana``（保留 scheme/host/port，丢弃 query/fragment）。
    """
    u = (server_url or "").strip()
    if not u:
        return ""
    if u.rstrip("/").endswith("/kibana"):
        return u.rstrip("/")
    parts = urlsplit(u)
    path = parts.path.rstrip("/")
    new_path = (path + "/kibana") if path else "/kibana"
    return urlunsplit((parts.scheme, parts.netloc, new_path, "", ""))


def list_servers() -> List[Dict[str, str]]:
    """汇总配置里的服务器（与 hcm_envs 同口径去重）。

    返回 ``[{key, name, server_url, source}]``；任何加载失败都静默跳过，绝不抛错。
    注意：``server_url`` 含真实 IP / 域名，仅在本机使用，不落任何对外响应（见调用方脱敏）。
    """
    out: List[Dict[str, str]] = []
    seen: set = set()

    # 注意：url 统一去尾斜杠后再做去重 / 拼 key，避免同一服务器因
    # ``http://x`` 与 ``http://x/`` 两种写法被当成两条、且派生出同 base_url 的两个站点。

    # 1) HCM 同源代理目标（hcm_whitelist.local.json: proxy_target.base_url）
    proxy = ""
    try:
        from core.config.hcm import load_hcm_whitelist
        wh = load_hcm_whitelist() or {}
        proxy = (((wh.get("proxy_target") or {}).get("base_url")) or "").strip().rstrip("/")
    except Exception:  # noqa: BLE001
        proxy = ""
    if proxy:
        out.append({
            "key": "hcm_proxy",
            "name": "代理（同源）",
            "server_url": proxy,
            "source": "hcm_whitelist",
        })
        seen.add(proxy)

    # 2) cf_accounts 里的服务器
    accounts = []
    try:
        from core.config.cf import load_cf_accounts
        accounts = load_cf_accounts() or []
    except Exception:  # noqa: BLE001
        accounts = []
    for acc in accounts:
        url = (acc.get("server_url") or "").strip().rstrip("/")
        if not url or url in seen:
            continue
        out.append({
            "key": f"cf:{url}",
            "name": acc.get("name", url),
            "server_url": url,
            "source": "cf_accounts",
        })
        seen.add(url)

    return out
