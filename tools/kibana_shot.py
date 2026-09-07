"""用系统 Chrome 渲染 Kibana 面板并截图（不下载 Chromium）。

环境：本机有 /Applications/Google Chrome.app，playwright python 已装，
8799 服务在跑。直接以系统 Chrome 为 executable_path 截图，
同时捕获 console error / 未捕获异常，定位「UI 混乱」根因。
"""
import sys

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
URL = "http://127.0.0.1:8799/web/"
OUT_FULL = "/tmp/kibana_full.png"
OUT_PANE = "/tmp/kibana_panel.png"


def main():
    from playwright.sync_api import sync_playwright

    console_errors = []
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        print("goto", URL)
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)

        # 等 Kibana tab 出现并点击
        page.wait_for_selector('[data-tab="kibana"]', timeout=20000)
        print("click kibana tab")
        page.click('[data-tab="kibana"]')

        # 等面板容器渲染
        try:
            page.wait_for_selector(".kibana-panel", timeout=20000)
            print("panel rendered")
        except Exception as e:
            print("WARN no .kibana-panel:", e)

        # 给异步/动画一点时间
        page.wait_for_timeout(2500)

        page.screenshot(path=OUT_FULL, full_page=False)
        print("saved", OUT_FULL)

        # 单独截面板元素
        try:
            pane = page.query_selector(".kibana-panel")
            if pane:
                pane.screenshot(path=OUT_PANE)
                print("saved", OUT_PANE)
        except Exception as e:
            print("WARN pane shot failed:", e)

        browser.close()

    print("\n=== console errors/warnings ===")
    for e in console_errors[:40]:
        print(" -", e)
    print("\n=== page errors ===")
    for e in page_errors[:40]:
        print(" -", e)
    print("\nDONE")


if __name__ == "__main__":
    sys.exit(main())
