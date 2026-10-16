// -*- coding: utf-8 -*-
// 「Log distribution」按函数着色的回归（utils/cfDist.ts）—— 锁死三件事：
//   ① 同一函数名恒定同一颜色（哈希取色，跨视图/跨过滤都稳定，不随排名漂移）
//   ② 堆叠柱分段：segs 计数之和 == 桶总数、按数量降序、时段外/坏时间戳不丢行
//   ③ 类型分布口径（countByType）不被改动
// 运行：node tests/test_cf_dist.mjs
// （Node 22.18+ 默认支持 type stripping，可直接 import .ts）
//
// ⚠️ cfDist.ts 内部 `import ... from './logFields'` 是 Vite 风格的无扩展名写法，
// Node ESM 解析不了；用 registerHooks 给相对导入补 .ts 后缀即可离线跑。
import { registerHooks } from 'node:module';
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && !/\.(ts|tsx|js|mjs|json)$/.test(specifier)) {
      try { return nextResolve(`${specifier}.ts`, context); } catch { /* 落回原样 */ }
    }
    return nextResolve(specifier, context);
  },
});

const {
  buildTimeBuckets, countByType, cfTypeColorVar, cfNameHash, CF_TYPE_PALETTE_SIZE,
} = await import('../frontend/web-react/src/utils/cfDist.ts');

let failed = 0;
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    failed++;
    console.log(`  ✗ ${name} ${extra}`);
  }
}

// 固定时间轴：分钟 0/1/2 ...，三个函数 + 一个错误行
const T0 = new Date('2026-10-16T11:00:00').getTime();
const row = (minute, type, content = 'ok') => ({
  create_time: new Date(T0 + minute * 60_000).toISOString().replace('T', ' ').slice(0, 19),
  log_type: type,
  content,
});
const rows = [
  row(0, 'OuterExplinerSync'), row(0, 'OuterExplinerSync'), row(0, 'employee_effect'),
  row(1, 'ConvGdyyEmp', 'boom: error occurred'),
  row(2, 'ConvGdyyEmp'), row(2, 'ConvHuaqiEmp'), row(2, 'ConvHuaqiEmp'),
  row(2, 'ConvJingcheEmp'),
  { create_time: 'not-a-time', log_type: 'employee_effect', content: 'x' }, // 坏时间戳：不计入桶
  row(3, '(未知时也有 seg)'), // 会落到最后一个桶
];

console.log('cfTypeColorVar：颜色只依赖函数名');
check('同一名字恒定同色', cfTypeColorVar('ConvGdyyEmp') === cfTypeColorVar('ConvGdyyEmp'));
check('空名字与 (未知) 同色', cfTypeColorVar('') === cfTypeColorVar('(未知)'));
check('色值必须是 var(--cfd-cN) 形式', /^var\(--cfd-c(10|[1-9])\)$/.test(cfTypeColorVar('x')));
check('N 在色板容量内', (() => {
  for (const n of ['a', 'ConvGdyyEmp', 'employee_effect', 'OuterExplinerSync', 'x'.repeat(50)]) {
    const m = /--cfd-c(\d+)/.exec(cfTypeColorVar(n));
    if (!m) return false;
    const idx = Number(m[1]);
    if (!(idx >= 1 && idx <= CF_TYPE_PALETTE_SIZE)) return false;
  }
  return true;
})());
check('调色板 10 个变量名互不相同', (() => {
  const s = new Set();
  for (let i = 0; i < 40; i++) s.add(cfTypeColorVar('fn-' + i));
  return s.size === CF_TYPE_PALETTE_SIZE;
})(), `distinct=${new Set(Array.from({ length: 40 }, (_, i) => cfTypeColorVar('fn-' + i))).size}`);
check('哈希非负且确定', cfNameHash('abc') === cfNameHash('abc') && cfNameHash('abc') >= 0);

console.log('buildTimeBuckets：堆叠分段');
const buckets = buildTimeBuckets(rows, 60_000);
check('桶数 = 时间跨度覆盖（0..3 分钟 → 4 桶）', buckets.length === 4, `len=${buckets.length}`);
check('坏时间戳不计入任何桶', buckets.reduce((s, b) => s + b.count, 0) === rows.length - 1);
const b1 = buckets[1];
check('第 2 桶总数 1（ConvGdyyEmp）', b1.count === 1 && b1.segs.length === 1 && b1.segs[0].name === 'ConvGdyyEmp');
check('第 2 桶 err=1', b1.err === 1);
const b2 = buckets[2];
check('第 3 桶总数 4', b2.count === 4, `count=${b2.count}`);
check('segs 之和 == 桶总数', b2.segs.reduce((s, x) => s + x.count, 0) === b2.count);
check('segs 按数量降序', b2.segs[0].count >= b2.segs[1].count && b2.segs[0].name === 'ConvHuaqiEmp');
check('segs 内计数正确（ConvHuaqiEmp×2 / ConvGdyyEmp×1 / ConvJingcheEmp×1）',
  b2.segs.find((x) => x.name === 'ConvHuaqiEmp')?.count === 2 &&
  b2.segs.find((x) => x.name === 'ConvGdyyEmp')?.count === 1 &&
  b2.segs.find((x) => x.name === 'ConvJingcheEmp')?.count === 1);
check('零计数桶也有 segs 空数组', buckets[0].segs.every((x) => x.count > 0) && Array.isArray(buckets[0].segs));
check('步宽非法/无有效时间 → 空数组', buildTimeBuckets(rows, 0).length === 0 &&
  buildTimeBuckets([{ create_time: 'bad', log_type: 'x' }], 60_000).length === 0);

console.log('countByType：口径保持');
const types = countByType(rows);
// 本题账里有四个 2（ConvGdyyEmp / ConvHuaqiEmp / OuterExplinerSync / employee_effect），
// 同数量按名称排序 → 榜首是 ConvGdyyEmp；同时校验整体单调不增。
check('数量单调不增', types.every((x, i) => i === 0 || types[i - 1].count >= x.count));
check('榜首 ConvGdyyEmp×2（四家并列 2 时按名称）', types[0].name === 'ConvGdyyEmp' && types[0].count === 2);
check('OuterExplinerSync×2', types.find((x) => x.name === 'OuterExplinerSync')?.count === 2);
check('坏时间戳的行仍计入类型', types.find((x) => x.name === 'employee_effect')?.count === 2);
check('err 计入类型项', types.find((x) => x.name === 'ConvGdyyEmp')?.err === 1);

console.log(failed === 0 ? '\nALL PASS' : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
