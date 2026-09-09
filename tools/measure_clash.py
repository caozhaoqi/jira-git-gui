# -*- coding: utf-8 -*-
"""逐层量 Clash 页各容器宽度，定位「右侧留白」到底丢在哪一层。"""
import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")
VW = int(os.environ.get("VW", "1280"))

JS = """() => {
  const sel = ['.app-shell', '.app-main', '.sidebar', '.clash-panel',
               '.clash-card', '.clash-iface-list', '.clash-iface'];
  const out = {viewport: window.innerWidth, body: document.body.getBoundingClientRect().width};
  for (const s of sel) {
    const e = document.querySelector(s);
    if (!e) { out[s] = null; continue; }
    const r = e.getBoundingClientRect();
    out[s] = {w: Math.round(r.width), left: Math.round(r.left), right: Math.round(r.right)};
  }
  // 卡片列数
  const cards = document.querySelectorAll('.clash-iface');
  out.cardCount = cards.length;
  return out;
}"""

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": VW, "height": 900})
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_selector('button[data-tab="clash"]', timeout=20000)
    pg.click('button[data-tab="clash"]')
    pg.wait_for_timeout(3500)
    data = pg.evaluate(JS)
    for k, v in data.items():
        print(f"{k:22} {v}")
    b.close()
