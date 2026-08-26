# -*- coding: utf-8 -*-
"""K8s 事件 / 描述 / Top 资源用量。"""
import json
import re
from core.errors import UserError
from .env import get_env
from .pods import run_kubectl_env


def list_events(env_name, namespace=None, kind=None, name=None, limit=200, all_ns=False):
    """列出集群事件，按时间倒序。返回 {events, warning, total}。

    - ``all_ns``：跨全部命名空间（忽略 namespace）。
    - ``kind`` / ``name``：对 involvedObject 做服务端无关过滤（name 支持模糊匹配）。
    - 原始命令：``kubectl get events -o json --sort-by=.metadata.creationTimestamp``。
    """
    args = ["get", "events", "-o", "json"]
    if all_ns:
        args += ["--all-namespaces"]
    elif namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    args += ["--sort-by=.metadata.creationTimestamp"]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("获取事件失败：%s" % err.strip()[:400])
    try:
        items = json.loads(out).get("items", [])
    except Exception:
        raise UserError("kubectl 返回非 JSON（可能未连接集群）。")

    events = []
    for it in items:
        meta = it.get("metadata", {})
        last = it.get("lastTimestamp") or it.get("eventTime") or ""
        first = it.get("firstTimestamp") or it.get("eventTime") or ""
        obj = it.get("involvedObject", {})
        src = (it.get("source") or {}).get("component", "") \
            or (it.get("reportingComponent", "") or "")
        events.append({
            "type": it.get("type", "Normal"),
            "reason": it.get("reason", ""),
            "message": it.get("message", ""),
            "object_kind": obj.get("kind", ""),
            "object_name": obj.get("name", ""),
            "object_ns": obj.get("namespace", ""),
            "source": src,
            "count": it.get("count", 1),
            "first_seen": first,
            "last_seen": last,
        })
    # kubectl 默认按时间升序，倒序使最新在前
    events.reverse()
    if kind or name:
        events = [e for e in events
                  if (not kind or e["object_kind"].lower() == kind.lower())
                  and (not name or name.lower() in e["object_name"].lower())]
    if limit:
        events = events[:limit]
    warning = sum(1 for e in events if e["type"] == "Warning")
    return {"events": events, "warning": warning, "total": len(events)}


def describe_resource(env_name, kind, name, namespace=None):
    """kubectl describe <kind> <name>，返回原始文本 + 该资源相关事件（便于排障）。

    文本忠实于 kubectl describe 输出；事件由 ``list_events`` 按名称过滤得到。
    """
    if not kind or not name:
        raise UserError("请指定资源类型与名称。")
    args = ["describe", kind, name]
    if namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("describe %s/%s 失败：%s" % (kind, name, err.strip()[:400]))
    related = []
    try:
        evd = list_events(env_name, namespace=namespace, kind=kind, name=name, limit=50)
        related = evd.get("events", [])
    except Exception:
        related = []
    return {"text": out, "events": related}


def _parse_top_val(s):
    """把 kubectl top 的量（如 '250m'/'1'/'512Mi'/'2Gi'/'1Gi'）解析为浮点数值，
    统一为「核」(CPU) 或「MiB」(内存) 量级，仅用于排序与条形占比估算。
    """
    s = (s or "").strip()
    if not s:
        return 0.0
    if s.endswith("m"):                        # millicores / millis
        try:
            return float(s[:-1]) / 1000.0
        except ValueError:
            return 0.0
    m = re.match(r"^([\d.]+)\s*(Ki|Mi|Gi|Ti|K|M|G|T|i|n)?$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return 0.0
    val = float(m.group(1))
    unit = m.group(2) or ""
    mult = {
        "n": 1e-9, "Ki": 1 / 1024.0, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 * 1024,
        "K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3, "i": 1.0,
    }
    return val * mult.get(unit, 1.0)


def get_top(env_name, scope="pods", namespace=None):
    """kubectl top pods/nodes。返回 {scope, rows}。

    rows（pod）：{name, namespace, cpu, memory}
    rows（node）：{name, cpu, cpu_pct, memory, memory_pct}
    均按内存消耗降序排列，便于一眼定位资源大户。
    """
    import json
    if scope not in ("pods", "nodes"):
        scope = "pods"
    args = ["top", scope, "--no-headers"]
    if scope == "pods":
        if namespace:
            args += ["-n", namespace]
        elif (env := get_env(env_name)[1]).get("namespace"):
            args += ["-n", env["namespace"]]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        # metrics-server 未就绪是最常见原因
        raise UserError("获取 Top 失败（集群需启用 metrics-server）：%s" % err.strip()[:400])
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if scope == "pods":
            if len(parts) >= 3:
                rows.append({
                    "name": parts[0],
                    "namespace": "",
                    "cpu": parts[1],
                    "memory": parts[2],
                })
            elif len(parts) == 2:        # 极少数输出仅 NAME + CPU
                rows.append({"name": parts[0], "namespace": "", "cpu": parts[1], "memory": "?"})
        else:
            if len(parts) >= 5:
                rows.append({
                    "name": parts[0], "cpu": parts[1], "cpu_pct": parts[2],
                    "memory": parts[3], "memory_pct": parts[4],
                })
            elif len(parts) >= 3:
                rows.append({
                    "name": parts[0], "cpu": parts[1], "cpu_pct": "",
                    "memory": parts[2], "memory_pct": "",
                })
    rows.sort(key=lambda r: _parse_top_val(r.get("memory", "")), reverse=True)
    return {"scope": scope, "rows": rows}
