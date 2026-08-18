# -*- coding: utf-8 -*-
"""Kubernetes Pod 状态 / 日志快照核心逻辑（无 PyQt 依赖，可在后台线程调用）。

设计：
- ``run_snapshot(opts, on_log=..., on_progress=..., should_cancel=...)`` 是唯一的对外入口，
  供 GUI（Worker 子线程）或 CLI 调用。
- 通过回调 ``on_log(msg)`` / ``on_progress(done, total, name)`` 把进度推给 UI；
  ``should_cancel()`` 返回 True 时中途退出（返回已完成的部分结果）。
- 配置类错误（kubectl 缺失、抓不到 pod）抛 ``core.errors.UserError``，由上层以干净文案提示；
  其余异常保持原样上抛，便于追溯。
- 输出：``report.html``（状态表 + 异常卡片 + 日志）+ ``pods.json``（可回放）+ ``summary.json`` + ``logs/<pod>.log``。

``opts`` 字段（均为可选，缺省取默认值）：
    namespace          str   命名空间（默认当前上下文默认 ns）
    selector           str   label 选择器，如 "app=hcm-core"
    pod_filter         str   pod 名正则，如 "hcm-core|celery"
    tail               int   每容器抓取日志行数（默认 200）
    restart_threshold  int   重启次数 >= 此值视为 HIGH（默认 5）
    all_logs           bool  抓取所有 pod 日志（默认仅异常 pod）
    include_previous   bool  同时抓取重启前容器日志（--previous）
    out_dir            str   输出目录（默认 ~/k8s_snapshots/<时间戳>）
    kubeconfig         str   kubeconfig 路径（默认用环境变量 KUBECONFIG）
    infile             str   离线模式：直接读取 kubectl get pods -o json 文件
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
from pathlib import Path

from core.errors import UserError

SEV_COLOR = {"HIGH": "#c0392b", "MED": "#d97706", "OK": "#16a34a"}


# --------------------------------------------------------------------- kubectl
def run_kubectl(args, kubeconfig=None, timeout=60):
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout, proc.returncode, proc.stderr
    except subprocess.TimeoutExpired:
        return "", 124, "kubectl timed out"
    except FileNotFoundError:
        return "", 127, "kubectl 不在 PATH 中（请先安装 kubectl 并加入 PATH）"


def _current_context(kubeconfig=None):
    out, rc, _ = run_kubectl(["config", "current-context"], kubeconfig)
    return out.strip() if rc == 0 else "?"


# --------------------------------------------------------------------- 解析
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


# --------------------------------------------------------------------- 日志
def fetch_logs(pod_name, container, kubeconfig, namespace, tail, previous, timeout=30):
    args = ["logs", pod_name]
    if namespace:
        args += ["-n", namespace]
    if container:
        args += ["--container", container]
    args += ["--tail", str(tail)]
    if previous:
        args += ["--previous"]
    out, rc, err = run_kubectl(args, kubeconfig, timeout=timeout)
    if rc == 0:
        return out
    return "# 无法获取日志 (rc=%d): %s" % (rc, err.strip()[:300])


# --------------------------------------------------------------------- HTML
def render_html(summary, records, logs_map, generated_at, cluster_info):
    rows = []
    for r in records:
        color = SEV_COLOR[r["sev"]]
        prob = "; ".join("%s:%s" % (lv, msg) for lv, msg in r["problems"]) or "—"
        rows.append(
            "<tr class='%s'>"
            "<td><span class='dot' style='background:%s'></span>%s</td>"
            "<td>%s</td><td>%d/%d</td><td>%d</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "</tr>"
            % (
                r["sev"], color, html.escape(r["name"]), html.escape(r["phase"]),
                r["ready"], r["total"], r["restarts"], html.escape(r["reason"] or "—"),
                html.escape(r["node"] or "—"), html.escape(r["host_ip"] or "—"),
                html.escape(r["pod_ip"] or "—"), html.escape(r["age"]),
            )
        )
    table_rows = "\n".join(rows)

    problem_cards = []
    for r in records:
        if r["sev"] == "OK":
            continue
        logs_html = ""
        for cname, log in logs_map.get(r["name"], {}).items():
            label = cname if len(r["containers"]) > 1 else "logs"
            logs_html += (
                "<div class='logblock'><div class='loglabel'>%s</div>"
                "<pre class='log'>%s</pre></div>"
                % (html.escape(label), html.escape(log[-4000:]))
            )
        problem_cards.append(
            "<div class='card' style='border-left:6px solid %s'>"
            "<div class='cardtitle'><span class='dot' style='background:%s'></span>%s "
            "<span class='badge'>%s</span></div>"
            "<div class='cardmeta'>phase=%s | restarts=%d | node=%s | %s</div>"
            "<div class='cardprob'>问题: %s</div>%s</div>"
            % (
                SEV_COLOR[r["sev"]], SEV_COLOR[r["sev"]], html.escape(r["name"]), r["sev"],
                html.escape(r["phase"]), r["restarts"], html.escape(r["node"] or "—"),
                html.escape(r["age"]),
                html.escape("; ".join("%s:%s" % (lv, m) for lv, m in r["problems"])),
                logs_html or "<div class='nolog'>（无日志）</div>",
            )
        )
    problem_html = "\n".join(problem_cards) or "<p class='ok'>✅ 未发现异常 Pod</p>"

    s = summary
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K8s Pod 快照 - %s</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:#f5f6f8;color:#1f2329;margin:0;padding:24px;line-height:1.5;}
  h1{font-size:20px;margin:0 0 4px;}
  .sub{color:#6b7280;font-size:13px;margin-bottom:18px;}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;}
  .stat{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 18px;
        min-width:110px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .stat .n{font-size:24px;font-weight:700;}
  .stat .l{font-size:12px;color:#6b7280;}
  .stat.high .n{color:#c0392b;} .stat.med .n{color:#d97706;} .stat.ok .n{color:#16a34a;}
  table{border-collapse:collapse;width:100%%;background:#fff;border-radius:10px;overflow:hidden;
        box-shadow:0 1px 2px rgba(0,0,0,.04);font-size:13px;}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #eef0f2;}
  th{background:#f0f2f5;font-weight:600;color:#374151;position:sticky;top:0;}
  tr.HIGH{background:#fdf0ef;} tr.MED{background:#fef6ef;}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%%;margin-right:6px;vertical-align:middle;}
  h2{font-size:16px;margin:24px 0 12px;}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;
        margin-bottom:12px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
  .cardtitle{font-weight:700;font-size:14px;}
  .cardmeta{color:#6b7280;font-size:12px;margin:4px 0;}
  .cardprob{font-size:12px;color:#c0392b;margin-bottom:6px;}
  .badge{font-size:11px;padding:1px 7px;border-radius:10px;background:#c0392b;color:#fff;margin-left:6px;}
  .logblock{margin-top:6px;}
  .loglabel{font-size:11px;color:#6b7280;margin:2px 0;}
  pre.log{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;max-height:320px;
          overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-all;margin:0;}
  .nolog{color:#9ca3af;font-size:12px;}
  .ok{color:#16a34a;font-weight:600;}
</style></head><body>
<h1>Kubernetes Pod 状态与日志快照</h1>
<div class="sub">生成时间: %s &nbsp;|&nbsp; 集群: %s &nbsp;|&nbsp; 命名空间: %s &nbsp;|&nbsp; 过滤: %s</div>
<div class="cards">
  <div class="stat"><div class="n">%d</div><div class="l">Pod 总数</div></div>
  <div class="stat ok"><div class="n">%d</div><div class="l">正常</div></div>
  <div class="stat med"><div class="n">%d</div><div class="l">警告(MED)</div></div>
  <div class="stat high"><div class="n">%d</div><div class="l">异常(HIGH)</div></div>
  <div class="stat high"><div class="n">%d</div><div class="l">抓取日志数</div></div>
</div>
<h2>异常 Pod 详情</h2>
%s
<h2>全部 Pod 状态</h2>
<table><thead><tr>
  <th>名称</th><th>状态</th><th>就绪</th><th>重启</th><th>原因</th>
  <th>节点</th><th>HostIP</th><th>PodIP</th><th>运行时长</th>
</tr></thead><tbody>
%s
</tbody></table>
</body></html>""" % (
        generated_at, html.escape(generated_at), html.escape(cluster_info.get("context", "—")),
        html.escape(cluster_info.get("namespace", "全部")), html.escape(cluster_info.get("filter", "无")),
        s["total"], s["ok"], s["med"], s["high"], s["logs"], problem_html, table_rows,
    )


# --------------------------------------------------------------------- 主入口
def run_snapshot(opts, on_log=None, on_progress=None, should_cancel=None):
    """执行一次快照，返回结果 dict。详见模块 docstring。"""
    if on_log is None:
        on_log = lambda m: None  # noqa: E731
    if on_progress is None:
        on_progress = lambda d, t, n: None  # noqa: E731
    if should_cancel is None:
        should_cancel = lambda: False  # noqa: E731

    namespace = opts.get("namespace") or None
    selector = opts.get("selector") or None
    pod_filter = opts.get("pod_filter") or None
    tail = int(opts.get("tail", 200))
    restart_threshold = int(opts.get("restart_threshold", 5))
    all_logs = bool(opts.get("all_logs", False))
    include_previous = bool(opts.get("include_previous", False))
    kubeconfig = opts.get("kubeconfig") or None
    infile = opts.get("infile") or None

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if opts.get("out_dir"):
        out_dir = Path(opts["out_dir"])
    else:
        out_dir = Path.home() / "k8s_snapshots" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = out_dir / "logs"
    logs_dir.mkdir(exist_ok=True)

    # 1) 获取 pods
    if infile:
        on_log("[*] 离线模式，读取 %s" % infile)
        try:
            data = json.loads(Path(infile).read_text(encoding="utf-8"))
        except Exception as e:
            raise UserError("读取 infile 失败：%s" % e)
        cluster_info = {"context": "offline", "namespace": namespace or "—", "filter": pod_filter or "无"}
    else:
        on_log("[*] 抓取 pods（namespace=%s, selector=%s）…" % (namespace or "默认", selector or "无"))
        args = ["get", "pods", "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        if selector:
            args += ["-l", selector]
        out, rc, err = run_kubectl(args, kubeconfig)
        if rc != 0:
            if rc == 127:
                raise UserError("未找到 kubectl，请先安装并加入 PATH。")
            raise UserError("kubectl get pods 失败：%s" % err.strip())
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise UserError("kubectl get pods 返回非 JSON（可能未配置 kubeconfig / 上下文）。")
        cluster_info = {
            "context": _current_context(kubeconfig),
            "namespace": namespace or "默认",
            "filter": pod_filter or "无",
        }

    items = data.get("items", []) if isinstance(data, dict) else data
    on_log("[*] 共 %d 个 pod" % len(items))

    # 2) 解析 + 分类
    records = []
    pat = re.compile(pod_filter) if pod_filter else None
    for it in items:
        rec = parse_pod(it)
        if pat and not pat.search(rec["name"]):
            continue
        sev, problems = classify(rec, restart_threshold)
        rec["sev"] = sev
        rec["problems"] = problems
        rec["age"] = compute_age(rec["created"])
        records.append(rec)

    records.sort(key=lambda r: ({"HIGH": 0, "MED": 1, "OK": 2}[r["sev"]], -r["restarts"], r["name"]))

    # 3) 抓日志
    logs_map = {}
    logs_count = 0
    total_pods = len(records)
    done = 0
    for rec in records:
        if should_cancel():
            on_log("[*] 用户取消，返回已完成部分。")
            break
        need = all_logs or rec["sev"] != "OK"
        if not need:
            done += 1
            on_progress(done, total_pods, rec["name"])
            continue
        pod_logs = {}
        containers = rec["containers"] or [None]
        for c in containers:
            log = fetch_logs(rec["name"], c, kubeconfig, namespace, tail, include_previous)
            key = c or "main"
            pod_logs[key] = log
            fname = "%s__%s.log" % (rec["name"], key) if c else "%s.log" % rec["name"]
            (logs_dir / fname).write_text(log, encoding="utf-8")
            logs_count += 1
        logs_map[rec["name"]] = pod_logs
        done += 1
        on_progress(done, total_pods, rec["name"])

    # 4) 统计 + 落盘
    summary = {
        "total": len(records),
        "ok": sum(1 for r in records if r["sev"] == "OK"),
        "med": sum(1 for r in records if r["sev"] == "MED"),
        "high": sum(1 for r in records if r["sev"] == "HIGH"),
        "logs": logs_count,
        "generated_at": ts,
        "cancelled": should_cancel(),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot = {
        "generated_at": ts,
        "cluster_info": cluster_info,
        "pods": [{k: v for k, v in r.items() if k != "created"} for r in records],
    }
    (out_dir / "pods.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    html_doc = render_html(summary, records, logs_map, ts, cluster_info)
    report_path = out_dir / "report.html"
    report_path.write_text(html_doc, encoding="utf-8")

    on_log("[*] 报告已生成：%s" % report_path)

    return {
        "out_dir": str(out_dir),
        "report": str(report_path),
        "summary": summary,
        "records": snapshot["pods"],
    }
