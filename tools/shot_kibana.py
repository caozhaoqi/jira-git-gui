# -*- coding: utf-8 -*-
"""给 Kibana 面板截几张图，便于肉眼评估 UI 是否混乱（不做事后断言）。"""
import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1480, "height": 920})
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1500)
    pg.screenshot(path="/tmp/shot_full.png")
    print("saved /tmp/shot_full.png")

    pg.wait_for_selector('button[data-tab="kibana"]', timeout=20000)
    pg.click('button[data-tab="kibana"]')
    pg.wait_for_selector(".kibana-panel", timeout=20000)
    pg.wait_for_timeout(4000)
    pg.screenshot(path="/tmp/shot_kibana_explorer.png")
    print("saved /tmp/shot_kibana_explorer.png")

    try:
        pg.click('.kibana-subtab:has-text("检索")', timeout=8000)
        pg.wait_for_timeout(4000)
        pg.screenshot(path="/tmp/shot_kibana_discover.png")
        print("saved /tmp/shot_kibana_discover.png")
    except Exception as e:
        print("discover 切换失败:", e)

    # 也截一张站点管理弹窗
    try:
        pg.click('button:has-text("管理站点")', timeout=8000)
        pg.wait_for_timeout(1500)
        pg.screenshot(path="/tmp/shot_kibana_sites.png")
        print("saved /tmp/shot_kibana_sites.png")
    except Exception as e:
        print("站点弹窗失败:", e)

    b.close()
print("done")
