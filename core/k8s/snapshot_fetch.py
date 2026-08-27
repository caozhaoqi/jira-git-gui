# -*- coding: utf-8 -*-
"""K8s 快照子模块（由 core/k8s_snapshot.py 拆分，保持 import 兼容）。

负责：pods 抓取（kubectl get）、日志抓取、快照编排（run_snapshot）。
Pod 解析/分类工具统一来自 ``core.k8s.models``（与渲染模块共用单一实现）。
"""
import asyncio
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from core.errors import UserError

from .kubectl import run_kubectl, _resolve_kubectl_binary, _current_context
from .models import parse_pod, compute_age, classify
from .snapshot_render import render_html

SEV_COLOR = {"HIGH": "#c0392b", "MED": "#d97706", "OK": "#16a34a"}
LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
LOG_LEVEL_DEFAULT = "INFO"


# --------------------------------------------------------------------- 日志
def fetch_logs(pod_name, container, kubeconfig, namespace, tail, previous, timeout=30, timestamps=False, since=None, until=None):
    args = ["logs", pod_name]
    if namespace:
        args += ["-n", namespace]
    if container:
        args += ["--container", container]
    args += ["--tail", str(tail)]
    if previous:
        args += ["--previous"]
    if timestamps:
        args += ["--timestamps"]
    if since:
        args += ["--since", str(since)]
    if until:
        args += ["--until", str(until)]
    out, rc, err = run_kubectl(args, kubeconfig, timeout=timeout)
    if rc == 0:
        return out
    return "# 无法获取日志 (rc=%d): %s" % (rc, err.strip()[:300])


# --------------------------------------------------------------------- HTML
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
    log_level = (opts.get("log_level") or LOG_LEVEL_DEFAULT).upper()
    if log_level not in LOG_LEVELS:
        log_level = LOG_LEVEL_DEFAULT
    _lv = LOG_LEVELS[log_level]

    def _log(level, msg):
        """按日志级别过滤输出。"""
        if LOG_LEVELS.get(level, 0) >= _lv:
            on_log(msg)

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
        _log("INFO", "[*] 离线模式，读取 %s" % infile)
        try:
            data = json.loads(Path(infile).read_text(encoding="utf-8"))
        except Exception as e:
            raise UserError("读取 infile 失败：%s" % e)
        cluster_info = {"context": "offline", "namespace": namespace or "—", "filter": pod_filter or "无"}
    else:
        _log("INFO", "[*] 抓取 pods（namespace=%s, selector=%s）…" % (namespace or "默认", selector or "无"))
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
    _log("INFO", "[*] 共 %d 个 pod" % len(items))

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
            _log("WARNING", "[*] 用户取消，返回已完成部分。")
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

    _log("INFO", "[*] 报告已生成：%s" % report_path)

    return {
        "out_dir": str(out_dir),
        "report": str(report_path),
        "summary": summary,
        "records": snapshot["pods"],
    }
