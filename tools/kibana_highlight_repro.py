# -*- coding: utf-8 -*-
"""复现「Kibana 搜索没高亮显示」：真实浏览器里跑一遍高亮链路。

场景：
  1. 打开 /web/，切 Kibana tab
  2. 站点选 jt-test（本地后端能真拉到日志的那个）
  3. 检索视角等日志行出现
  4. 在实时查找框（.kb-search-input）输入日志里一定存在的词
  5. 数 <mark class="kb-hl"> 的个数 —— 用户说这里没有高亮

另外一并核对：
  - 主筛选条的「关键词」查询后，返回的日志行里是否也高亮（用户很可能指的是这个）
"""
import os
import sys

BASE = os.environ.get("BASE", "http://127.0.0.1:8787").rstrip("/")
SITE = os.environ.get("SITE", "jt-test")

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

    print(f"== Kibana 高亮复现 @ {BASE}/web/ site={SITE} ==")
    page_errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
        page.wait_for_selector('button[data-tab="kibana"]', timeout=20000)
        page.click('button[data-tab="kibana"]')
        page.wait_for_selector(".kibana-panel", timeout=20000)

        # 站点切到能出数据的那个
        page.select_option(".kibana-topbar select.sel", SITE)

        # 检索视角（有日志流 + 实时查找条）
        page.click('.kibana-subtab:has-text("检索")')
        page.wait_for_selector(".kb-log .kb-line", state="attached", timeout=40000)
        n_lines = page.locator(".kb-log .kb-line").count()
        check("日志行已渲染", n_lines > 0, f"lines={n_lines}")

        # 从第一行 msg 文本里取一段真实存在的词，保证一定能命中
        first_msg = page.locator(".kb-log .kb-line .kb-msg").first.inner_text()
        needle = "GET" if "GET" in first_msg else first_msg.strip().split(" ")[0][:6]
        print(f"  第一行 msg: {first_msg.strip()[:80]!r}  取 needle={needle!r}")

        # ---- 场景 A：实时查找框（.kb-search-input） ----
        page.locator(".kb-search-input:visible").fill(needle)
        page.wait_for_timeout(800)
        marks = page.locator(".kb-log mark.kb-hl").count()
        cnt = page.locator(".kb-search-count:visible").inner_text()
        active = page.locator(".kb-log mark.kb-hl.active").count()
        print(f"  [实时查找] needle={needle!r} → mark={marks} active={active} count={cnt!r}")
        check("实时查找框输入后出现 <mark class=kb-hl>", marks > 0, f"marks={marks}")

        # 高亮是否真的可见（背景色不是 transparent / 未命中）
        bg = page.locator(".kb-log mark.kb-hl").first.evaluate(
            "el => getComputedStyle(el).backgroundColor")
        check("高亮 <mark> 有可见背景色", bg not in ("rgba(0, 0, 0, 0)", "transparent"), f"bg={bg}")

        # ---- 场景 B：主筛选条的「关键词」查询 ----
        page.locator(".kb-search-input:visible").fill("")
        page.wait_for_timeout(300)
        kw = page.locator(".kb-keyword:visible").first
        check("主筛选条有关键词输入框", kw.count() if hasattr(kw, "count") else kw is not None)
        kw.fill(needle)
        # 触发查询：关键词回车
        page.keyboard.press("Enter")
        page.wait_for_timeout(8000)
        page.wait_for_selector(".kb-log .kb-line", state="attached", timeout=30000)
        marks2 = page.locator(".kb-log mark.kb-hl").count()
        lines2 = page.locator(".kb-log .kb-line").count()
        print(f"  [关键词查询] needle={needle!r} → lines={lines2} mark={marks2}")
        check("主筛选关键词查询后，命中行有关键词高亮", marks2 > 0,
              f"lines={lines2} marks={marks2}")

        page.screenshot(path="/tmp/kibana_highlight.png", full_page=False)
        print("  📸 截图: /tmp/kibana_highlight.png")
        browser.close()

    check("无未捕获 JS 异常", not page_errors, "; ".join(page_errors[:3]))
    print(f"\n== 结果: PASS={PASS} FAIL={FAIL} ==")
    if FAIL:
        print("失败项:", FAILS)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
