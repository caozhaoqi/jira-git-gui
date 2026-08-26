# -*- coding: utf-8 -*-
"""Clash 分流助手 —— 配置与诊断路由。

拆分自 ``api/routes_clash.py``，业务子域：全面诊断（代理 / 路由 / 端口 / 连通性 / 配置解析）、
探测本机 Clash 配置文件路径、读取默认值配置、批量把 DIRECT 规则写入所有配置文件。
底层工具函数见 ``clash_base``。
"""
import logging
import re

from fastapi import APIRouter

from api.clash.clash_base import (
    _log, _run, _default_gateway, _list_services, _classify, _route_get,
    _probe_config_paths, _load_clash_defaults, _patch_clash_rules, _CLASH_DEFAULTS_FILE,
    _resolve_clash_defaults_source,
    DiagnoseReq, PatchAllReq,
)

logger = logging.getLogger("api.routes_clash_config")
router = APIRouter()


@router.post("/api/clash/diagnose")
async def clash_diagnose(req: DiagnoseReq):
    """全面诊断：系统代理 / 默认路由 / Clash 端口 / 外网直连+代理 / 各 IP 直连+代理 / 配置解析。"""
    items = []

    def add(key, label, ok, detail) -> None:
        items.append({"key": key, "label": label, "ok": ok, "detail": detail})

    # 1. 系统代理
    proxy_out = _run(["scutil", "--proxy"], timeout=3)
    http_on = bool(re.search(r"HTTPEnable\s*:\s*1", proxy_out))
    m_port = re.search(r"HTTPPort\s*:\s*(\d+)", proxy_out)
    http_port = m_port.group(1) if m_port else "7890"
    add("proxy", "系统代理 (HTTP)", http_on, f"已启用 → 127.0.0.1:{http_port}" if http_on else "未启用")

    # 2. 默认路由
    gw = _default_gateway()
    add("gw", "默认路由接口", bool(gw), gw or "未找到默认路由")

    # 2.5 服务顺序与默认路由抢占检测
    try:
        services = _list_services()
        wifi_rank = next((s["rank"] for s in services if "wi-fi" in s["name"].lower()), None)
        lan_before = [s for s in services if _classify(s["name"]) == "lan" and not s["disabled"] and (wifi_rank is None or s["rank"] < wifi_rank)]
        order_desc = " > ".join(f"{s['name']}{'*' if s['disabled'] else ''}" for s in services[:5])
        if lan_before:
            add("svc_order", "服务顺序 (Wi-Fi 优先)", False,
                f"有线网卡排位在 Wi-Fi 前，插线会抢默认路由！当前顺序: {order_desc}")
        elif gw and any(_classify(s["name"]) == "lan" for s in services if s["device"] == gw):
            add("svc_order", "服务顺序 (Wi-Fi 优先)", False,
                f"默认路由已被有线网卡 {gw} 抢占（服务顺序 Wi-Fi 靠后）")
        else:
            add("svc_order", "服务顺序 (Wi-Fi 优先)", True,
                f"Wi-Fi 优先或未插有线，当前顺序: {order_desc}")
    except Exception as e:  # noqa: BLE001
        add("svc_order", "服务顺序 (Wi-Fi 优先)", None, f"检测失败: {e}")

    # 3. Clash 端口
    import socket
    clash_open = False
    for port in (7890, 7891, 7897):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                clash_open = True
                break
        except OSError:
            pass
    add("clash", "Clash 代理端口", clash_open, "7890/7891/7897 有监听" if clash_open else "Clash 未运行或端口未开")

    # 4. 外网直连
    b = _run(["curl", "-s", "-o", "/dev/null", "-m", "4", "-w", "%{http_code}", "http://www.baidu.com/"], timeout=7)
    bc = b.strip().splitlines()[-1] if b.strip() else "000"
    add("wan_direct", "外网直连 (baidu)", bc not in ("000", ""), f"HTTP {bc}" + ("（直连正常）" if bc == "200" else ""))

    # 5. 外网经 Clash 代理
    g = _run(["curl", "-s", "-o", "/dev/null", "-m", "8", "-x", "http://127.0.0.1:7890", "-w", "%{http_code}", "https://www.google.com/"], timeout=11)
    gc = g.strip().splitlines()[-1] if g.strip() else "000"
    add("wan_proxy", "外网经 Clash (google)", gc not in ("000", ""), f"HTTP {gc}" + ("（代理节点可用）" if gc in ("200", "301", "302") else ""))

    # 6. 各 IP：路由接口 + 直连 + 经代理
    for ip in req.ips:
        host = ip.split(":")[0]
        if not re.fullmatch(r"[\d.]+", host):
            continue
        r = _route_get(host)
        iface = r.get("route_iface", "") or "无"
        d = _run(["curl", "-s", "-o", "/dev/null", "-m", "4", "-w", "%{http_code}", f"http://{ip}/"], timeout=7)
        dc = d.strip().splitlines()[-1] if d.strip() else "000"
        p = _run(["curl", "-s", "-o", "/dev/null", "-m", "4", "-x", "http://127.0.0.1:7890", "-w", "%{http_code}", f"http://{ip}/"], timeout=7)
        pc = p.strip().splitlines()[-1] if p.strip() else "000"
        ok = dc not in ("000", "") or pc not in ("000", "")
        add(f"ip_{host}", f"IP {host}", ok,
            f"路由接口: {iface} | 直连: HTTP {dc} | 经Clash: HTTP {pc}")

    # 7. Clash 配置解析
    import os
    path = req.clash_config_path.strip()
    if path and os.path.isfile(path):
        try:
            import yaml as _yaml

            with open(path, "r", encoding="utf-8") as f:
                _yaml.safe_load(f)
            add("cfg", "Clash 配置解析", True, f"{path} 解析正常")
        except Exception as e:  # noqa: BLE001
            add("cfg", "Clash 配置解析", False, f"{path} 解析失败: {e}")
    else:
        add("cfg", "Clash 配置解析", None, "未提供配置文件路径")

    _log.info("诊断完成: %s", " | ".join(f"{i['label']}={i['ok']}" for i in items))
    return {"ok": True, "items": items}


@router.get("/api/clash/config-path")
async def clash_config_path():
    """探测本机常见 Clash 配置文件路径（供前端一键填充）。"""
    paths = _probe_config_paths()
    current = ""
    try:
        out = _run(["defaults", "read", "com.west2online.ClashX", "selectConfigName"], timeout=3)
        current = out.strip()
    except Exception:  # noqa: BLE001
        current = ""
    if current and not current.endswith(".yaml"):
        current = current + ".yaml"
    _log.info("配置探测: %d 个候选, ClashX 当前选中=%s", len(paths), current or "(未知)")
    return {"ok": True, "paths": paths, "current_config": current}


@router.get("/api/clash/defaults")
async def clash_defaults():
    """返回默认 IP / 默认局域网接口；source 标识来源：local / example / missing / corrupt。"""
    d, src_path, src = _resolve_clash_defaults_source(_CLASH_DEFAULTS_FILE)
    _log.info(
        "默认值读取: %d 个 IP, lan_device=%s, 来源=%s, 路径=%s",
        len(d["default_ips"]),
        d["lan_device"] or "-",
        src,
        str(src_path),
    )
    return {
        "ok": True,
        "default_ips": d["default_ips"],
        "lan_device": d["lan_device"],
        "source": src,
    }


@router.post("/api/clash/patch-all")
async def clash_patch_all(req: PatchAllReq):
    """把 DIRECT 规则写入 ~/.config/clash 下【所有】yaml（含 ClashX 当前加载的）。"""
    import glob
    import os

    ips = [x.strip() for x in req.ips if x.strip()]
    if not ips:
        return {"ok": False, "error": "请先添加至少一个 IP"}
    clash_dir = os.path.join(os.path.expanduser("~"), ".config", "clash")
    files = sorted(
        glob.glob(os.path.join(clash_dir, "*.yaml"))
        + glob.glob(os.path.join(clash_dir, "*.yml"))
    )
    if not files:
        return {"ok": False, "error": f"未找到 {clash_dir} 下的配置文件"}
    results = []
    for p in files:
        r = _patch_clash_rules(p, ips)
        r["path"] = p
        results.append(r)
    updated = sum(1 for r in results if r.get("updated"))
    skipped = sum(1 for r in results if r.get("skipped"))
    failed = [r["path"] for r in results if r.get("error")]
    _log.info("批量写入配置: 共 %d 个, 更新 %d, 已存在 %d, 失败 %s", len(results), updated, skipped, failed or "无")
    return {
        "ok": True,
        "total": len(results),
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
        "hint": "规则已写入全部配置文件。请在 ClashX 菜单栏图标 → 配置 → 重新选择当前配置（或切换走再切回）使其重载生效。",
    }
