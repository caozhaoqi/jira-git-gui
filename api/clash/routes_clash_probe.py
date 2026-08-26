# -*- coding: utf-8 -*-
"""Clash 分流助手 —— 只读探测路由（网络接口 / 路由状态 / 连通性 / 代理端口）。

拆分自 ``api/routes_clash.py``，业务子域：系统网络接口检测、默认路由与 host 路由快照、
单 IP 连通性检查、Clash 代理端口监听探测。底层工具函数见 ``clash_base``。
"""
import logging
import platform
import re
from typing import Any, Dict, List

from fastapi import APIRouter

from api.clash.clash_base import (
    _log, _parse_hardware_ports, _ifconfig, _classify, _list_services,
    _default_gateway, _default_gateway_ip, _host_routes, _route_get, _curl_probe,
)

logger = logging.getLogger("api.routes_clash_probe")
router = APIRouter()


@router.get("/api/clash/interfaces")
async def clash_interfaces():
    """检测网络接口：Wi-Fi / Ethernet / 其他，附 IP、状态、默认网关、服务顺序。"""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "仅支持 macOS（当前系统：" + platform.system() + "）", "interfaces": []}
    ports = _parse_hardware_ports()
    result: List[Dict[str, Any]] = []
    seen: set = set()
    for p in ports:
        dev = p.get("device", "")
        if not dev or dev in seen or re.fullmatch(r"(utun|awdl|llw|anpi|ap|gif|stf|p2p|tap|tun|lo)\d*", dev):
            continue
        seen.add(dev)
        info = _ifconfig(dev)
        info["port"] = p.get("port", "")
        info["kind"] = _classify(info["port"])
        result.append(info)
    result.sort(key=lambda x: (x["status"] != "active", x["kind"] != "lan", x["device"]))
    services = _list_services()
    default_iface = _default_gateway()
    _log.info("接口检测: %s | 默认网关: %s",
              ", ".join(f"{x['device']}({x['kind']}:{x['ip'] or '-'}:{x['status']})" for x in result),
              default_iface or "-")
    return {
        "ok": True,
        "interfaces": result,
        "default_gateway": default_iface,
        "default_gateway_ip": _default_gateway_ip(),
        "services": services,
    }


@router.get("/api/clash/route-status")
async def clash_route_status():
    """默认路由 + host 路由 + 服务顺序快照（诊断「默认路由被局域网接口抢占」）。"""
    default_iface = _default_gateway()
    services = _list_services()
    host_routes = _host_routes()
    _log.info("路由状态: 默认接口=%s(%s) host路由=%d 条, 服务顺序=%s",
              default_iface or "-", _default_gateway_ip() or "-",
              len(host_routes), " > ".join(f"{s['name']}[{s['device']}]" for s in services))
    return {
        "ok": True,
        "default_iface": default_iface,
        "default_gateway_ip": _default_gateway_ip(),
        "services": services,
        "host_routes": host_routes,
    }


@router.get("/api/clash/check")
async def clash_check(ip: str):
    """检查单个 IP：当前路由接口 + HTTP 连通性。"""
    ip = ip.strip()
    if not re.fullmatch(r"[\w.:\-]+", ip):
        return {"ok": False, "error": "IP 格式不合法", "ip": ip}
    r = _route_get(ip)
    c = _curl_probe(ip)
    _log.info("连通性检查 %s: 路由接口=%s HTTP=%s", ip, r.get("route_iface") or "-", c.get("http_code"))
    return {"ok": True, "ip": ip, **r, **c}


@router.get("/api/clash/proxy-status")
async def clash_proxy_status():
    """探测常见 Clash 代理端口是否在监听（7890 / 7891 / 7897）。"""
    import socket

    out: List[Dict[str, Any]] = []
    for port in (7890, 7891, 7897):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                out.append({"port": port, "open": True})
        except OSError:
            out.append({"port": port, "open": False})
    return {"ok": True, "ports": out}
