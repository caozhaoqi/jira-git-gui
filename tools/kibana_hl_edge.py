# -*- coding: utf-8 -*-
"""Kibana「关键词高亮」边界回归 —— 防住最危险的一类回归。

背景：主筛选关键词现在会驱动高亮（后端对普通词走 match_phrase → 命中行逐字包含关键词）。
但后端对含 `*`/`?`/`:`/AND/OR/NOT 的词走 query_string（通配符/字段限定/布尔），
返回的行**不一定逐字包含**关键词。若把这套词也拿去「过滤」，就会把用户刚查出来的行
全部过滤光 —— 一行日志都看不到。

判定方式：先用 `/api/kibana/logs` 直接问后端拿「真实应显示行数」，再和 DOM 里的
行数比对。这样能把两种情况区分开：
  - 后端本来就返回 0 行（合理空结果）
  - 后端返回了 N 行、前端却过滤成 0/少于 N（回归！）
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8787").rstrip("/")
SITE = os.environ.get("SITE", "jt-test")
RANGE = os.environ.get("RANGE", "now-24h")

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


def api_rows(keyword):
    """直接问后端：该关键词下应返回多少行（与面板同样的请求参数）。"""
    body = json.dumps({"site": SITE, "start": RANGE, "size": 300, "keyword": keyword}).encode()
    req = urllib.request.Request(f"{BASE}/api/kibana/logs", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    return len(d.get("rows") or []), d.get("ok") is not False


def search_with_keyword(page, kw):
    box = page.get_by_role("textbox", name="关键词（空格分词，*? 走 query_string）")
    box.fill(kw)
    page.keyboard.press("Enter")
    page.wait_for_timeout(7000)
    page.wait_for_timeout(500)


def visible_stats(page):
    idx = page.evaluate("""() => {
      const divs = [...document.querySelectorAll('.kibana-subpane > div')];
      return divs.findIndex(d => d.style.display !== 'none');
    }""")
    pane = page.locator(".kibana-subpane > div").nth(idx)
    return {
        "lines": pane.locator(".kb-log .kb-line").count(),
        "marks": pane.locator(".kb-log mark.kb-hl").count(),
        "src": pane.locator(".kb-hl-src").count(),
        "empty": pane.locator(".kb-log .empty-hint").count(),
        "count_text": (pane.locator(".kb-search-count").inner_text()
                       if pane.locator(".kb-search-count").count() else ""),
    }


def main():
    from playwright.sync_api import sync_playwright

    print(f"== Kibana 关键词高亮边界回归 @ {BASE}/web/ site={SITE} range={RANGE} ==")
    page_errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        page.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
        page.wait_for_selector('button[data-tab="kibana"]', timeout=20000)
        page.click('button[data-tab="kibana"]')
        page.select_option(".kibana-topbar select.sel", SITE)
        # 时间范围对齐到脚本用的同一个（否则后端基线数对不上）
        page.click('.kibana-subtab:has-text("检索")')
        page.wait_for_selector(".kb-log .kb-line", state="attached", timeout=40000)

        cases = [
            # (关键词, 期望高亮?, 说明)
            ("hcm", True, "普通词 → match_phrase，行文逐字包含"),
            ("log:hcm", False, "字段限定 → query_string，行文不含 'log:hcm'"),
            ("hcm*", False, "通配符 → query_string"),
            ("POST /api", True, "多词短语 → match_phrase"),
        ]
        for kw, expect_hl, why in cases:
            want_rows, ok = api_rows(kw)
            search_with_keyword(page, kw)
            s = visible_stats(page)
            print(f"  [{kw!r}] 后端应显示={want_rows} DOM行数={s['lines']} "
                  f"marks={s['marks']} src={s['src']} count={s['count_text']!r}  ({why})")
            check(f"{kw!r}：DOM 行数与后端一致（不多不少）", s["lines"] == want_rows,
                  f"want={want_rows} got={s['lines']}")
            if want_rows > 0:
                check(f"{kw!r}：未把行过滤光", s["lines"] > 0, f"lines={s['lines']}")
                if expect_hl:
                    check(f"{kw!r}：出现高亮", s["marks"] > 0, f"marks={s['marks']}")
                    check(f"{kw!r}：显示「关键词高亮」来源提示", s["src"] == 1, f"src={s['src']}")
                else:
                    check(f"{kw!r}：逐字匹配不成立 → 不高亮且无来源提示",
                          s["marks"] == 0 and s["src"] == 0,
                          f"marks={s['marks']} src={s['src']}")

        # 查找框优先于关键词
        page.locator(".kb-search-input:visible").fill("GET")
        page.wait_for_timeout(500)
        s = visible_stats(page)
        check("查找框输入后来源提示消失（以查找词为准）", s["src"] == 0, f"src={s['src']}")
        page.locator(".kb-search-input:visible").fill("")
        page.wait_for_timeout(500)
        s = visible_stats(page)
        check("清空查找框后恢复关键词高亮", s["src"] == 1, f"src={s['src']}")

        # 跟随仍在：关键词高亮期间 activeMatch 必须保持 0（不抢滚底）
        am = page.evaluate("""() => {
          const pos = [...document.querySelectorAll('.kb-match-pos')]
            .find(e => e.offsetParent !== null);
          return pos ? pos.textContent.trim() : '';
        }""")
        print(f"  关键词高亮期间匹配定位文本: {am!r}")
        check("关键词高亮期间未自动定位到第一处（follow 不让位）", am.startswith("0"),
              f"pos={am!r}")

        page.screenshot(path="/tmp/kibana_hl_edge.png", full_page=False)
        print("  📸 截图: /tmp/kibana_hl_edge.png")
        browser.close()

    check("无未捕获 JS 异常", not page_errors, "; ".join(page_errors[:3]))
    print(f"\n== 结果: PASS={PASS} FAIL={FAIL} ==")
    if FAIL:
        print("失败项:", FAILS)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
