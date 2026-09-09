import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1480, "height": 920})
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1000)
    pg.click('button[data-tab="clash"]')
    pg.wait_for_timeout(2500)
    pg.screenshot(path="/tmp/shot_clash.png")
    print("saved /tmp/shot_clash.png")
    info = pg.evaluate("""() => {
        const get = (sel) => {
            const el = document.querySelector(sel);
            if (!el) return {found: false};
            const r = el.getBoundingClientRect();
            return {found: true, w: Math.round(r.width), x: Math.round(r.x), right: Math.round(r.right)};
        };
        return {
            window: window.innerWidth,
            app: get('.app-shell'),
            appBody: get('.app-body'),
            appMain: get('.app-main'),
            tabInner: get('.tab-inner'),
            workspaceBody: get('.workspace-body'),
            clashPanel: get('.clash-panel'),
            clashCard: get('.clash-card'),
            ifaceList: get('.clash-iface-list'),
        };
    }""")
    import json
    print(json.dumps(info, indent=2, ensure_ascii=False))
    b.close()
