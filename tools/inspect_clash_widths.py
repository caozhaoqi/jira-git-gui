import os, json
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    for w in [1235, 1280, 1366, 1440, 1600, 1920]:
        pg = b.new_page(viewport={"width": w, "height": 920})
        pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
        pg.wait_for_timeout(800)
        pg.click('button[data-tab="clash"]')
        pg.wait_for_timeout(1800)
        info = pg.evaluate("""() => {
            const get = (sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return Math.round(r.width);
            };
            return {
                win: window.innerWidth,
                sidebar: get('.sidebar'),
                workspace: get('.workspace'),
                wsBody: get('.workspace-body'),
                tabPane: get('.tab-pane:not(.tab-pane--hidden)'),
                clashPanel: get('.clash-panel'),
                clashCard: get('.clash-card'),
                ifaceList: get('.clash-iface-list'),
            };
        }""")
        # 计算右空白
        right_blank = w - (info['clashPanel'] or 0) - (info.get('sidebar') or 0) - 36
        print(f"win={w:>4}  side={info['sidebar']:>3}  wsBody={info['wsBody']:>4}  "
              f"panel={info['clashPanel']:>4}  card={info['clashCard']:>4}  "
              f"right_blank≈{right_blank:>4}")
        pg.close()
    b.close()
