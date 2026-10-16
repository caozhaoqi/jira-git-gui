// 防止「弹窗变体宽度被基础类压掉」——曾导致 Kibana 日志上下文弹窗（双击日志行打开）
// 永远只有 560px 宽，作者写的 900px 从未生效，用户反馈「窗口太小了」。
//
// 原因不是笔误，是**级联顺序**：
//   main.tsx 的 CSS 引入顺序是 global → panels → shell，
//   shell.css 里有一条 `.modal { width: min(560px, 94vw) }`，
//   它和 panels.css 里的 `.kb-ctx-modal { width: 900px }` **特异度相同**（都是单类），
//   而 shell.css 加载更晚 —— 同特异度下后者胜，变体宽度被静默吃掉。
//
// 所以：在 shell.css **之前**加载的样式表里，任何单类 `*-modal` 选择器都不能设 width，
// 必须写成 `.modal.xxx-modal`（两个类）来抬高特异度。
//
// 只查 width：它是决定「窗口太小」的属性，且一旦失效肉眼不一定看得出；
// 其他属性（如 overflow）即使同值重复也无副作用，不做断言以免误报。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend', 'web-react');
const STYLES = join(ROOT, 'src', 'styles');

// main.tsx: global → panels → shell → logviewer → cfdebug → services-config
// shell.css 之前加载的只有这两个，它们的单类变体都会被 `.modal` 压掉。
const BEFORE_SHELL = ['global.css', 'panels.css'];

// 单类 `*-modal`（不含 `.modal` 前缀、无后代/伪类/组合符）
const SINGLE_VARIANT = /^\.([a-z0-9][\w-]*-modal)$/;

let checked = 0;
const offenders = [];

for (const file of BEFORE_SHELL) {
  // 必须先剥掉注释：注释里可能带 `{` / `}`，而且「注释 + 选择器」会被下面的
  // `([^{}]+)\{` 一起captured，导致选择器不以 `.` 开头、锚点 `^\.` 失配而漏报
  // （第一版就栽在这里：守卫对真实违规无感，形同虚设）。
  const css = readFileSync(join(STYLES, file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  // 逐个规则取「选择器 { 声明块 }」（本项目样式表无嵌套，够用）
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = m[1].trim();
    const body = m[2];
    if (!SINGLE_VARIANT.test(selector)) continue;
    // 只认真的声明了 width 的（排除 `max-width` 这类）
    if (!/(^|;)\s*width\s*:/.test(body)) continue;
    offenders.push(`${file} → ${selector} { width: … }`);
  }
  checked++;
}

assert.equal(checked, BEFORE_SHELL.length, '要先读到 shell.css 之前的样式表');
assert.deepEqual(
  offenders,
  [],
  '以下弹窗变体只写了单类，宽度会被 shell.css 的 `.modal{width:min(560px,94vw)}` ' +
    '按同特异度压掉（改写成 `.modal.xxx-modal`）：\n  ' + offenders.join('\n  '),
);

// 反向确认：Kibana 日志上下文弹窗确实用了双类写法且宽度不再是被压掉的 560px
const panels = readFileSync(join(STYLES, 'panels.css'), 'utf8');
assert.match(
  panels,
  /\.modal\.kb-ctx-modal\s*\{[^}]*width:\s*min\(\s*(\d+)px/,
  'Kibana 上下文弹窗要用 `.modal.kb-ctx-modal` 并显式给宽度',
);
const ctxWidth = Number(panels.match(/\.modal\.kb-ctx-modal\s*\{[^}]*width:\s*min\(\s*(\d+)px/)[1]);
assert.ok(ctxWidth >= 900, `Kibana 上下文弹窗宽度 ${ctxWidth}px 偏小（应 ≥900）`);
assert.match(
  panels,
  /\.modal-body\.kb-ctx-body\s*\{[^}]*flex:\s*1/,
  '正文要 flex:1 撑满弹窗高度，否则再高也被 max-height 截住',
);
assert.match(
  panels,
  /\.modal-body\.kb-ctx-body\s*\{[^}]*max-height:\s*none/,
  '正文要覆盖 global.css 的 .modal-body{max-height:60vh}，否则高度仍被截',
);
// 正文元素的实际类是 `modal-body kb-ctx-body`，没有 `modal` 类：
// 写成 `.modal.kb-ctx-body` 会静默失配（曾因此让正文停在 540px）。
assert.doesNotMatch(
  panels,
  /\.modal\.kb-ctx-body/,
  '正文是 `modal-body kb-ctx-body`，选择器要写 `.modal-body.kb-ctx-body`',
);

console.log(`Modal cascade guard: passed（${checked} 个前置样式表，无单类变体设 width）`);
