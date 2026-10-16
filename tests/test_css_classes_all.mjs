// 防止「TSX 里用了类名但 CSS 里没有规则」——这类 bug 曾经把整个筛选开关条
// 渲染成无样式裸元素：标记写完了，配套 CSS 整段漏掉，肉眼在 grep 里看不出来。
//
// tests/test_hcm_css_classes.mjs 只覆盖 components/hcm/；本用例把同样的静态比对
// 推广到全部 src/components/**/*.tsx，避免 hcm 之外的目录重蹈覆辙
// （曾发现 UnifiedDiagnosisPanel 的 diag-summary/diag-* 类整套没有 CSS）。
//
// 只做静态比对，不需要浏览器：
//   1. 扫所有 tsx 里 className 出现的类名
//   2. 扫 src/styles/*.css 里出现过的 .类名 选择器
//   3. 前者必须是后者的子集
//
// 白名单：下面这些前缀/类名来自全局工具类或第三方（xterm / highlight.js）库，
// 由那些库自己的样式表提供，不由本项目 styles/ 负责。
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend', 'web-react');
const SRC = join(ROOT, 'src');

/** 收集目录下所有匹配的层级 */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

/** 全局工具类 / 第三方库的类名，不由本项目 styles/ 定义 */
const EXTERNAL_PREFIXES = [
  'xterm',        // @xterm/xterm
  'hljs',         // highlight.js
  'react-',       // 第三方 React 组件库预留
  'toast-',       // 由 Toast.tsx 自身注入的样式（见 Toast.tsx 内 <style>）
];

function isExternalClass(token) {
  return EXTERNAL_PREFIXES.some((p) => token.startsWith(p));
}

let failed = 0;
function check(name, condition, detail = '') {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}${detail ? `：${detail}` : ''}`);
  }
}

// 1) 组件里用到的类名
const tsxFiles = walk(join(SRC, 'components')).filter((p) => p.endsWith('.tsx'));
const used = new Map(); // class -> [files]

// JS 表达式里出现的非类名标识符/字面量（三元条件、布尔、关键字等）。
// 提取策略：只认「字符串字面量」的引号内容，表达式里的裸标识符一律不取——
// 否则 `cond ? 'a' : 'b'` 会把 cond 也当成类名，`x && 'y'` 会把 x 带上。
const NOT_CLASS = new Set([
  'true', 'false', 'null', 'undefined', 'is', 'and', 'or', 'if', 'else', 'then',
  'in', 'of', 'new', 'return', 'class', 'this',
]);
function classesFromExpr(expr) {
  const out = [];
  for (const m of expr.matchAll(/['"`]([^'"`]*)['"`]/g)) {
    out.push(...m[1].split(/\s+/));
  }
  return out;
}

// ⚠️ 历史 bug（2026-10-16 修复）：className 有两种写法，早先只走 classesFromExpr，
// 于是 `className="cfc-wrap"` 这种最常见的静态写法被整段漏掉 —— 外层正则已经把
// 双引号剥掉，剩下裸串 `cfc-wrap`，里面没有引号，classesFromExpr 自然提取不到。
// 后果是守卫只看到 82/1022 个类名（覆盖率 8%），"缺 CSS" 根本报不出来，
// 与它存在的意义完全相反。
//   1) className="a b c"        → 引号已被剥掉，整串就是类名列表，直接按空白切
//   2) className={...表达式...} → 只取引号内的片段（裸标识符是变量/条件，不是类名）
function classesFromClassNameAttr(quoted, expr) {
  if (quoted !== undefined) return quoted.split(/\s+/);
  return classesFromExpr(expr ?? '');
}

// TS 字符串字面量联合类型 / as const 数组里的「值」（如 type Tab = 'list' | 'info'）。
// 这些词会作为三元比较对象出现在 className 表达式里（tab === 'fields' ? … : …），
// 但它们是枚举值而不是类名，必须排除，否则每个 tab 名都会被误报成「缺 CSS」。
// 覆盖三种写法：
//   1) type X = 'a' | 'b'
//   2) useState<'a' | 'b'>（内联泛型，无 type 声明）
//   3) ['a', 'b'] as const
const literalValues = new Set();
const unionRe = /type\s+\w+\s*=\s*([^;]+);/g;
const genericUnionRe = /useState<([^>]+)>/g;
const asConstRe = /\[\s*((?:'[^']*'\s*(?:\|\s*)?)+)\s*\]\s+as\s+const/g;
for (const f of tsxFiles) {
  const src = readFileSync(f, 'utf8');
  for (const m of src.matchAll(unionRe)) {
    for (const v of m[1].matchAll(/'([^']+)'/g)) literalValues.add(v[1]);
  }
  for (const m of src.matchAll(genericUnionRe)) {
    for (const v of m[1].matchAll(/'([^']+)'/g)) literalValues.add(v[1]);
  }
  for (const m of src.matchAll(asConstRe)) {
    for (const v of m[1].matchAll(/'([^']+)'/g)) literalValues.add(v[1]);
  }
}

// 已知的「非类名」字符串值：它们作为比较对象出现在 className 表达式里，
// 写法上无法与类名字符串区分（如 e.status === 'whitespace_only' ? 'a' : 'b'）。
// 每条都注明来源，新增条目前先确认它真的是值而不是漏写的样式。
const KNOWN_NON_CLASS_VALUES = new Set([
  'kind',            // K8sEvents.tsx：q.set('kind', …) 查询参数名
  'whitespace_only', // DiffPanel.tsx：e.status 的枚举值（仅换行符差异）
]);

// 「确实是类名、但有意不写 CSS」的例外。这类不是漏写，加规则反而会改变现有版式，
// 所以显式登记并写清理由 —— 登记本身也让它们重新变得可见（不再是静默跳过）。
// 新增条目前必须逐条确认理由成立，不接受「先加进来让它绿」。
const KNOWN_UNSTYLED = new Map([
  ['unified-diag-panel', 'UnifiedDiagnosisPanel.tsx：外层容器，padding/height/overflow 全走行内 style，类名只作语义标记'],
  ['diag-summary', 'UnifiedDiagnosisPanel.tsx：纯包裹层，内部元素全部行内 style'],
  ['diag-cf-detail', 'UnifiedDiagnosisPanel.tsx：纯包裹层，内部元素全部行内 style'],
  ['diag-k8s-detail', 'UnifiedDiagnosisPanel.tsx：纯包裹层，内部元素全部行内 style'],
  ['card-body', 'DiffPanel.tsx：折叠区包裹层，子元素 .diff-cfg-card 自带 padding/border/背景，父层再加会双层内边距'],
  ['kb-dl-foot', 'KibanaLogStream.tsx：与 .modal-foot 同时使用，样式全部由 .modal-foot 提供，此名仅作扩展挂点'],
]);

const classNameRe = /className=(?:"([^"]*)"|\{([^}]*)\})/g;
for (const f of tsxFiles) {
  const src = readFileSync(f, 'utf8');
  for (const m of src.matchAll(classNameRe)) {
    for (const token of classesFromClassNameAttr(m[1], m[2])) {
      if (!/^[a-z][a-z0-9_-]*$/.test(token)) continue;
      if (token.length < 2) continue;
      // 模板字符串前缀片段（如 hcm-cf-err-kind--${x} 拆出的 hcm-cf-err-kind--）
      if (token.endsWith('-')) continue;
      if (NOT_CLASS.has(token)) continue;
      if (literalValues.has(token)) continue;
      if (KNOWN_NON_CLASS_VALUES.has(token)) continue;
      if (KNOWN_UNSTYLED.has(token)) continue;
      if (isExternalClass(token)) continue;
      if (!used.has(token)) used.set(token, []);
      used.get(token).push(f.replace(`${SRC}/`, ''));
    }
  }
}

// 2) 样式表里定义过的 .类名 选择器
const defined = new Set();
for (const f of walk(join(SRC, 'styles')).filter((p) => p.endsWith('.css'))) {
  const css = readFileSync(f, 'utf8');
  for (const m of css.matchAll(/\.([a-zA-Z_][\w-]*)/g)) defined.add(m[1]);
}
// 运行时注入样式的组件（Toast / LogViewer 等）里自带的类名也算已定义
const inlineStyled = new Set();
for (const f of tsxFiles) {
  const src = readFileSync(f, 'utf8');
  // <style{...}>...</style> 里出现的 .cls 选择器
  for (const block of src.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
    for (const m of block[1].matchAll(/\.([a-zA-Z_][\w-]*)/g)) inlineStyled.add(m[1]);
  }
}
for (const c of inlineStyled) defined.add(c);

console.log(`组件 className 里的类名：${used.size} 个（${tsxFiles.length} 个 tsx 文件）`);
console.log(`样式表里定义的类名：${defined.size} 个`);

const missing = [...used.keys()].filter((c) => !defined.has(c)).sort();
check(
  `每个组件里用到的类名都有 CSS 规则${missing.length ? `：缺 ${missing.slice(0, 30).join(', ')}${missing.length > 30 ? ` …等 ${missing.length} 个` : ''}` : ''}`,
  missing.length === 0,
);

// 守卫自身的健全性下限。提取器一旦又退化成「只认引号内片段」（见上方历史 bug），
// used.size 会从 ~1000 掉到 ~80，而 missing 反而更少 —— 是「静默放行」而不是报错。
// 有了这条下限，那种退化会立刻变红，不会再悄悄发生一次。
check(
  `类名提取覆盖量正常（used=${used.size}，下限 500）`,
  used.size >= 500,
);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
