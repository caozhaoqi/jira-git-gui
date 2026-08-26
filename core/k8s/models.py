# -*- coding: utf-8 -*-
"""K8s 快照公共模型与解析工具。

由 ``core/k8s_snapshot_fetch`` / ``core/k8s_snapshot_render`` 去重合并而来：
两模块原本各自复制了 ``parse_pod`` / ``compute_age`` / ``classify``，此处统一维护
单一实现，供抓取与渲染模块共用（``run_snapshot`` 等上层逻辑可直接从此处导入）。
"""
import datetime as dt


def parse_pod(item):
    meta = item.get("metadata", {})
    status = item.get("status", {})
    spec = item.get("spec", {})
    name = meta.get("name", "?")
    containers = [c.get("name") for c in spec.get("containers", [])]
    cs = status.get("containerStatuses", []) or []
    ready = sum(1 for c in cs if c.get("ready"))
    total = len(cs)
    restarts = max((c.get("restartCount", 0) for c in cs), default=0)
    phase = status.get("phase", "") or ("Terminating" if meta.get("deletionTimestamp") else "")
    reason = status.get("reason", "") or ""
    container_reasons = []
    for c in cs:
        for st_name, st_val in (c.get("state", {}) or {}).items():
            if st_name in ("waiting", "terminated") and st_val.get("reason"):
                container_reasons.append("%s:%s" % (c.get("name"), st_val.get("reason")))
    return {
        "name": name,
        "ready": ready,
        "total": total,
        "restarts": restarts,
        "phase": phase,
        "reason": reason,
        "container_reasons": container_reasons,
        "containers": containers,
        "node": spec.get("nodeName", ""),
        "host_ip": status.get("hostIP", ""),
        "pod_ip": status.get("podIP", ""),
        "created": meta.get("creationTimestamp", ""),
    }


def compute_age(created_iso):
    if not created_iso:
        return "?"
    try:
        created = dt.datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        delta = dt.datetime.now(dt.timezone.utc) - created
        days, hours = delta.days, delta.seconds // 3600
        mins = (delta.seconds % 3600) // 60
        if days > 0:
            return "%dd%dh" % (days, hours)
        if hours > 0:
            return "%dh%dm" % (hours, mins)
        return "%dm" % mins
    except Exception:
        return "?"


def classify(rec, restart_threshold):
    problems = []
    if rec["phase"] in ("Failed", "Terminating"):
        problems.append(("HIGH", rec["phase"]))
    if rec["reason"] == "Evicted" or rec["phase"] == "Evicted":
        problems.append(("HIGH", "Evicted"))
    for cr in rec["container_reasons"]:
        if any(k in cr for k in ("CrashLoopBackOff", "OOMKilled", "Error", "ErrImage")):
            problems.append(("HIGH", cr))
    if rec["restarts"] >= restart_threshold:
        problems.append(("HIGH", "restarts=%d" % rec["restarts"]))
    elif rec["restarts"] > 0:
        problems.append(("MED", "restarts=%d" % rec["restarts"]))
    if rec["phase"] == "Running" and rec["total"] and rec["ready"] < rec["total"]:
        problems.append(("MED", "未就绪 %d/%d" % (rec["ready"], rec["total"])))
    if rec["phase"] == "Pending":
        problems.append(("MED", "Pending"))
    sev = "OK"
    if any(p[0] == "HIGH" for p in problems):
        sev = "HIGH"
    elif any(p[0] == "MED" for p in problems):
        sev = "MED"
    return sev, problems
