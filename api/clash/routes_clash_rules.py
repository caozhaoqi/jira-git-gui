# -*- coding: utf-8 -*-
"""Clash 分流助手 —— 规则生成 / 一键应用 / 撤销 / 服务顺序修复路由。

拆分自 ``api/routes_clash.py``，业务子域：生成 Clash rules 片段与 macOS route 命令、
osascript 提权批量加/删路由 + 写入 Clash rules、修复「USB 网卡抢占默认路由」导致的外网中断。
底层工具函数见 ``clash_base``。
"""
import logging
import os
import re
import shlex

from fastapi import APIRouter

from api.clash.clash_base import (
    _log, _log_route_table, _lan_gateway, _route_cmd_for, DEFAULT_PRIVATE_CIDRS,
    _privileged_shell, _ifconfig, _DEV_RE, _IP_HOST_RE,
    _default_gateway, _default_gateway_ip, _route_get, _patch_clash_rules, _unpatch_clash_rules,
    _list_services, GenReq, ApplyReq, RevertReq, FixRouteReq,
)

logger = logging.getLogger("api.routes_clash_rules")
router = APIRouter()


def _apply_routes_privileged(ips, device) -> dict:
    """批量 route：先删后加（幂等），逐条验证。"""
    entries = [(ip, ip.split(":")[0]) for ip in ips]
    gw = _lan_gateway(device)
    script_lines = ["echo '== clash-ui route apply =='", f"echo 'lan gateway: {gw or '(无)'}'"]
    for ip, host in entries:
        script_lines.append(f'echo "=== {shlex.quote(ip)} ==="')
        script_lines.append(
            f"route -n delete -host {host} -interface {device} 2>/dev/null || true; "
            + (f"route -n delete -host {host} {gw} 2>/dev/null || true; " if gw else "")
            + f"{_route_cmd_for(host, device)} >/dev/null 2>&1 || true"
        )
    r = _privileged_shell("\n".join(script_lines))

    results = []
    for ip, host in entries:
        g = _route_get(host)
        iface = g.get("route_iface", "")
        results.append(
            {
                "ip": ip,
                "ok": iface == device,
                "route_iface": iface or "（无独立路由，仍走默认）",
                "route": _route_cmd_for(host, device),
                "gateway": gw,
            }
        )
    return {
        "results": results,
        "authorized": r.returncode == 0,
        "output": ((r.stdout or "") + (r.stderr or ""))[-2000:],
    }


@router.post("/api/clash/generate")
async def clash_generate(req: GenReq):
    """生成 Clash rules 片段 + macOS route 命令。"""
    ips = [x.strip() for x in req.ips if x.strip().split(":")[0].strip()]
    if not ips:
        return {"ok": False, "error": "请至少填写一个 IP"}

    lines = [
        "# ===== Clash 分流规则（插入到 rules: 列表【顶部】）=====",
        f"# 目标：指定内网 IP 直连局域网；其他流量走 {req.wan_device or '默认接口（WiFi）'} 进 Clash。",
    ]
    for ip in ips:
        host = ip.split(":")[0]
        lines.append(f"- IP-CIDR,{host}/32,DIRECT")
    lines.append("")
    lines.append("# ---- 常见私网段直连（按需保留）----")
    for cidr, label in DEFAULT_PRIVATE_CIDRS:
        lines.append(f"- IP-CIDR,{cidr},DIRECT,no-resolve  # {label}")
    clash_rules = "\n".join(lines)

    add_cmds: list = []
    del_cmds: list = []
    if req.lan_device:
        gw = _lan_gateway(req.lan_device)
        add_cmds.append(f"# 指定 IP 走局域网接口 {req.lan_device}（有线直连，经网关 {gw or '(无, 用 -interface)'}）")
        add_cmds.append(f"# 若 Clash 开启了增强模式/TUN，先临时关闭或按需调整；执行需 sudo。")
        for ip in ips:
            host = ip.split(":")[0]
            add_cmds.append("sudo " + _route_cmd_for(host, req.lan_device))
        del_cmds.append(f"# 撤销（恢复默认路由）：")
        for ip in ips:
            host = ip.split(":")[0]
            del_cmds.append(f"sudo route -n delete -host {host} -interface {req.lan_device} 2>/dev/null || true")
            if gw:
                del_cmds.append(f"sudo route -n delete -host {host} {gw} 2>/dev/null || true")
    else:
        add_cmds.append("# 未选择局域网接口，仅生成 Clash 规则。")

    _log.info("生成配置: ips=%s lan=%s wan=%s", ips, req.lan_device or "-", req.wan_device or "-")
    return {
        "ok": True,
        "ips": ips,
        "clash_rules": clash_rules,
        "route_add": "\n".join(add_cmds),
        "route_del": "\n".join(del_cmds),
        "hint": "路由命令需要 sudo 权限，请在终端执行；Clash 规则请粘贴到配置的 rules: 顶部。",
    }


@router.post("/api/clash/apply")
async def clash_apply(req: ApplyReq):
    """一键应用：osascript 提权批量加路由 + 写入 Clash rules。"""
    ips = [x.strip() for x in req.ips if x.strip()]
    if not ips:
        return {"ok": False, "error": "请先添加至少一个 IP"}
    device = req.lan_device.strip()
    if not req.only_clash:
        if not _DEV_RE.fullmatch(device):
            return {"ok": False, "error": "局域网接口名不合法（如 en13）"}
        iface_info = _ifconfig(device)
        if not iface_info.get("ip"):
            return {"ok": False, "error": f"接口 {device} 没有 IP（可能未插网线/未激活），请选择有 IP 的局域网接口"}
    for ip in ips:
        host = ip.split(":")[0]
        if not _IP_HOST_RE.fullmatch(host):
            return {"ok": False, "error": f"IP 格式不合法：{ip}"}

    _log.info("一键应用开始: ips=%s lan_device=%s clash_config=%s",
              ips, device or "-", req.clash_config_path.strip() or "(未填)")
    _log_route_table("应用前")

    route_result: dict = {"applied": False}
    clash_result: dict = {"updated": False}

    if not req.only_clash:
        route_result = _apply_routes_privileged(ips, device)
        route_result["applied"] = any(r["ok"] for r in route_result.get("results", []))
        _log.info("路由应用结果: %s", route_result.get("results"))
        _log_route_table("应用后")

    path = req.clash_config_path.strip()
    if path:
        if not os.path.isfile(path):
            _log.error("Clash 配置文件不存在: %s", path)
            return {"ok": False, "error": f"Clash 配置文件不存在：{path}", "route": route_result}
        clash_result = _patch_clash_rules(path, ips)

    default_now = _default_gateway()
    hijack_warn = ""
    if not req.only_clash and default_now and default_now == device:
        hijack_warn = (
            f"⚠️ 检测到默认路由现在指向局域网接口 {device}（服务顺序里它优先于 Wi-Fi），"
            f"外网（微信图片等）会走不通！请点「一键修复（Wi-Fi 优先）」，"
            f"或到 系统设置→网络→服务顺序 把 Wi-Fi 移到最前。"
        )
        _log.warning("默认路由被局域网接口抢占: %s", default_now)

    _log.info("一键应用完成: route=%s clash=%s%s", route_result.get("applied"), clash_result,
              " | " + hijack_warn if hijack_warn else "")
    reload_tip = ""
    if path:
        reload_tip = (
            "Clash 规则已写入配置文件，但 ClashX 需要重载才生效："
            "打开 ClashX 菜单 → 「配置」重新选择该文件（或右键菜单栏图标 → 重新加载）。"
            "重载前经 Clash 访问内网 IP 仍会 502，直连正常。"
        )
    return {
        "ok": True,
        "route": route_result,
        "clash": clash_result,
        "warning": hijack_warn,
        "hint": ("路由已生效（直连可用「检查连通性」复核）。" + reload_tip),
    }


@router.post("/api/clash/revert")
async def clash_revert(req: RevertReq):
    """一键撤销：删除自动添加的路由 + 移除 Clash 配置中的自动规则。"""
    route_result: dict = {"applied": False}
    clash_result: dict = {"updated": False}

    _log.info("一键撤销开始: ips=%s lan_device=%s clash_config=%s",
              req.ips, req.lan_device.strip() or "-", req.clash_config_path.strip() or "(未填)")
    _log_route_table("撤销前")

    device = req.lan_device.strip()
    if device and _DEV_RE.fullmatch(device) and req.ips:
        ips = [x.strip() for x in req.ips if x.strip()]
        gw = _lan_gateway(device)
        script_lines = ["echo '== clash-ui route revert =='"]
        for ip in ips:
            host = ip.split(":")[0]
            script_lines.append(
                f"route -n delete -host {host} -interface {device} 2>/dev/null || true"
            )
            if gw:
                script_lines.append(
                    f"route -n delete -host {host} {gw} 2>/dev/null || true"
                )
        r = _privileged_shell("\n".join(script_lines))
        results = []
        for ip in ips:
            host = ip.split(":")[0]
            g = _route_get(host)
            iface = g.get("route_iface", "")
            results.append(
                {
                    "ip": ip,
                    "ok": iface != device,
                    "route_iface": iface or "（已无独立路由）",
                }
            )
        route_result = {
            "applied": r.returncode == 0,
            "results": results,
            "output": ((r.stdout or "") + (r.stderr or ""))[-1000:],
        }
        _log.info("路由撤销结果: %s", results)
        _log_route_table("撤销后")

    path = req.clash_config_path.strip()
    if path and os.path.isfile(path):
        clash_result = _unpatch_clash_rules(path)
        _log.info("Clash 配置撤销: %s → %s", path, clash_result)

    _log.info("一键撤销完成: route=%s clash=%s", route_result.get("applied"), clash_result)
    return {
        "ok": True,
        "route": route_result,
        "clash": clash_result,
        "hint": "路由已删除；Clash 配置已还原（若存在自动规则）。",
    }


@router.post("/api/clash/fix-service-order")
async def clash_fix_service_order(req: FixRouteReq):
    """把外网接口（Wi-Fi）提到服务顺序第一位，并立即把默认路由切回外网接口。"""
    services = _list_services()
    if not services:
        return {"ok": False, "error": "未读取到网络服务列表"}

    wan_service = req.wan_service.strip()
    if not wan_service:
        for sv in services:
            if req.wan_device and sv["device"] == req.wan_device:
                wan_service = sv["name"]
                break
        if not wan_service:
            for sv in services:
                if "wi-fi" in sv["name"].lower() and not sv["disabled"]:
                    wan_service = sv["name"]
                    break
    if not wan_service:
        return {"ok": False, "error": "未找到外网服务（Wi-Fi），请手动填写"}

    ordered = [wan_service] + [s["name"] for s in services if s["name"] != wan_service]
    _log.info("调整服务顺序: %s", " > ".join(ordered))

    script = " ".join(shlex.quote(a) for a in ["networksetup", "-ordernetworkservices", *ordered])
    r = _privileged_shell(script)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    order_ok = r.returncode == 0 and "error" not in out.lower()

    route_msg = "未调整默认路由"
    gw = _default_gateway_ip()
    cur_iface = _default_gateway()
    if req.wan_device and cur_iface and cur_iface != req.wan_device and gw:
        _log.info("恢复默认路由: 当前=%s(%s) → %s(%s)", cur_iface, gw, req.wan_device, gw)
        r2 = _privileged_shell(
            f"route -n delete default -ifscope {cur_iface} 2>/dev/null; "
            f"route -n change default {gw} 2>/dev/null; "
            f"route -n add default {gw} 2>/dev/null; "
            f"route -n get default"
        )
        out2 = ((r2.stdout or "") + (r2.stderr or "")).strip()
        m = re.search(r"interface:\s*(\S+)", out2)
        route_msg = f"默认路由现指向 {m.group(1) if m else '?'}"
    _log_route_table("服务顺序调整后")
    return {
        "ok": order_ok,
        "wan_service": wan_service,
        "new_order": ordered,
        "output": out or "(无输出)",
        "route": route_msg,
        "hint": "已把外网接口提到服务顺序第一。若仍异常，请重新插拔网线或重启 Wi-Fi。",
    }
