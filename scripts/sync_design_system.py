#!/usr/bin/env python3
"""重新生成 design-system.html —— 把 src/styles/*.css 的最新内容同步进内联 <style>。

背景：design-system.html 是「样式总览 + 肉眼验收」交付物，按 main.tsx 的 CSS 加载顺序
内联全部面板样式。手工同步容易漏（改完 global.css 忘了重生成，页面里的演示就和线上不一致）。

本脚本按「分段标记」做替换，演示 HTML / 自身布局样式（.ds-*）一律不动：
    1. 在 HTML 里按 `/* ===== X.css (inlined) ===== */` 标记定位每个分段
    2. 用 src/styles/X.css 的全文替换分段内容
    3. 标记缺失或 CSS 文件缺失则报错退出（不做静默失败）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "web-react"
STYLES = FRONTEND / "src" / "styles"
TARGET = FRONTEND / "design-system.html"

# 与 main.tsx 的 import 顺序一致 —— 决定内联顺序，也就是样式的层叠次序
ORDER = [
    "global.css",
    "panels.css",
    "shell.css",
    "logviewer.css",
    "cfdebug.css",
    "services-config.css",
]

# 总览页自身布局（.ds-*）之前的守卫注释 —— 最后一个分段到此为止
OWN_LAYOUT_GUARD = "/* ===== 总览页自身布局"


def main() -> int:
    html = TARGET.read_text(encoding="utf-8")
    replaced = 0

    for name in ORDER:
        css_path = STYLES / name
        if not css_path.exists():
            print(f"✗ 缺少样式文件：{css_path}", file=sys.stderr)
            return 1

        marker = f"/* ===== {name} (inlined) ===== */"
        start = html.find(marker)
        if start == -1:
            print(f"✗ design-system.html 里找不到分段标记：{marker}", file=sys.stderr)
            return 1

        body_start = start + len(marker)

        # 下一段的起点：优先看后续文件的分段标记，最后一个分段则看自身布局守卫
        next_pos = None
        for other in ORDER[ORDER.index(name) + 1:]:
            p = html.find(f"/* ===== {other} (inlined) ===== */", body_start)
            if p != -1:
                next_pos = p
                break
        if next_pos is None:
            p = html.find(OWN_LAYOUT_GUARD, body_start)
            if p != -1:
                next_pos = p
        if next_pos is None:
            print(f"✗ 分段 {name} 找不到结束边界（既无下一段标记也无自身布局守卫）", file=sys.stderr)
            return 1

        css = css_path.read_text(encoding="utf-8").strip("\n")
        html = html[:body_start] + "\n" + css + "\n\n" + html[next_pos:]
        replaced += 1
        print(f"  ✓ 内联 {name}（{len(css.splitlines())} 行）")

    TARGET.write_text(html, encoding="utf-8")
    print(f"\n已重新生成 {TARGET.relative_to(ROOT)}（同步 {replaced} 段 CSS）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
