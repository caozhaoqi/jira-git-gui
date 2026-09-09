# -*- coding: utf-8 -*-
"""Kibana 面板 —— HTTP 级功能冒烟（真实打运行中的服务，覆盖全链路 + 前端产物）。

与 ``kibana_smoke.py``（ASGI 直连、不触发 lifespan）互补：本脚本走真实
uvicorn 监听端口，验证中间件 / 静态服务 / 路由全链路。需要服务先起来::

    PYTHONPATH=. ./venv/bin/python -m api.server --port 8787
    # 另开终端：
    BASE=http://127.0.0.1:8787 ./venv/bin/python tools/kibana_smoke_http.py

退出码：全部通过 0；任一断言失败 1。可用于 CI 卡点。

注意：本机环境设置了 HTTP_PROXY/HTTPS_PROXY（Clash 类代理），httpx 默认
trust_env=True 会把 localhost 请求也发往代理导致 404，故强制 trust_env=False
直连目标端口。
"""
import os
import sys
import time

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8787").rstrip("/")

PASS = 0
FAIL = 0
FAILS = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  ❌ {name}" + (f"  {detail}" if detail else ""))


def jget(r):
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:300]}


def main():
    print(f"== Kibana HTTP 冒烟 @ {BASE} ==")
    t0 = time.time()
    with httpx.Client(base_url=BASE, timeout=180, verify=False,
                      trust_env=False) as C:
        # 1) 站点列表
        sc, d = jget(C.get("/api/kibana/sites"))
        check("GET /api/kibana/sites -> 200", sc == 200, f"code={sc}")
        check("  sites.ok=true", d.get("ok") is True)
        sites = d.get("sites") or []
        check("  sites 非空且有 current", bool(sites) and bool(d.get("current")),
              f"n={len(sites)} current={d.get('current')}")
        site = d.get("current") or (sites[0]["name"] if sites else None)

        # 2) 站点连通性测试
        if site:
            sc, d = jget(C.post("/api/kibana/test", json={"name": site}))
            check("POST /api/kibana/test -> 200", sc == 200, f"code={sc}")
            check("  test.ok=true", d.get("ok") is True)
            steps = d.get("steps") or []
            bad = [s for s in steps if not s.get("ok")]
            check("  三步探测全 OK", not bad,
                  f"{[ (s['name'], s.get('detail')) for s in bad]}" if bad else f"steps={len(steps)}")

        # 3) 候选字段（筛选下拉）
        sc, d = jget(C.get("/api/kibana/fields", params={"start": "now-24h"}))
        check("GET /api/kibana/fields -> 200", sc == 200, f"code={sc}")
        check("  fields.ok=true 且含 namespaces/containers/apps/hosts/pods",
              d.get("ok") is True and all(k in d for k in
                  ("namespaces", "containers", "apps", "hosts", "pods")))

        # 4) Pod 概览（容器视角左树数据源）
        sc, d = jget(C.get("/api/kibana/pods", params={"start": "now-1h"}))
        check("GET /api/kibana/pods -> 200", sc == 200, f"code={sc}")
        pods = d.get("pods") or []
        check("  pods 非空且 total>0", bool(pods) and (d.get("total") or 0) > 0,
              f"n={len(pods)} total={d.get('total')}")

        # 5) 日志检索（无 pod，按级别）
        sc, d = jget(C.post("/api/kibana/logs",
                            json={"start": "now-30m", "size": 5, "levels": ["ERROR"]}))
        check("POST /api/kibana/logs (level) -> 200", sc == 200, f"code={sc}")
        check("  logs.ok=true", d.get("ok") is True)

        # 6) 日志检索（指定 pod，检索视角/容器视角主数据）
        anchor = None
        if pods:
            pod = pods[0]["pod"]
            sc, d = jget(C.post("/api/kibana/logs",
                                json={"start": "now-2h", "pod": pod, "size": 3}))
            check(f"POST /api/kibana/logs (pod={pod}) -> 200", sc == 200, f"code={sc}")
            rows = d.get("rows") or []
            check("  返回非空日志行", bool(rows), f"rows={len(rows)}")
            if rows:
                anchor = rows[0]["ts"]

        # 7) 时间直方图（检索视角顶部）
        sc, d = jget(C.post("/api/kibana/histogram", json={"start": "now-6h"}))
        check("POST /api/kibana/histogram -> 200", sc == 200, f"code={sc}")
        check("  histogram.ok=true 且 buckets 非空",
              d.get("ok") is True and bool(d.get("buckets")),
              f"buckets={len(d.get('buckets') or [])} interval={d.get('interval')}")

        # 8) 上下文（双击某行查看上下文）
        if anchor:
            sc, d = jget(C.post("/api/kibana/context",
                                json={"anchor": anchor, "before": 5, "after": 5}))
            check("POST /api/kibana/context -> 200", sc == 200, f"code={sc}")
            check("  context.ok=true 且 rows 非空",
                  d.get("ok") is True and bool(d.get("rows")),
                  f"rows={len(d.get('rows') or [])}")

        # 9) 导出
        sc, d = jget(C.post("/api/kibana/export",
                            json={"start": "now-30m", "size": 50}))
        check("POST /api/kibana/export -> 200", sc == 200, f"code={sc}")
        check("  export.ok=true 且 content 非空",
              d.get("ok") is True and bool(d.get("content")),
              f"filename={d.get('filename')} count={d.get('count')}")

        # 10) 缓存清理
        sc, d = jget(C.post("/api/kibana/cache/clear", json={"site": "current"}))
        check("POST /api/kibana/cache/clear -> 200", sc == 200, f"code={sc}")
        check("  cache/clear.ok=true", d.get("ok") is True)

        # 11) 前端 SPA 服务（/web 挂载 dist）
        sc = C.get("/web/").status_code
        check("GET /web/ -> 200 (SPA)", sc == 200, f"code={sc}")
        js_url = None
        idx = C.get("/web/").text
        import re
        m = re.search(r'src="(/web/assets/[^"]+\.js)"', idx)
        if m:
            js_url = m.group(1)
        check("  index.html 引用了 JS bundle", bool(js_url), js_url or "未找到")
        if js_url:
            js = C.get(js_url).text
            check("  bundle 内含 kibana 面板代码", "kibana" in js,
                  f"len={len(js)}")

    print(f"\n== 结果: PASS={PASS} FAIL={FAIL} 耗时 {time.time()-t0:.1f}s ==")
    if FAIL:
        print("失败项:", FAILS)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
