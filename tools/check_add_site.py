# -*- coding: utf-8 -*-
"""回归测试：点击「+ 新增」必须弹出空白表单（修复之前无反馈）。"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8799".rstrip("/")
ok = True

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1480, "height": 920})
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1200)
    pg.click('button[data-tab="kibana"]', timeout=20000)
    pg.wait_for_selector(".kibana-panel", timeout=20000)
    pg.wait_for_timeout(1500)

    # 打开站点管理弹窗
    try:
        pg.click('button:has-text("管理站点")', timeout=8000)
    except Exception:
        pg.click('button:has-text("Manage")', timeout=8000)
    pg.wait_for_selector(".kb-site-modal", timeout=15000)

    # 点击「+ 新增」前，表单应不存在
    before = pg.locator(".kb-site-form").count()
    print(f"[before] .kb-site-form count = {before}")

    # 点新增
    pg.click('button:has-text("新增")', timeout=8000)
    pg.wait_for_timeout(600)

    # 新增后，表单应出现，且 name 输入框为空（空白模板）
    try:
        pg.wait_for_selector(".kb-site-form", timeout=4000)
        after = pg.locator(".kb-site-form").count()
        name_val = pg.input_value('input[value=""], .kb-site-form input.input-sm')
        empty_name = pg.locator(".kb-site-form input.input-sm").first.input_value()
        print(f"[after ] .kb-site-form count = {after}")
        print(f"[after ] first input value (name) = {repr(empty_name)}")
        if after == 0:
            ok = False
            print("FAIL: 点击新增后表单未出现")
        elif empty_name != "":
            ok = False
            print("FAIL: 表单出现但 name 不为空（不是空白模板）")
        else:
            print("PASS: 点击新增 → 空白表单正常弹出")
        pg.screenshot(path="/tmp/shot_add_blank_form.png")
        print("saved /tmp/shot_add_blank_form.png")
    except Exception as e:
        ok = False
        print("FAIL: 等待 .kb-site-form 超时 ->", e)

    b.close()

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)