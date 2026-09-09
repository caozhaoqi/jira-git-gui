# -*- coding: utf-8 -*-
"""截 Clash（网络接口）tab 的图，用于验证宽度适配。"""
import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")
VW = int(os.environ.get("VW", "1280"))

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": VW, "height": 900})
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_selector('button[data-tab="clash"]', timeout=20000)
    pg.click('button[data-tab="clash"]')
    pg.wait_for_timeout(4000)
    out = f"/tmp/shot_clash_{VW}.png"
    pg.screenshot(path=out)
    print("saved", out)
    # 顺便量一下内容实际占多宽
    try:
        w = pg.evaluate("() => { const e=document.querySelector('.clash-panel'); return e ? e.getBoundingClientRect().width : -1; }")
        print("clash-panel width =", w, " / viewport", VW)
    except Exception as e:
        print("measure failed", e)
    b.close()
