// 防止「TSX 里用了类名但 CSS 里没有规则」——HCM 对象浏览器曾因此把
// 整个筛选开关条（.hcm-filterbar / .hcm-chip）和三个面板展开体（.hcm-panel-*）
// 渲染成无样式裸元素：标记写完了，配套 CSS 整段漏掉，肉眼在 grep 里看不出来。
//
// 本用例只做静态比对，不需要浏览器：
//   1. 扫 src/components/hcm/*.tsx 里 className 出现的 hcm-* 类名
//   2. 扫 src/styles/*.css 里出现过的 .hcm-* 选择器
//   3. 前者必须是后者的子集
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend', 'web-react');
const SRC = join(ROOT, 'src');

let failed = 0;
function check(name, condition) {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}`);
  }
}

/** 收集目录下所有匹配的层级 */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

// 1) 组件里用到的 hcm-* 类名
const tsxFiles = walk(join(SRC, 'components', 'hcm')).filter((p) => p.endsWith('.tsx'));
const used = new Map(); // class -> [files]
const classNameRe = /className=(?:"([^"]*)"|\{([^}]*)\})/g;
for (const f of tsxFiles) {
  const src = readFileSync(f, 'utf8');
  for (const m of src.matchAll(classNameRe)) {
    const raw = (m[1] ?? m[2] ?? '')
      // 模板字符串 / 条件拼接统一打散成空白，再挑 hcm- 前缀的 token
      .replace(/[`"'${}()?:;]/g, ' ');
    for (const token of raw.split(/\s+/)) {
      // 跳过模板字符串前缀片段（如 `hcm-cf-err-kind--${x}` 拆出的 hcm-cf-err-kind--）
      if (!/^hcm-[a-z0-9-]+$/.test(token)) continue;
      if (token.endsWith('--')) continue;
      if (token.endsWith('-')) continue;
      if (!used.has(token)) used.set(token, []);
      used.get(token).push(f.replace(`${SRC}/`, ''));
    }
  }
}

// 2) 样式表里定义过的 .hcm-* 选择器
const defined = new Set();
for (const f of walk(join(SRC, 'styles')).filter((p) => p.endsWith('.css'))) {
  const css = readFileSync(f, 'utf8');
  for (const m of css.matchAll(/\.(hcm-[a-z0-9-]+)/g)) defined.add(m[1]);
}

console.log(`HCM 组件 className 里的 hcm-* 类名：${used.size} 个（${tsxFiles.length} 个 tsx 文件）`);
console.log(`样式表里定义的 .hcm-* 选择器：${defined.size} 个`);

const missing = [...used.keys()].filter((c) => !defined.has(c)).sort();
check(
  `每个组件里用到的 hcm-* 类名都有 CSS 规则${missing.length ? `：缺 ${missing.join(', ')}` : ''}`,
  missing.length === 0,
);

// 反向检查单列出来只做提示（很多类是给别的组件/历史遗留用的，不算失败）
const orphans = [...defined].filter((c) => !used.has(c)).sort();
console.log(`  ℹ 已定义但 HCM 对象浏览器未使用的类：${orphans.length} 个${orphans.length ? `（${orphans.slice(0, 12).join(', ')}${orphans.length > 12 ? ' …' : ''}）` : ''}`);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
