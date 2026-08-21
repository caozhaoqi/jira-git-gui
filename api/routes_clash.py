# -*- coding: utf-8 -*-
"""Clash 分流配置助手路由（macOS 网络接口检测 / 路由查询 / 配置生成 / 诊断）。

设计原则：
- 检测：networksetup / ifconfig / route / scutil（只读）
- 应用：osascript 提权执行 route；直接改写 Clash 配置 rules（先备份 + YAML 校验回滚）
- 日志：所有操作（检测/检查/应用/撤销/生成/诊断）记录到 logs/clash_ui.log
"""
import glob
import logging
import os
import platform
import re
import shlex
import socket
import subprocess
import tempfile
import time as _time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

DEFAULT_PRIVATE_CIDRS = [
    ("10.0.0.0/8", "A 类私网"),
    ("172.16.0.0/12", "B 类私网"),
    ("192.168.0.0/16", "C 类私网"),
]

# --------------------------------------------------------------------------- #
#  操作日志（logs/clash_ui.log）
# --------------------------------------------------------------------------- #
_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "clash_ui.log"
_log = logging.getLogger("clash_ui")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _h = logging.FileHandler(str(_LOG_PATH), encoding="utf-8")
        _h.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [clash-ui] %(message)s")
        )
        _log.addHandler(_h)
    except OSError as e:  # noqa: BLE001
        _log.addHandler(logging.NullHandler())
        print(f"[clash-ui] 日志初始化失败: {e}", flush=True)


def _log_route_table(tag: str) -> None:
    """记录当前路由表快照（默认 + 所有 host 路由）。"""
    out = _run(["netstat", "-rn"], timeout=5)
    interesting = []
    for line in out.splitlines():
        if re.search(r"^(default|73\.|83\.|10\.|172\.|192\.168)", line.strip()):
            interesting.append(line.strip())
    _log.info("路由表快照[%s]:\n%s", tag, "\n".join(interesting) if interesting else "(空)")


# --------------------------------------------------------------------------- #
#  系统检测（只读）
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], timeout: float = 5.0) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"<error: {e}>"


def _parse_hardware_ports() -> List[Dict[str, str]]:
    """解析 `networksetup -listallhardwareports` → [{port, device, mac}]。"""
    out = _run(["networksetup", "-listallhardwareports"], timeout=5)
    ports: List[Dict[str, str]] = []
    cur: Dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("Hardware Port:"):
            if cur:
                ports.append(cur)
            cur = {"port": line.split(":", 1)[1].strip()}
        elif line.startswith("Device:"):
            cur["device"] = line.split(":", 1)[1].strip()
        elif line.startswith("Ethernet Address:"):
            cur["mac"] = line.split(":", 1)[1].strip()
    if cur:
        ports.append(cur)
    return ports


def _ifconfig(dev: str) -> Dict[str, Any]:
    out = _run(["ifconfig", dev], timeout=3)
    info: Dict[str, Any] = {"device": dev, "ip": "", "status": "inactive", "flags": ""}
    m = re.search(r"inet ([\d.]+) ", out)
    if m:
        info["ip"] = m.group(1)
    m2 = re.search(r"flags=(\d+)<([^>]+)>", out)
    if m2:
        info["flags"] = m2.group(2)
        info["status"] = "active" if "UP" in m2.group(2) else "inactive"
    return info


def _default_gateway() -> str:
    """默认网关接口（用于提示「其他流量走 WiFi」时应该选谁）。"""
    out = _run(["route", "-n", "get", "default"], timeout=3)
    m = re.search(r"interface:\s*(\S+)", out)
    return m.group(1) if m else ""


def _default_gateway_ip() -> str:
    """默认网关 IP（route -n get default 的 gateway 字段）。"""
    out = _run(["route", "-n", "get", "default"], timeout=3)
    m = re.search(r"gateway:\s*(\S+)", out)
    return m.group(1) if m else ""


def _list_services() -> List[Dict[str, Any]]:
    """解析 networksetup 服务列表与顺序。

    返回 [{name, device, disabled, rank}]，rank 越小越优先（0 = 第一）。
    注意：星号 (*) 在 -listallnetworkservices 里表示服务被禁用；
    -listnetworkserviceorder 里的 (*) 同样表示禁用（服务可能仍占排位）。
    """
    order_out = _run(["networksetup", "-listnetworkserviceorder"], timeout=5)
    services: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in order_out.splitlines():
        m_rank = re.match(r"^\((\*|\d+)\)\s+(.+)$", line.strip())
        if m_rank:
            if cur:
                services.append(cur)
            rank = 0 if m_rank.group(1) == "*" else int(m_rank.group(1))
            cur = {"name": m_rank.group(2).strip(), "rank": rank, "device": "", "disabled": m_rank.group(1) == "*"}
        elif "Device:" in line and cur:
            cur["device"] = line.split("Device:", 1)[1].split()[0].rstrip(")") if line.split("Device:", 1)[1].split() else ""
    if cur:
        services.append(cur)
    # 用 listallnetworkservices 修正 disabled 标记（* 前缀）
    all_out = _run(["networksetup", "-listallnetworkservices"], timeout=5)
    disabled_names = set()
    for line in all_out.splitlines():
        s = line.strip()
        if s.startswith("*"):
            disabled_names.add(s[1:].strip())
    for sv in services:
        if sv["name"] in disabled_names:
            sv["disabled"] = True
    return services


def _host_routes() -> List[Dict[str, str]]:
    """列出当前所有 IPv4 host 路由（netstat -rn 的 /32 或 H 标志）。"""
    out = _run(["netstat", "-rn"], timeout=5)
    routes = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        dst, gw, flags, netif = parts[0], parts[1], parts[2], parts[3]
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+(/\d+)?", dst) and "H" in flags and netif != "lo0":
            routes.append({"host": dst.rstrip("/32"), "gateway": gw, "flags": flags, "iface": netif})
    return routes


def _lan_gateway(device: str) -> str:
    """局域网接口的网关 IP。

    从 netstat -rn 找 flags 含 G 且 netif == device 的 IPv4 路由网关。
    例：en13 插 USB 网线后 default → 10.6.6.254（网关）。
    """
    out = _run(["netstat", "-rn"], timeout=5)
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) >= 4
            and parts[3] == device
            and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parts[1])
            and "G" in parts[2]
        ):
            return parts[1]
    return ""


def _route_cmd_for(host: str, device: str) -> str:
    """构造 host 路由命令。

    关键：目标 IP 若不在接口子网内，-interface 会创建 L2 直达路由（ARP 失败 → 502/000），
    必须经网关（route add -host <ip> <gateway>）才能由路由器转发。
    网关不存在（接口无路由）时才退回 -interface。
    """
    gw = _lan_gateway(device)
    if gw:
        return f"route -n add -host {host} {gw}"
    return f"route -n add -host {host} -interface {device}"


def _classify(port: str) -> str:
    p = port.lower()
    if "wi-fi" in p or "airport" in p or "wlan" in p:
        return "wifi"
    if "ethernet" in p or "usb" in p or "lan" in p:
        return "lan"
    return "other"


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


def _route_get(ip: str) -> Dict[str, Any]:
    """查询某 IP 当前走哪个接口（route -n get）。"""
    out = _run(["route", "-n", "get", ip], timeout=3)
    m = re.search(r"interface:\s*(\S+)", out)
    return {"route_iface": m.group(1) if m else "", "raw": out.strip()[:300]}


def _curl_probe(ip: str) -> Dict[str, Any]:
    """连通性探测：curl -m 3 返回 HTTP 状态（ip 可能带端口，如 1.2.3.4:8080）。"""
    host = ip.split(":")[0]
    url = f"http://{ip}/"
    out = _run(["curl", "-s", "-o", "/dev/null", "-m", "3", "-w", "%{http_code}", url], timeout=6)
    code = out.strip().splitlines()[-1] if out.strip() else ""
    # curl 连接失败返回 000；仅 2xx/3xx/4xx/5xx 数字码才算“可达”
    return {"host": host, "http_code": code, "reachable": code.isdigit() and code != "000"}


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


class GenReq(BaseModel):
    ips: List[str] = []
    lan_device: str = ""   # 指定 IP 应走的局域网接口（en0/en1…）
    wan_device: str = ""   # 其他流量接口（WiFi），仅用于注释提示


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

    add_cmds: List[str] = []
    del_cmds: List[str] = []
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


@router.get("/api/clash/proxy-status")
async def clash_proxy_status():
    """探测常见 Clash 代理端口是否在监听（7890 / 7891 / 7897）。"""
    out: List[Dict[str, Any]] = []
    for port in (7890, 7891, 7897):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                out.append({"port": port, "open": True})
        except OSError:
            out.append({"port": port, "open": False})
    return {"ok": True, "ports": out}


# --------------------------------------------------------------------------- #
#  一键应用 / 撤销（osascript 提权执行 route；直接改写 Clash 配置 rules）
# --------------------------------------------------------------------------- #
_IP_HOST_RE = re.compile(r"^[\d.]+$")
_DEV_RE = re.compile(r"^[A-Za-z0-9]+$")
CLASH_UI_MARK = "# clash-ui-auto"


def _privileged_shell(script: str, timeout: int = 180):
    """通过 osascript 以管理员权限执行 shell 脚本（macOS 弹系统密码框，一次授权）。

    返回 CompletedProcess；用户取消 / 非 GUI 会话时会失败并带 stderr。
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", prefix="clash-ui-", delete=False
    ) as f:
        f.write("#!/bin/bash\nset +e\n" + script + "\n")
        tmp = f.name
    os.chmod(tmp, 0o700)
    try:
        osa = f'do shell script "bash {tmp}" with administrator privileges'
        return subprocess.run(
            ["osascript", "-e", osa], capture_output=True, text=True, timeout=timeout
        )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _apply_routes_privileged(ips: List[str], device: str) -> Dict[str, Any]:
    """批量 route：先删后加（幂等），逐条验证。

    注意：
    1. macOS 的 route add 失败时（如接口无 IP）可能仍返回退出码 0，不能信退出码
       —— 执行后逐个用 route -n get 验证目标 IP 是否真的指向目标接口。
    2. 目标 IP 不在接口子网时必须经网关（route add -host <ip> <gw>），
       -interface 会建 L2 直达路由导致 ARP 失败（502/000）。
    """
    entries = [(ip, ip.split(":")[0]) for ip in ips]
    gw = _lan_gateway(device)
    script_lines = ["echo '== clash-ui route apply =='", f"echo 'lan gateway: {gw or '(无)'}'"]
    for ip, host in entries:
        script_lines.append(f'echo "=== {shlex.quote(ip)} ==="')
        # 删除旧路由（兼容 -interface 与 经网关 两种形式）
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


def _patch_clash_rules(path: str, ips: List[str]) -> Dict[str, Any]:
    """向 Clash YAML 的 rules: 段顶部插入 IP-CIDR DIRECT 规则（先备份、去重）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return {"updated": False, "error": f"读取配置文件失败：{e}"}

    lines = text.splitlines()
    insert_idx = None
    indent = "  "
    for i, line in enumerate(lines):
        m = re.match(r"^[ \t]*rules\s*:\s*(#.*)?$", line)
        if m:
            insert_idx = i + 1
            for j in range(i + 1, min(i + 6, len(lines))):
                m2 = re.match(r"^([ \t]*)- ", lines[j])
                if m2:
                    indent = m2.group(1)
                    break
            break
    if insert_idx is None:
        return {"updated": False, "error": "未找到 rules: 段，请确认是 Clash 的 YAML 配置"}

    new_rules = [
        f"{indent}- IP-CIDR,{ip.split(':')[0]}/32,DIRECT  {CLASH_UI_MARK}"
        for ip in ips
    ]
    existing = "\n".join(lines)
    to_add = [r for r in new_rules if f"IP-CIDR,{r.split('IP-CIDR,')[1].split('/')[0]}/32" not in existing]
    if not to_add:
        return {"updated": False, "skipped": "规则已存在，无需重复添加", "backup": ""}

    backup = f"{path}.clash-ui.bak-{_time.strftime('%Y%m%d%H%M%S')}"
    try:
        with open(backup, "w", encoding="utf-8") as f:
            f.write(text)
        lines[insert_idx:insert_idx] = to_add
        new_text = "\n".join(lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError as e:
        return {"updated": False, "error": f"写入配置文件失败：{e}"}

    # 写入后立即做 YAML 校验；解析失败则回滚备份，绝不留下坏配置（否则 Clash 重载会失败）
    try:
        import yaml as _yaml

        _yaml.safe_load(new_text)
    except Exception as e:  # noqa: BLE001
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        _log.error("Clash 配置写入后 YAML 校验失败，已回滚: %s (path=%s)", e, path)
        return {"updated": False, "error": f"写入后 YAML 校验失败，已自动回滚：{e}"}

    _log.info("Clash 配置已更新: %s (+%d 条规则, 备份=%s)", path, len(to_add), backup)
    return {"updated": True, "backup": backup, "added": len(to_add), "rules": to_add}


def _unpatch_clash_rules(path: str) -> Dict[str, Any]:
    """移除带 clash-ui-auto 标记的规则行。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"updated": False, "error": f"读取配置文件失败：{e}"}
    keep = [ln for ln in lines if CLASH_UI_MARK not in ln]
    removed = len(lines) - len(keep)
    if removed == 0:
        return {"updated": False, "skipped": "配置中没有可移除的自动规则", "removed": 0}
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(keep))
    except OSError as e:
        return {"updated": False, "error": f"写入配置文件失败：{e}"}
    return {"updated": True, "removed": removed}


class ApplyReq(BaseModel):
    ips: List[str] = []
    lan_device: str = ""
    clash_config_path: str = ""
    only_clash: bool = False  # True = 只改 Clash 配置，不动路由


@router.post("/api/clash/apply")
async def clash_apply(req: ApplyReq):
    """一键应用：osascript 提权批量加路由 + 写入 Clash rules。

    说明：route 修改需要管理员权限，会弹出 macOS 系统授权框（输入密码一次）。
    """
    ips = [x.strip() for x in req.ips if x.strip()]
    if not ips:
        return {"ok": False, "error": "请先添加至少一个 IP"}
    device = req.lan_device.strip()
    if not req.only_clash:
        if not _DEV_RE.fullmatch(device):
            return {"ok": False, "error": "局域网接口名不合法（如 en13）"}
        # 关键：局域网接口必须有 IP，否则 host 路由无效（route add 会 Network is unreachable）
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

    route_result: Dict[str, Any] = {"applied": False}
    clash_result: Dict[str, Any] = {"updated": False}

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

    # 应用后检查：默认路由是否被局域网接口抢占（插网线常见症状 → 外网断）
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


class RevertReq(BaseModel):
    ips: List[str] = []
    lan_device: str = ""
    clash_config_path: str = ""


@router.post("/api/clash/revert")
async def clash_revert(req: RevertReq):
    """一键撤销：删除自动添加的路由 + 移除 Clash 配置中的自动规则。"""
    route_result: Dict[str, Any] = {"applied": False}
    clash_result: Dict[str, Any] = {"updated": False}

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
            # 已不在路由表（route get 失败 → iface 空）或已不指向目标接口 = 撤销成功
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


class DiagnoseReq(BaseModel):
    ips: List[str] = []
    clash_config_path: str = ""


@router.post("/api/clash/diagnose")
async def clash_diagnose(req: DiagnoseReq):
    """全面诊断：系统代理 / 默认路由 / Clash 端口 / 外网直连+代理 / 各 IP 直连+代理 / 配置解析。

    用于定位「启用后内外网全断」类问题 —— 逐条给出 ✓/✗ 与耗时。
    """
    items: List[Dict[str, Any]] = []

    def add(key: str, label: str, ok: bool, detail: str) -> None:
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

    # 2.5 服务顺序与默认路由抢占检测（USB 网卡排位靠前 → 插线即抢默认 → 外网断）
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


class FixRouteReq(BaseModel):
    wan_device: str = ""          # 外网接口（如 en0），其服务排第一
    wan_service: str = ""         # 外网服务名（如 "Wi-Fi"）；不填则按 wan_device 自动匹配


@router.post("/api/clash/fix-service-order")
async def clash_fix_service_order(req: FixRouteReq):
    """把外网接口（Wi-Fi）提到服务顺序第一位，并立即把默认路由切回外网接口。

    解决：USB 有线网卡排位靠前导致插线后默认路由被抢占 → 外网（微信图片等）全断。
    需要管理员权限（osascript 授权框）。
    """
    services = _list_services()
    if not services:
        return {"ok": False, "error": "未读取到网络服务列表"}

    # 确定 wan 服务名
    wan_service = req.wan_service.strip()
    if not wan_service:
        for sv in services:
            if req.wan_device and sv["device"] == req.wan_device:
                wan_service = sv["name"]
                break
        if not wan_service:
            # 自动选 Wi-Fi 服务（含 "Wi-Fi" 且非 disabled）
            for sv in services:
                if "wi-fi" in sv["name"].lower() and not sv["disabled"]:
                    wan_service = sv["name"]
                    break
    if not wan_service:
        return {"ok": False, "error": "未找到外网服务（Wi-Fi），请手动填写"}

    # 构造新顺序：wan 第一，其余保持原顺序（去重）
    ordered = [wan_service] + [s["name"] for s in services if s["name"] != wan_service]
    cmd_args = ["networksetup", "-ordernetworkservices", *ordered]
    _log.info("调整服务顺序: %s", " > ".join(ordered))

    script = " ".join(shlex.quote(a) for a in ["networksetup", "-ordernetworkservices", *ordered])
    r = _privileged_shell(script)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    order_ok = r.returncode == 0 and "error" not in out.lower()

    # 立即把默认 IPv4 路由切回 wan 接口（若当前默认在别处）
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


def _probe_config_paths() -> List[Dict[str, Any]]:
    """探测常见 Clash 客户端配置文件位置，并补充 ~/.config/clash 下所有 yaml。

    注意：ClashX 实际加载的是【菜单里选中的订阅配置】（如 0607.yaml），
    config.yaml 只是基础模板 —— 返回全部候选并标记，由用户确认。
    """
    home = os.path.expanduser("~")
    clash_dir = os.path.join(home, ".config", "clash")
    found: List[Dict[str, Any]] = []

    # 1) ~/.config/clash 下全部 yaml（ClashX 配置目录）
    if os.path.isdir(clash_dir):
        for p in sorted(
            glob.glob(os.path.join(clash_dir, "*.yaml"))
            + glob.glob(os.path.join(clash_dir, "*.yml"))
        ):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            is_base = os.path.basename(p).startswith("config.")
            found.append(
                {
                    "path": p,
                    "size": size,
                    "base": is_base,
                    "note": "ClashX 基础模板（通常不是当前加载的配置）"
                    if is_base
                    else "订阅配置（ClashX 菜单选中的才生效）",
                }
            )
    # 2) 其他常见客户端
    other_cands = [
        os.path.join(home, ".config", "clash-verge", "clash-verge.yaml"),
        os.path.join(home, ".config", "clash-verge", "profiles.yaml"),
        os.path.join(home, ".config", "mihomo", "config.yaml"),
        "/opt/homebrew/etc/clash/config.yaml",
        "/usr/local/etc/clash/config.yaml",
    ]
    seen = {x["path"] for x in found}
    for c in other_cands:
        if c not in seen and os.path.isfile(c):
            found.append(
                {
                    "path": c,
                    "size": os.path.getsize(c),
                    "base": False,
                    "note": "其他客户端配置",
                }
            )
    found.sort(key=lambda x: (x["size"] == 0, x["base"], x["path"]))
    return found


@router.get("/api/clash/config-path")
async def clash_config_path():
    """探测本机常见 Clash 配置文件路径（供前端一键填充）。

    同时读取 ClashX 当前选中的配置（defaults selectConfigName），用于标注哪个真正生效。
    """
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


class PatchAllReq(BaseModel):
    ips: List[str] = []


@router.post("/api/clash/patch-all")
async def clash_patch_all(req: PatchAllReq):
    """把 DIRECT 规则写入 ~/.config/clash 下【所有】yaml（含 ClashX 当前加载的）。

    解决：ClashX 加载的配置不确定（菜单可切换任意订阅），写单个文件可能不生效。
    全部写入后无论用户选哪个配置，重载后规则都在。
    """
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
