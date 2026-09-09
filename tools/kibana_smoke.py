# -*- coding: utf-8 -*-
"""Kibana 后端端点冒烟（真实打 73.2.3.27）。

不是单测（需要网络 + 真实 Kibana），仅用于改动后端后人工验证。用法::

    PYTHONPATH=. ./venv/bin/python tools/kibana_smoke.py

注意：用 ``httpx.ASGITransport`` 直连 ASGI app，**不触发 lifespan**，
因此不会跑启动时的 CF 自动登录（那会联网几十秒）。
"""
import asyncio
import sys
import time

import httpx

import api.server  # noqa: F401  必须导入以注册全部路由
from api.common import app


def show(name, resp):
    try:
        data = resp.json()
    except Exception:
        data = {"_raw": resp.text[:400]}
    head = f"[{resp.status_code}] {name} ok={data.get('ok') if isinstance(data, dict) else '?'}"
    if isinstance(data, dict) and not data.get("ok"):
        head += f" error={data.get('error')}"
    print(head)
    if not isinstance(data, dict):
        return data
    for k in ("kibana_version", "current", "source", "total", "cached", "count", "filename"):
        if k in data:
            print(f"    {k}: {data[k]}")
    for k in ("steps",):
        if k in data:
            for s in data[k]:
                print(f"    step {s['name']}: {'OK' if s['ok'] else 'FAIL'} - {s['detail']}")
    for k in ("namespaces", "containers", "apps", "hosts", "pods"):
        if k in data and data[k] and "key" in data[k][0]:
            print(f"    {k}({len(data[k])}): {[(b['key'], b['count']) for b in data[k]][:8]}")
    if "pods" in data and data["pods"] and "pod" in data["pods"][0]:
        print(f"    pods({len(data['pods'])}): "
              f"{[(p['pod'], p['count'], p['errors']) for p in data['pods']][:4]}")
    if "buckets" in data:
        print(f"    buckets({len(data['buckets'])}) interval={data.get('interval')}: "
              f"{data['buckets'][:2]}")
    if "rows" in data:
        print(f"    rows({len(data['rows'])})")
        for r in data["rows"][:2]:
            print(f"      [{r['ts']}] {r['level']} {r['container']}/{r['pod']}: "
                  f"{r['msg'][:90]}")
    if "content" in data:
        print("    content head:", (data["content"] or "")[:150].replace("\n", " | "))
    return data


async def main():
    t0 = time.time()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://smoke",
                                 timeout=180) as C:
        show("GET  /api/kibana/sites", await C.get("/api/kibana/sites"))
        show("POST /api/kibana/test",
             await C.post("/api/kibana/test", json={"name": "hnrc"}))
        show("GET  /api/kibana/fields",
             await C.get("/api/kibana/fields", params={"start": "now-24h"}))
        pods = show("GET  /api/kibana/pods",
                    await C.get("/api/kibana/pods", params={"start": "now-1h"}))
        show("POST /api/kibana/logs", await C.post("/api/kibana/logs", json={
            "start": "now-30m", "size": 5, "levels": ["ERROR"]}))

        anchor = None
        if pods.get("ok") and pods.get("pods"):
            pod = pods["pods"][0]["pod"]
            d = show(f"POST /api/kibana/logs (pod={pod})",
                     await C.post("/api/kibana/logs",
                                  json={"start": "now-2h", "pod": pod, "size": 3}))
            if d.get("rows"):
                anchor = d["rows"][0]["ts"]

        show("POST /api/kibana/histogram",
             await C.post("/api/kibana/histogram", json={"start": "now-6h"}))
        if anchor:
            show("POST /api/kibana/context", await C.post("/api/kibana/context", json={
                "anchor": anchor, "before": 5, "after": 5}))
        show("POST /api/kibana/export",
             await C.post("/api/kibana/export", json={"start": "now-30m", "size": 50}))
        show("POST /api/kibana/cache/clear",
             await C.post("/api/kibana/cache/clear", json={"site": "current"}))
    print(f"\n总耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
