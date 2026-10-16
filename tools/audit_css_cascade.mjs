// 全站 CSS 级联审计：找出「同一元素上，先写的类被后加载文件里同特异度的类静默压掉」的声明。
//
// 为什么需要它：main.tsx 的引入顺序是 global → panels → shell → logviewer → cfdebug
// → services-config。后加载的同特异度规则会赢，于是 panels.css 里给变体类写的属性
// （`.kb-ctx-modal { width: 900px }`）会被 shell.css 的基类（`.modal { width: min(560px,94vw) }`）
// 吃掉 —— 作者写的值从未生效，表现为「窗口太小」。这类 bug 静态看单文件完全正常，
// 只有把「元素实际带了哪些类」和「文件加载顺序」放一起才看得出来。
//
// 判据（只报高信号项）：
//   1. 从 TSX 的 className 字面量里取出「同一元素上的类组合」G；
//   2. 取所有纯类复合选择器（`.a` / `.a.b`）且选择器类集 ⊆ G 的无条件规则（@media 内不算，
//      响应式覆盖是合理设计）；
//   3. 同一属性上，若「输给赢家」的声明与赢家**特异度相同**却因**文件更早**而落败
//      → 该声明是死代码（Category A，就是「窗口太小」的根因）；
//      若同特异度、同文件、只是写得更靠后 → 同文件内的覆盖（Category B，多为有意分层）。
//   4. 值相同的不报（无副作用）。
//
// 用法：node tools/audit_css_cascade.mjs [--all]
//   --all  连 Category B（同文件内覆盖）也一并列出
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const WEB = join(ROOT, 'frontend', 'web-react');
const STYLES = join(WEB, 'src', 'styles');

// 与 main.tsx 的 import 顺序严格一致 —— 这个顺序就是判据的一部分
const LOAD_ORDER = [
  'global.css',
  'panels.css',
  'shell.css',
  'logviewer.css',
  'cfdebug.css',
  'services-config.css',
];

const SHOW_ALL = process.argv.includes('--all');

// ---------------------------------------------------------------- 极简 CSS 解析
function parseRules(css) {
  const out = [];
  const n = css.length;
  let i = 0;
  let prelude = '';
  const atStack = [];
  while (i < n) {
    const ch = css[i];
    if (ch === '{') {
      const head = prelude.trim();
      prelude = '';
      if (head.startsWith('@')) {
        atStack.push(head);
        i++;
        continue;
      }
      let j = i + 1;
      let d = 1;
      let body = '';
      while (j < n && d > 0) {
        if (css[j] === '{') d++;
        else if (css[j] === '}') {
          d--;
          if (d === 0) break;
        }
        body += css[j];
        j++;
      }
      out.push({ selector: head, body, at: atStack.length ? atStack[atStack.length - 1] : null });
      i = j + 1;
      continue;
    }
    if (ch === '}') {
      atStack.pop();
      prelude = '';
      i++;
      continue;
    }
    prelude += ch;
    i++;
  }
  return out;
}

function parseDecls(body) {
  const decls = [];
  for (const part of body.split(';')) {
    const k = part.indexOf(':');
    if (k < 0) continue;
    const prop = part.slice(0, k).trim().toLowerCase();
    const value = part.slice(k + 1).trim().replace(/\s+/g, ' ');
    if (prop && value) decls.push({ prop, value });
  }
  return decls;
}

// 只认纯类复合选择器：`.a`、`.a.b`（含后代/伪类/元素的跳过——它们不作用于同一元素）
const COMPOUND = /^(?:\.[a-zA-Z_][\w-]*)+$/;

const rules = []; // { classes:[], spec, fileIndex, order, decls, raw }
LOAD_ORDER.forEach((file, fileIndex) => {
  const raw = readFileSync(join(STYLES, file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  let order = 0;
  for (const r of parseRules(raw)) {
    if (r.at) continue; // 跳过 @media 等条件规则
    for (const sel of r.selector.split(',')) {
      const s = sel.trim();
      if (!COMPOUND.test(s)) continue;
      const classes = s.slice(1).split('.');
      rules.push({
        classes,
        spec: classes.length,
        fileIndex,
        order: order++,
        decls: parseDecls(r.body),
        raw: `${file}: ${s}`,
      });
    }
  }
});

// ---------------------------------------------------------------- 从 TSX 取「同元素类组合」
function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, acc);
    else if (p.endsWith('.tsx')) acc.push(p);
  }
  return acc;
}

const groups = new Map(); // key: sorted classes -> Set(来源文件)
const addGroup = (text, file) => {
  const classes = text
    .replace(/\$\{[^}]*\}/g, ' ') // 去掉模板插值
    .split(/\s+/)
    .filter((c) => /^[a-zA-Z_][\w-]*$/.test(c));
  if (classes.length < 2) return; // 单类不可能出现「类之间互相压」的问题
  const key = [...classes].sort().join(' ');
  if (!groups.has(key)) groups.set(key, new Set());
  groups.get(key).add(relative(WEB, file));
};

for (const file of walk(join(WEB, 'src'))) {
  const src = readFileSync(file, 'utf8');
  // ① 纯字面量：className="a b c"
  for (const m of src.matchAll(/className="([^"]*)"/g)) addGroup(m[1], file);
  // ② 表达式：className={`a b ${x}`} / className={'a b' + (c ? ' d' : '')}
  //    取表达式里所有字符串字面量拼起来 —— 漏掉拼接形式会整片漏报
  //    （K8sFiles 的 `'modal modal-lg' + (editFullscreen ? ' modal-fullscreen' : '')` 就是这种）
  for (const m of src.matchAll(/className=\{([^}]*)\}/g)) {
    let text = '';
    for (const s of m[1].matchAll(/'([^']*)'|"([^"]*)"|`([^`]*)`/g)) {
      text += ' ' + (s[1] ?? s[2] ?? s[3] ?? '');
    }
    addGroup(text, file);
  }
}

// ---------------------------------------------------------------- 审计
// 只报「尺寸类属性」：这是「窗口太小 / 面板被压扁」的唯一成因族。
// 颜色/边框这类属性上「基类 + 修饰类」互相覆盖是正常设计（.btn 与 .btn-danger），
// 不报，否则全是噪音（实测放开后 258 条，其中 99% 是有意为之）。
const SIZE_PROPS = new Set([
  'width', 'min-width', 'max-width',
  'height', 'min-height', 'max-height',
  'flex', 'flex-basis', 'flex-grow', 'flex-shrink',
]);

const catA = [];
const catB = [];

// 尺寸属性上「谁压谁」还要看方向：本仓命名是 `<base>` 与 `<base>-<修饰>` / `<域>-<base>`，
// 例如 `.modal` 与 `.modal-lg`、`.modal` 与 `.kb-ctx-modal`。
//   - 修饰类的名字**包含**基类名，且修饰类输了 → 变体被基类压掉（可疑，就是本 bug）
//   - 反过来基类输给修饰类 → 正常（修饰类本就该赢）
// 定义要放在循环之前：`direction` 在循环里被调用，而 const 有暂时性死区。
const SUSPICIOUS = 'suspicious';
const NORMAL = 'normal';
const UNRELATED = 'unrelated';

function direction(loser, winner) {
  const lc = loser.rule.classes;
  const wc = winner.rule.classes;
  if (lc.length !== 1 || wc.length !== 1) return UNRELATED;
  const [l] = lc;
  const [w] = wc;
  if (l !== w && l.includes(w)) return SUSPICIOUS; // 变体名含基类名，却输给了基类
  if (l !== w && w.includes(l)) return NORMAL; // 基类输给变体，符合预期
  return UNRELATED;
}

for (const [key, files] of groups) {
  const group = key.split(' ');
  const set = new Set(group);
  // 适用规则：选择器的类集是元素类集的子集
  const applicable = rules.filter((r) => r.classes.every((c) => set.has(c)));
  if (applicable.length < 2) continue;

  const props = new Set();
  for (const r of applicable) for (const d of r.decls) props.add(d.prop);

  for (const prop of props) {
    const decls = [];
    for (const r of applicable) {
      for (const d of r.decls) if (d.prop === prop) decls.push({ ...d, rule: r });
    }
    if (decls.length < 2) continue;
    // 排序：特异度 > 文件顺序 > 文件内顺序
    const rank = (d) => [d.rule.spec, d.rule.fileIndex, d.rule.order];
    const cmp = (a, b) => {
      const A = rank(a);
      const B = rank(b);
      for (let i = 0; i < 3; i++) if (A[i] !== B[i]) return A[i] - B[i];
      return 0;
    };
    const sorted = [...decls].sort(cmp);
    const win = sorted[sorted.length - 1];
    const keyOf = (r) => r.classes.join('.');
    for (const lose of sorted.slice(0, -1)) {
      if (lose.value === win.value) continue; // 值相同，无副作用
      if (lose.rule.spec !== win.rule.spec) continue; // 特异度不同属正常层叠
      // 同一个类在多份文件里被重定义：这是本仓**有意的分层细化**（shell.css 有注释说明），
      // 后加载者胜是设计如此。只有当「不同类」互相压时才是意外。
      if (keyOf(lose.rule) === keyOf(win.rule)) continue;
      const rec = {
        group: key,
        prop,
        loser: `${lose.rule.raw} { ${prop}: ${lose.value} }`,
        winner: `${win.rule.raw} { ${prop}: ${win.value} }`,
        files: [...files].slice(0, 3).join(', '),
        size: SIZE_PROPS.has(prop),
        direction: direction(lose, win),
      };
      if (lose.rule.fileIndex < win.rule.fileIndex) catA.push(rec);
      else catB.push(rec);
    }
  }
}

// ---------------------------------------------------------------- 分类
const sizeConflicts = [...catA, ...catB].filter((r) => r.size);
const sizeSuspicious = sizeConflicts.filter((r) => r.direction === SUSPICIOUS);
const sizeNormal = sizeConflicts.filter((r) => r.direction !== SUSPICIOUS);

// 非尺寸属性里同样只挑「变体输给基类」这一方向：那才是作者写了却没生效的样式。
// 基类输给修饰类（.btn → .btn-danger、.card-soft → .cf-cfg-card）是正常设计。
const otherSuspicious = catA.filter((r) => !r.size && r.direction === SUSPICIOUS);
const otherNormal = catA.filter((r) => !r.size && r.direction !== SUSPICIOUS);

// 同文件内的覆盖大多是「有意分层」（同一个类在多份文件里逐层细化），但同文件内
// 「变体输给基类」仍是可疑的 —— 多半是把变体写在了基类前面。
const sameFileSuspicious = catB.filter((r) => r.direction === SUSPICIOUS);

// ---------------------------------------------------------------- 报告
const fmt = (r) =>
  `  元素类: ${r.group}\n    被压掉: ${r.loser}\n    实际生效: ${r.winner}\n    出处: ${r.files}`;

const aOther = catA.filter((r) => !r.size);

console.log(`解析规则 ${rules.length} 条，className 组合 ${groups.size} 组\n`);

console.log(`=== ⚠ 尺寸属性被压掉·变体输给基类（就是「窗口太小」）: ${sizeSuspicious.length} 项 ===`);
if (!sizeSuspicious.length) console.log('  （无）');
for (const r of sizeSuspicious) console.log(fmt(r));

console.log(`\n=== ⚠ 非尺寸属性·变体输给基类（写了却没生效的视觉样式）: ${otherSuspicious.length} 项 ===`);
if (!otherSuspicious.length) console.log('  （无）');
for (const r of otherSuspicious) console.log(fmt(r));

console.log(`\n=== 尺寸属性·基类输给变体 / 无从判断（多为正常）: ${sizeNormal.length} 项 ===`);
if (!SHOW_ALL) console.log('  （默认折叠，加 --all 查看）');
else for (const r of sizeNormal) console.log(fmt(r));

console.log(`\n=== 非尺寸属性·基类输给修饰类（正常设计，默认折叠）: ${otherNormal.length} 项 ===`);
if (SHOW_ALL) for (const r of otherNormal) console.log(fmt(r));

console.log(`\n=== ⚠ 同文件内·变体输给基类（变体多半写在了基类前面）: ${sameFileSuspicious.length} 项 ===`);
if (!sameFileSuspicious.length) console.log('  （无）');
for (const r of sameFileSuspicious) console.log(fmt(r));

console.log(`\n=== 同文件内其他覆盖（有意分层，默认折叠）: ${catB.length - sameFileSuspicious.length} 项 ===`);
if (SHOW_ALL) for (const r of catB.filter((x) => x.direction !== SUSPICIOUS)) console.log(fmt(r));

process.exit(sizeSuspicious.length + otherSuspicious.length + sameFileSuspicious.length ? 1 : 0);
