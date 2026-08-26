# -*- coding: utf-8 -*-
"""K8s 网络连通性检测：kubectl / kubeconfig / 集群 / 内网 / 外网。

依赖 ``core.k8s.env.get_env`` 与 ``core.k8s.pods.run_kubectl_env`` /
``core.k8s.run_kubectl``（基础封装在本包内）。
"""
import re
import socket
import time
from pathlib import Path

from .env import get_env
from .pods import run_kubectl_env
from .kubectl import run_kubectl


def _api_server_host(kubeconfig):
    """从 kubeconfig 解析第一个 cluster.server 的 (host, port)。"""
    if not kubeconfig or not Path(kubeconfig).exists():
        return None
    try:
        text = Path(kubeconfig).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"server:\s*([^\s#]+)", text)
    if not m:
        return None
    url = m.group(1).strip().rstrip("/")
    mm = re.match(r"https?://([^:/]+)(?::(\d+))?", url)
    if not mm:
        return None
    host = mm.group(1)
    port = int(mm.group(2)) if mm.group(2) else 443
    return host, port


def _split_host(target, default_port=443):
    if ":" in target:
        h, p = target.rsplit(":", 1)
        try:
            return h, int(p)
        except ValueError:
            return target, default_port
    return target, default_port


def _tcp_probe(host, port, timeout=3):
    """TCP 探测，返回 (ok, latency_ms|None)。"""
    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True, round((time.time() - t0) * 1000, 1)
    except Exception:
        return False, None


def detect_network(env_name, extra_hosts=None, on_log=None):
    """检测当前机器到指定环境的网络状况。

    返回 dict：
        env, checks[], intranet[], cluster_ok, internet_ok, summary
    checks[] 元素：{name, status:'ok'|'warn'|'fail', detail}
    """
    if on_log is None:
        on_log = lambda m: None  # noqa: E731
    _, env = get_env(env_name)
    checks = []
    intranet = []

    # 1) kubectl 客户端
    on_log("[*] 检测 kubectl 客户端…")
    out, rc, err = run_kubectl(["version", "--client", "--request-timeout=5s"], timeout=15)
    if rc == 0:
        first = out.strip().split("\n", 1)[0][:140]
        checks.append({"name": "kubectl 客户端", "status": "ok", "detail": first})
    else:
        checks.append({"name": "kubectl 客户端", "status": "fail",
                       "detail": err.strip()[:200] or "未安装 kubectl"})

    # 2) kubeconfig 文件
    kc = env.get("kubeconfig")
    if kc and Path(kc).exists():
        checks.append({"name": "kubeconfig 文件", "status": "ok", "detail": kc})
    else:
        checks.append({"name": "kubeconfig 文件", "status": "fail",
                       "detail": "未配置或不存在：%s" % (kc or "(空)")})

    # 3) 集群连通（kubectl version）
    on_log("[*] 检测集群连通…")
    out, rc, err = run_kubectl_env(env_name, ["version", "--request-timeout=5s"], timeout=15)
    cluster_ok = rc == 0
    checks.append({
        "name": "集群连通 (kubectl version)",
        "status": "ok" if cluster_ok else "fail",
        "detail": (out.strip().split("\n", 1)[0][:140] if cluster_ok
                   else "无法连接集群：" + err.strip()[:160]),
    })

    # 4) 内网 - API Server TCP 探测（最关键的"是否在内网/VPN"信号）
    on_log("[*] 探测集群 API Server（内网）…")
    api = _api_server_host(kc)
    if api:
        host, port = api
        ok, ms = _tcp_probe(host, port, 3)
        intranet.append({"target": "%s:%d" % (host, port), "ok": ok, "ms": ms})
        checks.append({
            "name": "内网·集群 API Server",
            "status": "ok" if ok else "fail",
            "detail": ("可达 %s (延迟 %sms)" % (host, ms)) if ok
            else ("不可达 %s:%d（可能不在内网/VPN）" % (host, port)),
        })
    else:
        checks.append({"name": "内网·集群 API Server", "status": "warn",
                      "detail": "未能从 kubeconfig 解析 API Server 地址"})

    # 5) 用户自定义内网主机
    hosts = list(extra_hosts or []) + list(env.get("intranet_hosts") or [])
    for h in hosts:
        host, port = _split_host(h, 443)
        ok, ms = _tcp_probe(host, port, 3)
        intranet.append({"target": "%s:%d" % (host, port), "ok": ok, "ms": ms})
        checks.append({
            "name": "内网探测 %s:%d" % (host, port),
            "status": "ok" if ok else "fail",
            "detail": ("可达 (延迟 %sms)" % ms) if ok else "不可达",
        })

    # 6) 外网探测（判断是双通还是仅内网）
    on_log("[*] 探测外网…")
    ok, ms = _tcp_probe("8.8.8.8", 53, 3)
    internet_ok = ok
    checks.append({
        "name": "外网探测 (8.8.8.8:53)",
        "status": "ok" if ok else "warn",
        "detail": ("可达 (延迟 %sms)" % ms) if ok else "不可达（可能处于隔离内网）",
    })

    fails = [c for c in checks if c["status"] == "fail"]
    if not fails:
        summary = "网络正常：可连接集群与内网。"
    else:
        summary = "存在 %d 项异常（多因未接入对应内网/VPN 或 kubeconfig 缺失）。" % len(fails)
    return {
        "env": env_name,
        "checks": checks,
        "intranet": intranet,
        "cluster_ok": cluster_ok,
        "internet_ok": internet_ok,
        "summary": summary,
    }
