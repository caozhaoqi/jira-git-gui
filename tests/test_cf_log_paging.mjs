// -*- coding: utf-8 -*-
// CF 日志结果表「本地分页 / 匹配序号」回归（utils/cfLogPaging.ts）—— 锁死四件事：
//   ① localPaged=false（服务端分页语义）时原样返回全量，行为与改造前一致
//   ② 实时刷新累积集（merged=true）必须走本地分页：行数超过 pageSize 后只渲染一页，
//      不再把累积行堆到 5000 条上限全量渲染
//   ③ pageStart 跨页唯一且连续 —— 行号 / React key / 展开态都以它为基准，
//      拿「页内下标」当 key 会在一翻到第 2 页时就与第 1 页撞 key
//   ④ 匹配序号跨页连续：rowOrd 从「当前页首行之前的匹配数」起算，
//      否则翻页后「第 N 处匹配」的高亮整体错位到别的行
// 另附 CfPanel.tsx 的静态接线不变量（分页门槛用 localPaged、isFull 不被 merged 污染）。
// 运行：node tests/test_cf_log_paging.mjs
//
// ⚠️ cfLogPaging.ts 内部 `import ... from './logFields'` 是 Vite 风格的无扩展名写法，
// Node ESM 解析不了；用 registerHooks 给相对导入补 .ts 后缀即可离线跑。
import { registerHooks } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && !/\.(ts|tsx|js|mjs|json)$/.test(specifier)) {
      try { return nextResolve(`${specifier}.ts`, context); } catch { /* 落回原样 */ }
    }
    return nextResolve(specifier, context);
  },
});

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', 'frontend', 'web-react', 'src');
const {
  sliceLocalPage, rowMatchCount, countMatchesBefore, findMatches,
} = await import(join(SRC, 'utils', 'cfLogPaging.ts'));

let failed = 0;
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    failed++;
    console.log(`  ✗ ${name} ${extra}`);
  }
}

const row = (i, type = 'fn_a', content = 'ok') => ({
  id: i,
  log_type: type,
  create_time: `2026-10-20 10:00:${String(i % 60).padStart(2, '0')}`,
  content: `${content}-${i}`,
});

// --------------------------------------------------------------------------- //
//  ① 服务端分页语义：localPaged=false 时不得改动任何行为
// --------------------------------------------------------------------------- //
{
  const rows = Array.from({ length: 50 }, (_, i) => row(i));
  const r = sliceLocalPage(rows, 200, false, 1);
  check('localPaged=false → 原样返回全量', r.display === rows);
  check('localPaged=false → pageStart=0 / totalPages=1', r.pageStart === 0 && r.totalPages === 1);
  // 用户停在本地第 3 页但不分页：页码原样带回，不得被强改
  check('localPaged=false → localPage 不被收敛', sliceLocalPage(rows, 200, false, 3).localPage === 3);
}

// --------------------------------------------------------------------------- //
//  ② 实时刷新累积集必须走本地分页（本问题的核心）
// --------------------------------------------------------------------------- //
{
  // 平台 total=786，流只推了一页 100 行起步；累积到 250 行时：
  // isFull = 786>0 && 250>=786 = false；merged=true → localPaged = true
  const total = 786;
  const accumulated = Array.from({ length: 250 }, (_, i) => row(i));
  const isFull = total > 0 && accumulated.length >= total;
  const localPaged = isFull || true; // merged
  check('累积 250/786 行时 isFull 仍为 false（不得谎报全量）', isFull === false);
  const r = sliceLocalPage(accumulated, 200, localPaged, 1);
  check('累积集超过 pageSize 后只渲染一页', r.display.length === 200, `got ${r.display.length}`);
  check('总页数 = ceil(250/200) = 2', r.totalPages === 2, `got ${r.totalPages}`);
  check('首页取最新 200 行', r.display[0].id === 0 && r.display[199].id === 199);

  // 翻到第 2 页：剩余 50 行，pageStart=200
  const r2 = sliceLocalPage(accumulated, 200, localPaged, 2);
  check('第 2 页 pageStart=200', r2.pageStart === 200, `got ${r2.pageStart}`);
  check('第 2 页 50 行', r2.display.length === 50, `got ${r2.display.length}`);

  // 未超 page_size 时不分页（100 行 / page_size 200）：全量展示，无分页器
  const small = Array.from({ length: 100 }, (_, i) => row(i));
  const rs = sliceLocalPage(small, 200, true, 1);
  check('未超 page_size 不分页', rs.display.length === 100 && rs.totalPages === 1);

  // localPage 越界收敛到末页（用户停在第 9 页，行被 5000 上限裁掉后只剩 2 页）
  const rc = sliceLocalPage(accumulated, 200, localPaged, 9);
  check('localPage 越界收敛到末页', rc.localPage === 2 && rc.pageStart === 200, `got ${rc.localPage}`);
  // pageSize 脏数据兜底
  check('page_size=0 兜底为 1', sliceLocalPage([1, 2, 3], 0, true, 1).totalPages === 3);
}

// --------------------------------------------------------------------------- //
//  ③ pageStart 跨页唯一且连续（React key / 行号 / 展开态基准）
// --------------------------------------------------------------------------- //
{
  const rows = Array.from({ length: 450 }, (_, i) => row(i));
  const pageSize = 100;
  const totalPages = Math.ceil(rows.length / pageSize);
  const seen = new Set();
  let dup = 0;
  for (let p = 1; p <= totalPages; p++) {
    const { display, pageStart } = sliceLocalPage(rows, pageSize, true, p);
    for (let i = 0; i < display.length; i++) {
      const globalIdx = pageStart + i;
      if (seen.has(globalIdx)) dup++;
      seen.add(globalIdx);
    }
  }
  check('跨页 globalIdx 无重复', dup === 0, `dup=${dup}`);
  check('跨页 globalIdx 覆盖 0..N-1', seen.size === rows.length, `${seen.size}/${rows.length}`);
  // 旧写法 result.rows.length - view.rows.length + i 会在第 2 页与第 1 页撞 key，
  // 这里固化「每一页的 globalIdx 都落在本页区间内」
  const p2 = sliceLocalPage(rows, pageSize, true, 2);
  check('第 2 页 globalIdx 区间为 [100,200)',
    p2.pageStart + 0 === 100 && p2.pageStart + p2.display.length - 1 === 199);
}

// --------------------------------------------------------------------------- //
//  ④ 匹配序号跨页连续
// --------------------------------------------------------------------------- //
{
  const needle = 'needle';
  // 每行内容都是 needle-i；log_type 故意不含 needle，保证「每行恰好 1 处匹配」
  const rows = Array.from({ length: 450 }, (_, i) => ({
    id: i,
    log_type: 'fn_a',
    create_time: `2026-10-20 10:00:00`,
    content: `${needle}-${i}`,
  }));
  const pageSize = 100;
  // 每行 1 处匹配（只在 content）→ 全量 450 处
  const total = rows.reduce((n, r) => n + rowMatchCount(r, needle, false, ''), 0);
  check('每行 1 处匹配，全量 450', total === 450, `got ${total}`);

  for (const p of [1, 2, 3, 4, 5]) {
    const { pageStart } = sliceLocalPage(rows, pageSize, true, p);
    const before = countMatchesBefore(rows, pageStart, needle, false, '');
    check(`第 ${p} 页 rowOrd 起点 = ${(p - 1) * 100}`, before === (p - 1) * 100, `got ${before}`);
  }
  // 大小写敏感：needle 全小写，log_type 里也是小写 → 仍命中
  check('大小写不敏感计数',
    rowMatchCount(rows[0], 'NEEDLE', false, '') === rowMatchCount(rows[0], 'needle', false, ''));
  check('大小写敏感计数为 0',
    rowMatchCount(rows[0], 'NEEDLE', true, '') === 0);
  // 多字段叠加：同时命中 content + log_type
  const multi = { id: 1, log_type: 'needle', create_time: '2026-01-01 00:00:00', content: 'needle' };
  check('content + log_type 两处都计', rowMatchCount(multi, 'needle', false, '') === 2,
    `got ${rowMatchCount(multi, 'needle', false, '')}`);
  check('空 q 不计匹配', rowMatchCount(multi, '', false, '') === 0);
  check('countMatchesBefore(startIdx<=0)=0', countMatchesBefore(rows, 0, needle, false, '') === 0);
  check('countMatchesBefore(空 q)=0', countMatchesBefore(rows, 100, '', false, '') === 0);
  check('countMatchesBefore 越界 startIdx 被夹住',
    countMatchesBefore(rows, 99999, needle, false, '') === rows.length);
}

// --------------------------------------------------------------------------- //
//  findMatches 口径（与高亮渲染一致）
// --------------------------------------------------------------------------- //
{
  check('findMatches 找齐不重叠区间',
    JSON.stringify(findMatches('aXbXc', 'X', true)) === JSON.stringify([[1, 2], [3, 4]]));
  check('findMatches 空 q 返回空', findMatches('abc', '', false).length === 0);
  check('findMatches 连续重叠不重复计数',
    findMatches('aaaa', 'aa', true).length === 2);
}

// --------------------------------------------------------------------------- //
//  CfPanel.tsx 静态接线不变量（防回归）
// --------------------------------------------------------------------------- //
{
  const src = readFileSync(join(SRC, 'components', 'CfPanel.tsx'), 'utf8');

  // 分页门槛必须是 localPaged，不能退回 isFull（isFull 还管「已加载全部」徽标）
  check('分页切片走 sliceLocalPage(localPaged)',
    /sliceLocalPage\(filtered, pageSize, localPaged,/.test(src));
  check('localPaged = isFull || merged',
    /const localPaged = isFull \|\| !!result\.merged;/.test(src));
  // isFull 本身不能被 merged 污染，否则流开着就谎报「全量已加载」
  check('isFull 定义未被 merged 污染',
    /const isFull = \(result\.total \|\| 0\) > 0 && result\.rows\.length >= result\.total;/.test(src));
  // SSE 合并必须打 merged 标记，否则累积集永远走不了本地分页
  check('SSE 合并置 merged: true', /merged: true,/.test(src));
  // 合并不得再把用户踢回第 1 页
  check('SSE 合并不强制 localPage: 1', !/rows: \[\.\.\.fresh, \.\.\.r\.rows\][\s\S]{0,120}localPage: 1/.test(src));
  // 分页器与行号/key 都以 pageStart 为基准
  check('分页器门槛用 view.localPaged', /view\.localPaged && view\.totalPages > 1/.test(src));
  check('行号用 view.pageStart', /idx=\{view\.pageStart \+ i \+ 1\}/.test(src));
  check('React key 用 view.pageStart + i', /const globalIdx = view\.pageStart \+ i;/.test(src));
  check('rowOrd 从 view.matchOrdBefore 起算', /let rowOrd = view\.matchOrdBefore;/.test(src));
  // 「已加载全部」徽标仍只认 isFull
  check('fullLoaded 徽标仍只认 isFull', /view\.isFull && <span>\{t\('cf\.fullLoaded'\)\}<\/span>/.test(src));
}

// --------------------------------------------------------------------------- //
console.log(failed === 0 ? '\n全部通过' : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
