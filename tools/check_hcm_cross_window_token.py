# -*- coding: utf-8 -*-
"""验证跨窗口打开 HCM 详情/错误定位页时，token + 网关 target 通过 URL 参数落地。

复现原 bug：旧逻辑 window.open 后新窗口还是 about:blank，写 w.localStorage 抛 SecurityError 被吞，
导致详情页读不到 token 报 configRequired。新逻辑由 openHcmWindow 在 URL 注入 hcm-token/hcm-target，
目标页挂载时落地到 store，并透传给后端 /api/hcm/direct。

验证点：
1. 加载 ?hcm-detail 页后，store/localStorage 的 hcm.token == URL 中的 hcm-token（落地成功）。
2. 详情页发起的 /api/hcm/direct 请求体里 token == hcm-token 且 target == hcm-target（透传成功）。
3. 加载 ?hcm-cf-err 页后，localStorage 的 hcm.token == URL 中的 hcm-token（落地成功）。
4. 全程无未捕获 JS 异常。
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8799".rstrip("/")
DETAIL_URL = (
    BASE + "/web/?hcm-detail=1&hcm-model=DemoModel"
    "&hcm-token=UNITTEST_TOKEN_123&hcm-target=http://example.com/gw"
)
CF_ERR_URL = (
    BASE + "/web/?hcm-cf-err=1&hcm-token=CF_TOKEN_456&hcm-target=http://example.com/gw"
)

ok = True
js_errors = []


def capture_direct_requests(page, sink):
    page.on("request", lambda r: (
        sink.append(r) if "/api/hcm/direct" in r.url else None
    ))


with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])

    # ---------- 1) 详情页：token 落地 + target 透传 ----------
    ctx = b.new_context(viewport={"width": 1280, "height": 860})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: js_errors.append(str(e)))
    sink = []
    capture_direct_requests(pg, sink)

    pg.goto(DETAIL_URL, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_selector(".hcm-detail-page", timeout=20000)
    pg.wait_for_timeout(2500)  # 等挂载 effect 落地 token + 重新发起请求

    ls_token = pg.evaluate("() => localStorage.getItem('hcm.token')")
    print(f"[detail] localStorage hcm.token = {ls_token!r}")
    if ls_token != "UNITTEST_TOKEN_123":
        ok = False
        print("FAIL: 详情页 token 未从 URL 落地到 store (localStorage)")

    # 详情页 token 输入框应显示落地后的 token
    try:
        val = pg.input_value(".hcm-detail-token input", timeout=5000)
        print(f"[detail] token input value = {val!r}")
        if val != "UNITTEST_TOKEN_123":
            ok = False
            print("FAIL: 详情页 token 输入框未显示落地 token")
    except Exception as e:
        ok = False
        print("FAIL: 找不到详情页 token 输入框 ->", e)

    # 检查 /api/hcm/direct 请求体是否带上了 token + target
    if sink:
        last = sink[-1]
        try:
            body = json.loads(last.post_data or "{}")
        except Exception:
            body = {}
        print(f"[detail] /api/hcm/direct body = {json.dumps(body, ensure_ascii=False)}")
        if body.get("token") != "UNITTEST_TOKEN_123":
            ok = False
            print("FAIL: 详情页请求未携带正确 token")
        if body.get("target") != "http://example.com/gw":
            ok = False
            print("FAIL: 详情页请求未携带正确 target")
    else:
        ok = False
        print("FAIL: 详情页未发起 /api/hcm/direct 请求（可能仍卡在 configRequired）")

    pg.close()
    ctx.close()

    # ---------- 2) 云函数错误定位页：token 落地 ----------
    ctx2 = b.new_context(viewport={"width": 1100, "height": 900})
    pg2 = ctx2.new_page()
    pg2.on("pageerror", lambda e: js_errors.append(str(e)))
    pg2.goto(CF_ERR_URL, wait_until="domcontentloaded", timeout=60000)
    pg2.wait_for_selector(".hcm-cf-err", timeout=20000)
    pg2.wait_for_timeout(2000)

    ls_token2 = pg2.evaluate("() => localStorage.getItem('hcm.token')")
    print(f"[cf-err] localStorage hcm.token = {ls_token2!r}")
    if ls_token2 != "CF_TOKEN_456":
        ok = False
        print("FAIL: 云函数错误定位页 token 未从 URL 落地到 store")
    pg2.close()
    ctx2.close()

    b.close()

if js_errors:
    ok = False
    print("FAIL: 出现 JS 异常 ->", js_errors)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
