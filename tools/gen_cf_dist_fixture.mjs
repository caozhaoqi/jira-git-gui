// -*- coding: utf-8 -*-
// 生成 CfDistribution「按函数着色」的静态 fixture HTML。
//
// 为什么用生成器而不是手写 HTML：fixture 必须和 CfDistribution.tsx 真实渲染出的
// DOM/内联样式**逐字节同构**（同样的 buildTimeBuckets / countByType / 取色 / 高度
// 归一化 / 刻度抽样），否则验证的是另一个东西。数据刻意复刻用户截图的比例
// （5 个函数、1000 条、2m 桶宽、首柱高 + 中段零散 + 右段簇、59 条疑似错误）。
//
// 运行：node tools/gen_cf_dist_fixture.mjs [输出路径，默认 /tmp/cf-dist-fixture.html]
import { writeFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && !/\.(ts|tsx|js|mjs|json)$/.test(specifier)) {
      try { return nextResolve(`${specifier}.ts`, context); } catch { /* 落回原样 */ }
    }
    return nextResolve(specifier, context);
  },
});

const WEB = new URL('../frontend/web-react/src/', import.meta.url);
const OUT = process.argv[2] || '/tmp/cf-dist-fixture.html';
const {
  buildTimeBuckets, countByType, cfTypeColorVar, fmtBucketFull, fmtBucketTick, fmtStep, pickStepMs,
} = await import(new URL('utils/cfDist.ts', WEB).href);

// ── 确定性伪随机（LCG），保证每次生成的 fixture 一致 ──
let seed = 20261016;
const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);

const T0 = new Date('2026-10-16T11:02:00').getTime();
const STEP = 2 * 60_000; // 2 分钟桶宽（和截图一致）
const N_BUCKETS = 37; // ~72 分钟
// 每个桶的「函数权重」：首桶 employee_effect 高、10 号桶附近 ConvJingcheEmp 小尖峰、
// 18 桶之后 OuterExplinerSync/ConvHuaqiEmp 成簇（复刻截图的疏密节奏）
const TYPES = ['OuterExplinerSync', 'employee_effect', 'ConvGdyyEmp', 'ConvHuaqiEmp', 'ConvJingcheEmp'];
function bucketWeights(i) {
  const w = { OuterExplinerSync: 3, employee_effect: 1, ConvGdyyEmp: 1, ConvHuaqiEmp: 1, ConvJingcheEmp: 0.5 };
  if (i < 3) { w.employee_effect = 14; w.OuterExplinerSync = 2; }
  if (i >= 9 && i <= 11) { w.ConvJingcheEmp = 3; }
  if (i >= 18) { w.OuterExplinerSync = 9; w.ConvHuaqiEmp = 3; w.employee_effect = 0.5; }
  return w;
}
const rows = [];
let errBudget = 59;
for (let i = 0; i < N_BUCKETS; i++) {
  const w = bucketWeights(i);
  const entries = TYPES.map((n) => [n, w[n]]);
  const wSum = entries.reduce((s, [, v]) => s + v, 0);
  const n = Math.max(2, Math.round((1000 / N_BUCKETS) * (0.35 + 1.6 * rnd())));
  for (let k = 0; k < n; k++) {
    let r = rnd() * wSum;
    let type = entries[0][0];
    for (const [name, v] of entries) { r -= v; if (r <= 0) { type = name; break; } }
    const isErr = errBudget > 0 && rnd() < 0.06;
    if (isErr) errBudget -= 1;
    // 桶内再抖一个 0~110s 偏移，避免同一桶的时间戳完全相同
    const ts = T0 + i * STEP + Math.floor(rnd() * 110);
    const d = new Date(ts);
    const p = (x) => String(x).padStart(2, '0');
    rows.push({
      create_time: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`,
      log_type: type,
      content: isErr ? 'sync failed: error in batch' : 'ok',
    });
  }
}

const stepMs = pickStepMs(
  (() => { let mn = Infinity, mx = -Infinity;
    for (const r of rows) { const t = new Date(r.create_time.replace(' ', 'T')).getTime(); if (t < mn) mn = t; if (t > mx) mx = t; }
    return mx - mn; })(),
  48,
);
const buckets = buildTimeBuckets(rows, stepMs);
const types = countByType(rows);
const total = rows.length;
const maxCount = buckets.reduce((m, b) => Math.max(m, b.count), 0) || 1;
const tickIdx = new Set(buckets.map((_, i) => i).filter((i) => i % Math.max(1, Math.ceil(buckets.length / 8)) === 0));
const errTotal = rows.filter((r) => /error|failed|异常|错误|失败/i.test(r.content)).length;
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const bars = buckets.map((b, i) => {
  const segs = b.segs.map((s) =>
    `<div class="cf-dist-bar-seg" style="height:${((s.count / maxCount) * 100).toFixed(3)}%;background:${cfTypeColorVar(s.name)}"></div>`,
  ).join('');
  const flag = b.err > 0 ? '<div class="cf-dist-bar-flag"></div>' : '';
  const segDesc = b.segs.map((s) => `${esc(s.name)}×${s.count}`).join(', ');
  const title = `${fmtBucketFull(b.ts, stepMs)} · ${b.count}${b.err ? ` (${b.err} err)` : ''}\n${segDesc}\n点击柱条按该时段过滤`;
  const label = tickIdx.has(i) ? `<div class="cf-dist-bar-label">${fmtBucketTick(b.ts, stepMs)}</div>` : '';
  return `<div class="cf-dist-bar${b.count ? ' clickable' : ''}" title="${esc(title)}" role="${b.count ? 'button' : ''}" tabindex="${b.count ? 0 : ''}"><div class="cf-dist-bar-inner">${segs}${flag}</div>${label}</div>`;
}).join('\n');

const typeRows = types.slice(0, 10).map((it) => {
  const pct = total ? (it.count / total) * 100 : 0;
  const color = cfTypeColorVar(it.name);
  return `<button class="cf-dist-type" title="${esc(it.name)} · ${it.count}${it.err ? ` (${it.err} err)` : ''}"><span class="cf-dist-type-name"><i class="cf-dist-type-swatch" style="background:${color}"></i>${esc(it.name)}</span><span class="cf-dist-type-track"><span class="cf-dist-type-fill" style="width:${pct.toFixed(1)}%;background:${color}"></span></span><span class="cf-dist-type-count">${it.count} · ${pct.toFixed(1)}%</span></button>`;
}).join('\n');

const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>cf-dist fixture</title>
<link rel="stylesheet" href="${WEB.href}styles/global.css">
<link rel="stylesheet" href="${WEB.href}styles/panels.css">
<link rel="stylesheet" href="${WEB.href}styles/shell.css">
<link rel="stylesheet" href="${WEB.href}styles/logviewer.css">
<link rel="stylesheet" href="${WEB.href}styles/cfdebug.css">
<link rel="stylesheet" href="${WEB.href}styles/services-config.css">
</head>
<body style="margin:0;padding:24px;background:var(--bg);">
<div class="card-soft cf-dist-card">
  <div class="panel-header">
    <button class="clash-collapser" aria-expanded="true"><i class="cfd-caret">▾</i><h2 class="section-title">云函数日志分布</h2></button>
    <span class="cf-dist-meta">基于 ${total} 条已加载日志 · 桶宽 ${fmtStep(stepMs)}<span class="cf-dist-meta-err"> · ${errTotal} 条疑似错误</span></span>
  </div>
  <div class="cf-dist-body">
    <div class="cf-dist-section">
      <div class="cf-dist-subtitle"><span>🕒 时间分布</span><span class="cf-dist-hint">柱内色块=函数占比 · 点击柱条按该时段过滤</span></div>
      <div class="cf-dist-bars">
${bars}
      </div>
    </div>
    <div class="cf-dist-section">
      <div class="cf-dist-subtitle"><span>🏷 类型分布</span><span class="cf-dist-hint">点击类型可过滤，再点取消</span></div>
      <div class="cf-dist-types">
${typeRows}
      </div>
    </div>
  </div>
</div>
</body></html>
`;
writeFileSync(OUT, html);
console.log(`fixture → ${OUT}`);
console.log(`rows=${total} buckets=${buckets.length} step=${fmtStep(stepMs)} err=${errTotal}`);
console.log('types:', types.map((x) => `${x.name}=${x.count}`).join(' '));
console.log('colors:', types.map((x) => `${x.name}=${cfTypeColorVar(x.name)}`).join(' '));
