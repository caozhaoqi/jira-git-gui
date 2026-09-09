# -*- coding: utf-8 -*-
"""Kibana 面板 —— 真实浏览器 UI 冒烟（Playwright + Chromium）。

补齐「API/构建都过、但 UI 没在浏览器跑过」这最后一环：验证 React 面板能渲染、
Kibana tab 可切换、站点下拉加载、容器/检索两种视角都能出内容，且无未捕获 JS 异常。

前置：需先 `playwright install chromium` 且服务在跑::

    PYTHONPATH=. ./venv/bin/python -m api.server --port 8787   # 服务
    BASE=http://127.0.0.1:8787 ./venv/bin/python tools/kibana_browser_smoke.py

退出码：全部通过 0；任一断言失败 1。
"""
import os
import sys

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

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

    print(f"== Kibana 浏览器冒烟 @ {BASE}/web/ ==")
    page_errors = []
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        # 未捕获 JS 异常 = 硬失败
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        # console error：过滤离线字体/外链资源等良性噪音
        def _on_console(msg):
            if msg.type == "error":
                txt = msg.text
                if any(k in txt for k in
                       ("fonts.googleapis", "fonts.gstatic", "favicon",
                        "Failed to load resource", "net::ERR", "status of 4", "status of 5")):
                    return
                console_errors.append(txt)
        page.on("console", _on_console)

        page.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
        check("SPA 首页加载（无导航崩溃）", True)

        # 切到 Kibana tab
        page.wait_for_selector('button[data-tab="kibana"]', timeout=20000)
        page.click('button[data-tab="kibana"]')
        page.wait_for_selector(".kibana-panel", timeout=20000)
        check("Kibana 面板渲染（.kibana-panel 出现）", True)

        # 站点下拉加载（reloadSites -> /api/kibana/sites）
        page.wait_for_function(
            "document.querySelectorAll('.kibana-topbar select.sel option').length > 0",
            timeout=20000,
        )
        opts = page.eval_on_selector_all(
            ".kibana-topbar select.sel option", "els => els.map(e => e.textContent)")
        check("站点下拉已填充（sites 接口生效）", bool(opts), f"options={opts}")

        # 两个子视角 tab
        subtabs = page.eval_on_selector_all(
            ".kibana-subtab", "els => els.map(e => e.textContent.trim())")
        check("子视角 tab 齐全（容器/检索）", len(subtabs) == 2, f"subtabs={subtabs}")

        # 容器视角：默认应能看到 Pod 树（拉过 pods 后）
        try:
            page.wait_for_selector(".kb-tree-pod", timeout=25000)
            pods = page.eval_on_selector_all(".kb-tree-pod", "els => els.length")
            check("容器视角 Pod 树渲染", pods > 0, f"pods={pods}")
        except Exception as e:
            check("容器视角 Pod 树渲染", False, str(e)[:120])

        # 切到检索视角，等直方图
        try:
            page.click('.kibana-subtab:has-text("检索")')
            page.wait_for_selector(".kb-hist-bars", timeout=25000)
            bars = page.eval_on_selector_all(".kb-bar", "els => els.length")
            check("检索视角时间直方图渲染", bars > 0, f"bars={bars}")
        except Exception as e:
            check("检索视角时间直方图渲染", False, str(e)[:120])

        # 截图存盘，便于人工看
        shot = "/tmp/kibana_panel.png"
        page.screenshot(path=shot, full_page=False)
        print(f"  📸 截图: {shot}")

        browser.close()

    # 运行期异常判定
    check("无未捕获 JS 异常（pageerror）", not page_errors,
          ("; ".join(page_errors[:3]) if page_errors else ""))
    check("无致命 console error", not console_errors,
          ("; ".join(console_errors[:3]) if console_errors else ""))

    print(f"\n== 结果: PASS={PASS} FAIL={FAIL} ==")
    if FAIL:
        print("失败项:", FAILS)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
