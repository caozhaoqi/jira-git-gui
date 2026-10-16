# -*- coding: utf-8 -*-
"""验收「凭据失效 / 配置缺失 → 全局可操作横幅」这条链路（真实浏览器）。

本机环境是「一切正常」的（有 Cookie、有 K8s 环境、有 Kibana 站点），
所以自然状态下不会出现任何告警。这里用 route 拦截把后端响应改成失败/缺失，
走**真实组件代码路径**，验证：

  1. Kibana 站点接口返回 {ok:false, error}（此前被静默吞成「（无站点）」）
     → 打开 Kibana 页签应弹出横幅，标题「尚未配置 Kibana 站点」，正文带后端原因。
  2. /api/repos 返回 config_missing=cookie（未配置 Cookie）
     → 打开应用应弹出「尚未配置 Jira Cookie」横幅。
  3. 横幅是 role=alert，且带可操作按钮（管理站点 / 连接设置 / 忽略）。
  4. 浅色 + 深色两套主题下都渲染正常（class 已从 cookie-expired-* 改名为 app-alert*）。

用法：先确保后端在 8787 且已 `npm run build`，然后
    ./venv/bin/python tools/check_alert_banners.py
"""
import os
import sys

BASE = os.environ.get("BASE", "http://127.0.0.1:8787").rstrip("/")
SHOT_DIR = os.environ.get("SHOT_DIR", "/tmp")

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


def main():
    from playwright.sync_api import sync_playwright

    print(f"== 全局告警横幅验收 @ {BASE}/web/ ==")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---- 场景 1：Kibana 站点接口失败（ok:false） --------------------------
        print("\n[1] Kibana 站点加载失败（ok:false，此前完全静默）")
        page.route(
            "**/api/kibana/sites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok":false,"error":"连接被拒绝：无法连接 Kibana (ECONNREFUSED)","sites":[],"current":null}',
            ),
        )
        page.goto(f"{BASE}/web/", wait_until="domcontentloaded")
        page.click('.tab:has-text("Kibana")')
        alert = page.locator(".app-alert").first
        alert.wait_for(state="visible", timeout=10000)
        text = alert.inner_text()
        check("出现全局横幅", alert.count() > 0)
        check("标题是「尚未配置 Kibana 站点」", "尚未配置 Kibana 站点" in text, text.split("\n")[0])
        check("正文带后端具体原因", "ECONNREFUSED" in text)
        check("有「管理站点」操作按钮", page.locator('.app-alert button:has-text("管理站点")').count() > 0)
        check("有「知道了」可忽略", page.locator('.app-alert button:has-text("知道了")').count() > 0)
        check("是无障碍 alert", alert.get_attribute("role") == "alert")
        page.screenshot(path=f"{SHOT_DIR}/alert_kibana_light.png")

        # 深色主题：横幅不得变成深底深字
        page.evaluate("document.documentElement.classList.add('dark')")
        page.evaluate("document.body.classList.add('dark')")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{SHOT_DIR}/alert_kibana_dark.png")
        fg = alert.evaluate("el => getComputedStyle(el).color")
        bg = alert.evaluate("el => getComputedStyle(el).backgroundColor")
        check("深色下横幅仍可读（前景≠背景）", fg != bg, f"fg={fg} bg={bg}")

        # ---- 场景 2：未配置 Jira Cookie --------------------------------------
        print("\n[2] 未配置 Jira Cookie（config_missing=cookie）")
        page2 = ctx.new_page()
        page2.on("pageerror", lambda e: errors.append(str(e)))
        page2.route(
            "**/api/repos*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"repos":[],"error":"未配置 Cookie","config_missing":"cookie"}',
            ),
        )
        page2.goto(f"{BASE}/web/", wait_until="domcontentloaded")
        a2 = page2.locator(".app-alert").first
        a2.wait_for(state="visible", timeout=10000)
        t2 = a2.inner_text()
        check("出现配置缺失横幅", "尚未配置 Jira Cookie" in t2, t2.split("\n")[0])
        check("有「连接设置」入口", page2.locator('.app-alert button:has-text("连接设置")').count() > 0)
        page2.screenshot(path=f"{SHOT_DIR}/alert_jira_config.png")

        # ---- 场景 3：会话失效（auth_failed） ---------------------------------
        print("\n[3] Jira 会话失效（auth_failed）")
        page3 = ctx.new_page()
        page3.on("pageerror", lambda e: errors.append(str(e)))
        page3.route(
            "**/api/repos*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=(
                    '{"repos":[],"auth_failed":true,'
                    '"auth_detail":"HTML 仓库页跳转到登录页（HTTP 200）；'
                    'REST 返回 HTTP 401（4 次请求；接口：/rest/gitplugin/1.0/repository/all）"}'
                ),
            ),
        )
        page3.goto(f"{BASE}/web/", wait_until="domcontentloaded")
        a3 = page3.locator(".app-alert").first
        a3.wait_for(state="visible", timeout=10000)
        t3 = a3.inner_text()
        check("出现会话失效横幅", "远端会话已失效" in t3, t3.split("\n")[0])
        check("正文带后端证据（登录页 / 401）", "登录页" in t3 and "401" in t3)
        check("有「重新粘贴 Cookie」", page3.locator('.app-alert button:has-text("重新粘贴 Cookie")').count() > 0)
        check("有「改用 PAT 克隆到本地」", page3.locator('.app-alert button:has-text("改用 PAT")').count() > 0)
        page3.screenshot(path=f"{SHOT_DIR}/alert_jira_auth.png")

        # ---- 场景 4：横幅 + toast 同时出现，toast 必须让开 -------------------
        # 两者都锚在顶部，横幅一出现 toast 就会压住它的右端（含操作按钮）。
        # 横幅高度随文案行数 / 语言切换变化，所以断言的是「实测底边」而非固定像素。
        print("\n[4] 横幅与 toast 同时出现：toast 不得压住横幅")
        page4 = ctx.new_page()
        page4.on("pageerror", lambda e: errors.append(str(e)))
        page4.route(
            "**/api/kibana/sites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"ok":false,"error":"连接被拒绝：无法连接 Kibana (ECONNREFUSED)","sites":[],"current":null}',
            ),
        )
        # 让文件树报错，逼出一个 toast
        page4.route(
            "**/api/tree**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"error":"FORCED_TREE_FAIL","entries":[]}',
            ),
        )
        page4.goto(f"{BASE}/web/", wait_until="domcontentloaded")
        page4.click('.tab:has-text("Kibana")')
        box = page4.locator(".app-alerts").first
        box.wait_for(state="visible", timeout=10000)
        # 亮出横幅后再切回仓库页 → 重新拉文件树 → 必然弹 toast
        page4.click('.tab:has-text("仓库")')
        toast = page4.locator(".toast").first
        try:
            toast.wait_for(state="visible", timeout=10000)
        except Exception:
            pass
        check("横幅与 toast 同时存在", toast.count() > 0)
        if toast.count() > 0:
            ab = box.bounding_box()
            tb = toast.bounding_box()
            check("toast 落在横幅下方（不重叠）",
                  tb["y"] >= ab["y"] + ab["height"] - 1,
                  f"横幅底={ab['y'] + ab['height']:.0f} toast顶={tb['y']:.0f}")
            check("CSS 变量记录了横幅底边",
                  bool(page4.evaluate(
                      "getComputedStyle(document.documentElement).getPropertyValue('--app-alert-bottom').trim()"
                  )))
            page4.screenshot(path=f"{SHOT_DIR}/alert_toast_offset.png")

        check("无 JS 运行时报错", not errors, "; ".join(errors[:3]))
        browser.close()

    print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
    if FAILS:
        print("失败项：" + ", ".join(FAILS))
    print(f"截图：{SHOT_DIR}/alert_kibana_light.png, alert_kibana_dark.png, "
          f"alert_jira_config.png, alert_jira_auth.png, alert_toast_offset.png")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
