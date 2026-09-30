#!/usr/bin/env node
/**
 * i18n 一致性校验（防回归）。
 *
 * 背景：这个项目曾出现三类真实缺陷——
 *   1) 代码里 t('x.y') 的键在三份字典里都不存在 → 界面直接渲染裸 key
 *      （最严重的一次是 K8s「应用 YAML」的不可逆操作确认框）；
 *   2) 某个键只在部分语言里存在（如 zh 缺 cfdebug.watchPlaceholder）；
 *   3) 插值占位符写错（如 en 写成 '{{n}' 少一个右花括号 → 永远不替换）。
 *
 * 本脚本检查：
 *   A. 三份字典的叶子键集合是否一致（缺键 / 多余键）
 *   B. 源码中出现的 t('字面量') 是否在全部三份字典中存在
 *   C. 每个键的 {{var}} 占位符是否成对，且跨语言是否一致
 *
 * 用法：node scripts/check-i18n.mjs        （在 frontend/web-react 下执行）
 * 退出码：0 = 通过，1 = 有问题
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');

const { zh } = await import(join(SRC, 'i18n/zh.ts'));
const { en } = await import(join(SRC, 'i18n/en.ts'));
const { ja } = await import(join(SRC, 'i18n/ja.ts'));

const LOCALES = { zh, en, ja };

/** 展平嵌套字典 → Set('a.b.c') */
function leafKeys(dict, prefix = '') {
  const out = new Set();
  for (const [k, v] of Object.entries(dict)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object') {
      for (const x of leafKeys(v, path)) out.add(x);
    } else {
      out.add(path);
    }
  }
  return out;
}

/** 取某键的值字符串 */
function valueAt(dict, key) {
  let node = dict;
  for (const p of key.split('.')) {
    if (node == null || typeof node !== 'object') return undefined;
    node = node[p];
  }
  return typeof node === 'string' ? node : undefined;
}

// ---------- A. 三份字典键集合比对 ----------
const keys = Object.fromEntries(Object.entries(LOCALES).map(([k, d]) => [k, leafKeys(d)]));
const problems = [];
const warnings = [];

const allKeys = new Set([...keys.zh, ...keys.en, ...keys.ja]);
for (const key of [...allKeys].sort()) {
  const missing = Object.entries(keys).filter(([, s]) => !s.has(key)).map(([l]) => l);
  if (missing.length) {
    problems.push(`字典缺键：${key} 缺少 [${missing.join(', ')}]`);
  }
}

// ---------- C. 插值占位符 ----------
const varRe = /\{\{(\w+)\}\}/g;
const rawBrace = /(?<!\{)\{(?!\{)|\}(?!\})/;
for (const key of [...allKeys].sort()) {
  const perLocale = {};
  for (const [loc, dict] of Object.entries(LOCALES)) {
    const s = valueAt(dict, key);
    if (s === undefined) continue;
    perLocale[loc] = [...s.matchAll(varRe)].map((m) => m[1]).sort().join(',');
    // 单独出现的花括号（如 '{{n})' 少一个右括号）视为笔误
    const cleaned = s.replace(/\{\{\w+\}\}/g, '');
    if (rawBrace.test(cleaned)) {
      problems.push(`占位符括号不配对：[${loc}] ${key} = ${JSON.stringify(s)}`);
    }
  }
  const sets = new Set(Object.values(perLocale));
  if (sets.size > 1) {
    problems.push(`占位符跨语言不一致：${key} → ${JSON.stringify(perLocale)}`);
  }
}

// ---------- B. 源码 t('字面量') 引用检查 ----------
const files = [];
(function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(ts|tsx)$/.test(p) && !p.includes('/i18n/')) files.push(p);
  }
})(SRC);

const usedKeys = new Map(); // key -> [files]
const tCall = /\bt\(\s*'([^'\\]+)'/g;
for (const f of files) {
  const src = readFileSync(f, 'utf8');
  for (const m of src.matchAll(tCall)) {
    const key = m[1];
    if (!usedKeys.has(key)) usedKeys.set(key, []);
    usedKeys.get(key).push(f.replace(`${ROOT}/`, ''));
  }
}
for (const [key, where] of [...usedKeys].sort()) {
  const missing = Object.entries(keys).filter(([, s]) => !s.has(key)).map(([l]) => l);
  if (missing.length) {
    problems.push(`代码引用了不存在的键：${key}（缺 [${missing.join(', ')}]）← ${where[0]}`);
  }
}

// ---------- 未被任何代码引用的键（提示，不算错误） ----------
const unused = [...keys.zh].filter((k) => !usedKeys.has(k));
if (unused.length) {
  warnings.push(`${unused.length} 个键未被源码以字面量方式引用（可能是动态拼接或历史残留）`);
}

// ---------- 输出 ----------
const n = (s) => s.size;
console.log('字典叶子键数：' + Object.entries(keys).map(([l, s]) => `${l}=${n(s)}`).join('  '));
console.log(`源码 t('...') 字面量引用：${usedKeys.size} 个键 / ${files.length} 个文件`);

if (warnings.length) {
  console.log('\n⚠ 提示：');
  for (const w of warnings) console.log('  - ' + w);
}

if (problems.length) {
  console.error(`\n✗ 发现 ${problems.length} 个 i18n 问题：`);
  for (const p of problems) console.error('  - ' + p);
  process.exit(1);
}
console.log('\n✓ i18n 校验通过（三份字典键一致、代码引用全部存在、占位符配对且跨语言一致）');
