# -*- coding: utf-8 -*-
"""弹窗尺寸验收（真机 CSS，不需真后端数据）。

背景：panels.css 里的弹窗变体（`.kb-ctx-modal` 等）此前只写单类，被 shell.css
后加载的 `.modal{width:min(560px,94vw)}` 按同特异度压掉 —— Kibana 日志上下文弹窗
（双击日志行打开）永远只有 560px，用户反馈「窗口太小了」。

这里在真实页面上注入与组件**完全一致**的 class 结构，量渲染后的实际尺寸。
（对应静态守卫 tests/test_modal_cascade.mjs；静态查「选择器写法」，这里查「最终结果」。）

用法：先起后端（默认 8787），再 `./venv/bin/python tools/check_modal_sizes.py`
"""
import os
import sys

BASE = os.environ.get("BASE", "http://127.0.0.1:8787").rstrip("/")
SHOT_DIR = os.environ.get("SHOT_DIR", "/tmp")

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


# 与 KibanaLogStream.tsx 的 <div className="modal-body kb-ctx-body"> 一致
CTX_ROWS = "".join(
    f'<div class="kb-line wrap{" anchor" if i == 20 else ""}">'
    f'<span class="kb-ts">19:0{i % 10}:1{i % 10}.000</span>'
    f'<span class="kb-lv lv-{"error" if i % 7 == 0 else "info"}">INFO </span>'
    f'<span class="kb-msg">2026-10-15 19:0{i % 10} 容器 app-worker-{i % 3} 处理请求 '
    f'POST /api/v1/hcm/model/list 耗时 {120 + i}ms 结果 ok 这是用于验证长行换行的填充文本</span>'
    f"</div>"
    for i in range(60)
)

CASES = [
    {
        "name": "Kibana 日志上下文弹窗（双击日志行）",
        "selector": ".kb-ctx-modal",
        "expect_w": 1160,
        "min_w": 900,
        "html": '<div class="modal kb-ctx-modal">'
                '<div class="modal-header"><span id="kb-ctx-title">上下文 · app-worker-0</span>'
                '<button class="btn btn-ghost btn-sm">✕</button></div>'
                f'<div class="modal-body kb-ctx-body">{CTX_ROWS}</div></div>',
    },
    {
        "name": "合并冲突面板",
        "selector": ".conflict-modal",
        "expect_w": 960,
        "min_w": 900,
        "html": '<div class="modal conflict-modal">'
                '<div class="modal-header"><h3>合并冲突</h3></div>'
                '<div class="modal-body"><div class="conflict-list">'
                '<div class="conflict-item">x</div></div></div></div>',
    },
    {
        "name": "Kibana 下载对话框",
        "selector": ".kb-dl-modal",
        "expect_w": 460,
        "min_w": 420,
        "html": '<div class="modal kb-dl-modal">'
                '<div class="modal-header"><span>⬇ 下载</span></div>'
                '<div class="modal-body kb-dl-body">'
                '<label class="kb-dl-field"><span class="kb-dl-label">时间范围</span>'
                '<select class="sel"><option>近 1 小时</option></select></label>'
                '<label class="kb-dl-field"><span class="kb-dl-label">容器</span>'
                '<select class="sel"><option>全部容器</option></select></label>'
                "</div></div>",
    },
    {
        # 这个弹窗**故意**铺满：它只在独立窗口（?view=kibana-sites）里渲染，
        # `.modal.kb-sites-window { width: 100% }` 压过 `.modal.kb-site-modal` 的 860px
        # 是有意为之（panels.css 有注释：页内小弹窗看不全编辑表单，故开独立窗口铺满）。
        # 这里断言 100%，是为了防止有人把它「修」回 860px 而把独立窗口改坏。
        "name": "Kibana 站点管理（独立窗口专用，故意铺满）",
        "selector": ".kb-site-modal",
        "expect_w": 1440,
        "min_w": 1440,
        "html": '<div class="modal kb-site-modal kb-sites-window">'
                '<div class="modal-header"><span class="kb-modal-title-icon">⚙</span>'
                '<span class="kb-modal-title-text">站点管理</span></div>'
                '<div class="modal-body">x</div></div>',
    },
    {
        "name": "K8s 资源描述弹窗 / 文件编辑弹窗（modal-lg）",
        "selector": ".modal-lg",
        "expect_w": 860,
        "min_w": 800,
        "html": '<div class="modal modal-lg">'
                '<div class="modal-header"><h3>Describe</h3></div>'
                '<div class="modal-body"><pre class="k8s-desc">x</pre></div></div>',
    },
    {
        "name": "K8s 文件编辑弹窗·全屏模式（modal-fullscreen 仍须压过 modal-lg）",
        "selector": ".modal-fullscreen",
        "expect_w": 1440,
        "min_w": 1440,
        "html": '<div class="modal modal-lg modal-fullscreen">'
                '<div class="modal-header"><h3>编辑文件</h3></div>'
                '<div class="modal-body"><textarea class="k8s-file-edit-area"></textarea></div>'
                '<div class="modal-footer">x</div></div>',
    },
]

# ⚠️ 用 offsetWidth/offsetHeight 而不是 getBoundingClientRect：
# .modal 挂着 `animation: modal-in`（from: scale(0.97)），注入后立刻量会读到缩放中的
# 尺寸（实测偏小 3%，1125 vs 1160），offset* 是布局尺寸、不受 transform 影响。
MEASURE = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const body = el.querySelector('.modal-body');
  return {
    width: el.offsetWidth,
    height: el.offsetHeight,
    cssWidth: getComputedStyle(el).width,
    bodyMaxH: body ? getComputedStyle(body).maxHeight : '',
    bodyScrollW: body ? body.scrollWidth : 0,
    bodyClientW: body ? body.clientWidth : 0,
  };
}"""

INJECT = """(html) => {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = html;
  document.body.appendChild(mask);
}"""


def main():
    from playwright.sync_api import sync_playwright

    print(f"== 弹窗尺寸验收 @ {BASE}/web/ ==")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/web/", wait_until="domcontentloaded")
        page.wait_for_timeout(400)

        for c in CASES:
            print(f"\n[{c['name']}]")
            page.evaluate(INJECT, c["html"])
            # .modal 挂着 modal-in 动画（from: opacity 0 / scale 0.97），不等它跑完
            # 截图会拍到「半透明几乎看不见」的弹窗，量出来的 rect 也会偏小。
            page.wait_for_timeout(450)
            m = page.evaluate(MEASURE, c["selector"])
            check("弹窗已渲染", m is not None)
            if not m:
                continue
            check(f"宽度 = {c['expect_w']}px（作者设定值生效）",
                  abs(m["width"] - c["expect_w"]) <= 4,
                  f"实测 {m['width']}px（css width={m['cssWidth']}）")
            check("不再是被压掉的 560px", m["width"] != 560, f"实测 {m['width']}px")
            check(f"宽度 ≥ {c['min_w']}px", m["width"] >= c["min_w"])
            # 清理，避免影响下一个用例的取景
            page.evaluate("() => document.querySelectorAll('.modal-mask').forEach(e => e.remove())")

        # 上下文弹窗单独细看：高度要撑起来、长行要换行不横向溢出
        print("\n[上下文弹窗：高度与换行]")
        page.evaluate(INJECT, CASES[0]["html"])
        page.wait_for_timeout(450)  # 等 modal-in 动画结束再截图/量尺寸
        m = page.evaluate(MEASURE, ".kb-ctx-modal")
        check("正文 max-height 已被覆盖为 none", m["bodyMaxH"] == "none",
              f"实测 {m['bodyMaxH']}（若为 540px 说明 .modal-body 的 60vh 还在生效）")
        check("弹窗高度撑到 ≥ 80vh", m["height"] >= 720, f"实测 {m['height']}px（视口 900）")
        check("长日志行换行、无横向溢出",
              m["bodyScrollW"] <= m["bodyClientW"] + 1,
              f"scrollW={m['bodyScrollW']} clientW={m['bodyClientW']}")
        page.screenshot(path=f"{SHOT_DIR}/modal_kb_ctx_light.png")

        # 深色下也要能看
        page.evaluate("document.documentElement.classList.add('dark')")
        page.evaluate("document.body.classList.add('dark')")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{SHOT_DIR}/modal_kb_ctx_dark.png")
        fg = page.evaluate("() => getComputedStyle(document.querySelector('.kb-ctx-modal')).color")
        bg = page.evaluate(
            "() => getComputedStyle(document.querySelector('.kb-ctx-modal')).backgroundColor")
        check("深色下弹窗可读（前景≠背景）", fg != bg, f"fg={fg} bg={bg}")

        check("无 JS 运行时报错", not errors, "; ".join(errors[:3]))
        browser.close()

    print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
    if FAILS:
        print("失败项：" + ", ".join(FAILS))
    print(f"截图：{SHOT_DIR}/modal_kb_ctx_light.png, modal_kb_ctx_dark.png")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
