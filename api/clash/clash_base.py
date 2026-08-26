# -*- coding: utf-8 -*-
"""Clash 分流配置助手 —— 基础模块（常量 / 日志 / 底层工具函数 / Pydantic 模型）。

拆分自 ``api/routes_clash.py``。原文件为自包含单体（977 行），按业务子域拆分为
``routes_clash_probe`` / ``routes_clash_rules`` / ``routes_clash_config`` 三个子路由模块。

为避免子模块间的横向耦合与循环导入，这里集中放置：
- 常量（``DEFAULT_PRIVATE_CIDRS`` / ``CLASH_UI_MARK`` 等）、操作日志初始化；
- 所有被多个端点复用的底层工具函数（系统检测 ``_run`` / ``_ifconfig`` / ``_default_gateway*`` /
  ``_list_services`` / ``_host_routes`` / ``_lan_gateway`` / ``_route_cmd_for`` / ``_classify`` /
  ``_route_get`` / ``_curl_probe``，提权执行 ``_privileged_shell``，Clash 规则读写
  ``_patch_clash_rules`` / ``_unpatch_clash_rules``，配置探测 ``_probe_config_paths`` /
  ``_load_clash_defaults``，路由表快照 ``_log_route_table``）与所有 Pydantic 请求模型。
"""
import glob
import json
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

from pydantic import BaseModel

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


# --------------------------------------------------------------------------- #
#  系统检测（只读）与提权执行
# --------------------------------------------------------------------------- #
def _log_route_table(tag: str) -> None:
    """记录当前路由表快照（默认 + 所有 host 路由）。"""
    out = _run(["netstat", "-rn"], timeout=5)
    interesting = []
    for line in out.splitlines():
        if re.search(r"^(default|73\.|83\.|10\.|172\.|192\.168)", line.strip()):
            interesting.append(line.strip())
    _log.info("路由表快照[%s]:\n%s", tag, "\n".join(interesting) if interesting else "(空)")


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
    """解析 networksetup 服务列表与顺序。"""
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
    """局域网接口的网关 IP。"""
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
    """构造 host 路由命令（优先经网关，否则退回 -interface）。"""
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


def _route_get(ip: str) -> Dict[str, Any]:
    """查询某 IP 当前走哪个接口（route -n get）。"""
    out = _run(["route", "-n", "get", ip], timeout=3)
    m = re.search(r"interface:\s*(\S+)", out)
    return {"route_iface": m.group(1) if m else "", "raw": out.strip()[:300]}


def _curl_probe(ip: str) -> Dict[str, Any]:
    """连通性探测：curl -m 3 返回 HTTP 状态。"""
    host = ip.split(":")[0]
    url = f"http://{ip}/"
    out = _run(["curl", "-s", "-o", "/dev/null", "-m", "3", "-w", "%{http_code}", url], timeout=6)
    code = out.strip().splitlines()[-1] if out.strip() else ""
    return {"host": host, "http_code": code, "reachable": code.isdigit() and code != "000"}


# --------------------------------------------------------------------------- #
#  一键应用 / 撤销（osascript 提权执行 route；直接改写 Clash 配置 rules）
# --------------------------------------------------------------------------- #
_IP_HOST_RE = re.compile(r"^[\d.]+$")
_DEV_RE = re.compile(r"^[A-Za-z0-9]+$")
CLASH_UI_MARK = "# clash-ui-auto"


def _privileged_shell(script: str, timeout: int = 180):
    """通过 osascript 以管理员权限执行 shell 脚本（macOS 弹系统密码框，一次授权）。"""
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


# --------------------------------------------------------------------------- #
#  默认值配置（config/clash_defaults.local.json，含真实 IP 的文件不入库）
# --------------------------------------------------------------------------- #
_CLASH_DEFAULTS_FILE = (
    Path(__file__).resolve().parent.parent / "config" / "clash_defaults.local.json"
)


def _resolve_clash_defaults_source(primary, project_root=None):
    """定位 Clash 默认值配置，返回 (result_dict, path_or_None, source)。

    source ∈ {local, example, missing, corrupt}。
    搜索顺序：primary → 项目根 → _env_search_roots()（冻结含 MEIPASS、否则数据根）。
    命中 .local 用真实默认；缺失时回退已提交的 .example（占位 IP，非真实）；
    二者皆无则告警返回空。对齐 cf/hcm 同类的「根搜索 + 示例回退 + 告警」机制。
    """
    from core.config.connect import _env_search_roots  # 延迟导入，规避循环依赖
    # 从 primary 推导项目根（primary 即 clash_defaults.local.json 路径），避免依赖
    # 模块级 _CLASH_DEFAULTS_FILE，使测试通过 mock patch 仍能正确隔离沙箱。
    PROJ_ROOT = primary.parent.parent
    roots = [PROJ_ROOT] + _env_search_roots(project_root)
    cands = [primary]
    for r in roots:
        cands += [
            r / "config" / "clash_defaults.local.json",
            r / "config" / "clash_defaults.example.json",
            r / "clash_defaults.local.json",
            r / "clash_defaults.example.json",
        ]
    for c in cands:
        if not c.is_file():
            continue
        try:
            data = json.loads(c.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _log.warning("读取 Clash 默认值配置失败: %s", e)
            return {"default_ips": [], "lan_device": ""}, c, "corrupt"
        ips = [
            x.strip()
            for x in (data.get("default_ips") or [])
            if isinstance(x, str) and x.strip()
        ]
        lan = (data.get("lan_device") or "").strip()
        if c.name.endswith(".local.json"):
            return {"default_ips": ips, "lan_device": lan}, c, "local"
        _log.warning(
            "未找到 clash_defaults.local.json，回退示例默认值"
            "（占位 IP，请在 config/clash_defaults.local.json 配置真实默认值）"
        )
        return {"default_ips": ips, "lan_device": lan}, c, "example"
    _log.warning("clash_defaults.local.json 与示例模板均缺失，返回空默认值")
    return {"default_ips": [], "lan_device": ""}, None, "missing"


def _load_clash_defaults(project_root=None):
    """读取默认值配置；缺失/损坏时优雅降级（见 _resolve_clash_defaults_source）。"""
    result, _, _ = _resolve_clash_defaults_source(_CLASH_DEFAULTS_FILE, project_root)
    return result


def _probe_config_paths() -> List[Dict[str, Any]]:
    """探测常见 Clash 客户端配置文件位置，并补充 ~/.config/clash 下所有 yaml。"""
    home = os.path.expanduser("~")
    clash_dir = os.path.join(home, ".config", "clash")
    found: List[Dict[str, Any]] = []

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


# --------------------------------------------------------------------------- #
#  Pydantic 请求模型（供各子路由模块共用）
# --------------------------------------------------------------------------- #
class GenReq(BaseModel):
    ips: List[str] = []
    lan_device: str = ""   # 指定 IP 应走的局域网接口（en0/en1…）
    wan_device: str = ""   # 其他流量接口（WiFi），仅用于注释提示


class ApplyReq(BaseModel):
    ips: List[str] = []
    lan_device: str = ""
    clash_config_path: str = ""
    only_clash: bool = False  # True = 只改 Clash 配置，不动路由


class RevertReq(BaseModel):
    ips: List[str] = []
    lan_device: str = ""
    clash_config_path: str = ""


class DiagnoseReq(BaseModel):
    ips: List[str] = []
    clash_config_path: str = ""


class FixRouteReq(BaseModel):
    wan_device: str = ""          # 外网接口（如 en0），其服务排第一
    wan_service: str = ""         # 外网服务名（如 "Wi-Fi"）；不填则按 wan_device 自动匹配


class PatchAllReq(BaseModel):
    ips: List[str] = []
