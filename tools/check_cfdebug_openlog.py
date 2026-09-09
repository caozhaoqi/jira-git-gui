import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8799").rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1480, "height": 920})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"{BASE}/web/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(800)
    pg.click('button[data-tab="cfdebug"]')
    pg.wait_for_timeout(2500)
    n_fn = pg.eval_on_selector_all(".cfdebug-fn", "els => els.length")
    n_btn = pg.eval_on_selector_all(".cfdebug-fn-openlog", "els => els.length")
    print(f"云函数列表项 .cfdebug-fn = {n_fn}")
    print(f"日志按钮 .cfdebug-fn-openlog = {n_btn}")
    if n_btn:
        title = pg.eval_on_selector(".cfdebug-fn-openlog", "el => el.title")
        print("首个按钮 title =", title)
    pg.screenshot(path="/tmp/shot_cfdebug.png")
    print("截图 /tmp/shot_cfdebug.png")
    print("pageerror:", errs[:3] if errs else "无")
    b.close()
