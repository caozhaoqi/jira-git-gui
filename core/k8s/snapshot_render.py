# -*- coding: utf-8 -*-
"""K8s 快照渲染（由 core/k8s_snapshot.py 拆分，保持 import 兼容）。

仅负责把快照结果渲染为 HTML。Pod 解析与分类工具已去重到 ``core.k8s.models``，
本模块直接引用，避免与 ``k8s_snapshot_fetch`` 重复实现。
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

from .models import parse_pod, compute_age, classify

SEV_COLOR = {"HIGH": "#c0392b", "MED": "#d97706", "OK": "#16a34a"}
LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
LOG_LEVEL_DEFAULT = "INFO"


# --------------------------------------------------------------------- 日志
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
