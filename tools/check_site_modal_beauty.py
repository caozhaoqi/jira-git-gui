# -*- coding: utf-8 -*-
"""美化管理站点弹窗后，肉眼验证关键样式 & 文案是否到位（不做硬断言）。"""
import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1480, "height": 920})
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1200)

    # 切到 Kibana 标签
    pg.wait_for_selector('button[data-tab="kibana"]', timeout=20000)
    pg.click('button[data-tab="kibana"]')
    pg.wait_for_selector(".kibana-panel", timeout=20000)
    pg.wait_for_timeout(2000)

    # 打开站点管理弹窗
    try:
        pg.click('button:has-text("管理站点")', timeout=8000)
    except Exception:
        # 兜底：直接点含 'manageSites' 渲染文案的按钮
        pg.click('button:has-text("Manage")', timeout=8000)
    pg.wait_for_selector(".kb-site-modal", timeout=15000)
    pg.wait_for_timeout(1200)
    pg.screenshot(path="/tmp/shot_site_modal.png")
    print("saved /tmp/shot_site_modal.png (列表+表单态)")

    # 点击列表中第一个站点的编辑按钮（icon ✎），确认表单态完整渲染
    try:
        pg.click('.kb-site-op-btn[title*="编辑"], .kb-site-op-btn[title*="Edit"]', timeout=4000)
    except Exception:
        pg.click('.kb-site-op-btn:not(.danger)', timeout=4000)
    pg.wait_for_selector('.kb-site-form', timeout=8000)
    pg.wait_for_timeout(800)
    pg.screenshot(path="/tmp/shot_site_form.png")
    print("saved /tmp/shot_site_form.png (表单编辑态)")

    # 检测关键元素是否都在
    checks = [
        ('.kb-modal-title-icon', 'header icon'),
        ('.kb-site-section-title', 'section title'),
        ('.kb-site-form-grid', 'form grid'),
        ('.kb-site-form-foot', 'footer'),
        ('.kb-cur-tag', 'active badge (optional)'),
    ]
    for sel, name in checks:
        n = pg.locator(sel).count()
        print(f"  {name:25s} {sel:30s} = {n}")

    # 文案检查：没有 'common.edit' 这种 raw key 残留
    html = pg.content()
    bad = [w for w in ['common.edit', 'common.delete', 'common.save', 'kibana.site.'] if w in html]
    print(f"  raw i18n keys 残留: {bad if bad else '无 ✓'}")

    b.close()
print("done")