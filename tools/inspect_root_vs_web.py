import os, json
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    for path in ["/", "/web/"]:
        for w in [1280, 1600]:
            pg = b.new_page(viewport={"width": w, "height": 860})
            pg.goto(f"{BASE}{path}", wait_until="networkidle", timeout=60000)
            pg.wait_for_timeout(800)
            try:
                pg.click('button[data-tab="clash"]', timeout=8000)
            except Exception as e:
                print(f"{path} win={w}: 点不到 clash tab -> {e}")
                pg.close()
                continue
            pg.wait_for_timeout(1800)
            info = pg.evaluate("""() => {
                const g = (s) => { const e=document.querySelector(s); if(!e) return null;
                    const r=e.getBoundingClientRect(); return {w:Math.round(r.width), right:Math.round(r.right)}; };
                return {win: window.innerWidth, url: location.pathname,
                        shell:g('.app-shell'), body:g('.app-body'), ws:g('.workspace'),
                        wsBody:g('.workspace-body'), pane:g('.tab-pane:not(.tab-pane--hidden)'),
                        panel:g('.clash-panel'), card:g('.clash-card'),
                        ipInput:g('.clash-ip-input'), ifaceList:g('.clash-iface-list')};
            }""")
            panel = info['panel']
            blank = (info['win'] - panel['right']) if panel else None
            print(f"path={path:<6} win={w:<5} shell={info['shell']['w']:<5} "
                  f"wsBody={info['wsBody']['w']:<5} pane={info['pane']['w'] if info['pane'] else None:<5} "
                  f"panel={panel['w'] if panel else None:<5} "
                  f"ipInput={info['ipInput']['w'] if info['ipInput'] else None:<5} "
                  f"=> RIGHT_BLANK={blank}")
            pg.close()
    b.close()
