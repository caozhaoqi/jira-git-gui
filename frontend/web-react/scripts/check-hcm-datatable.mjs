#!/usr/bin/env node
/**
 * HCM 数据表格视图的不变量（静态源码断言）。
 *
 * 背景：HCM「数据」子页签原先只有 JSON 视图，983 条 × 几十列时读不动。
 * 新增表格视图 + 全屏 + 独立窗口，这里锁定几条不能回退的行为：
 *  1. 表格与 JSON 双视图共存，默认表格；
 *  2. 数据源兼容 data.list / data.data.list / 直出数组三种形态；
 *  3. 嵌套值折叠成 {…}/[n] 而不是展开成多层列；
 *  4. 空值统一 —，不显示空白（否则看起来像渲染失败）；
 *  5. 独立窗口通过 sessionStorage 传数据，且读取后即删；
 *  6. 数据过大时降级提示改用全屏，而不是静默失败。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, '..', 'src', 'components', 'hcm');
const read = (f) => readFileSync(join(SRC, f), 'utf8');

const browser = read('HcmObjectBrowser.tsx');
const table = read('HcmDataTable.tsx');
const win = read('HcmTableWindow.tsx');
const root = readFileSync(join(here, '..', 'src', 'Root.tsx'), 'utf8');

let failed = 0;
const check = (name, cond, detail = '') => {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.log(`  ✗ ${name}${detail ? ' — ' + detail : ''}`); failed++; }
};

console.log('HCM 数据表格视图 · 不变量');

check('表格/JSON 双视图状态', /dataViewMode, setDataViewMode\] = useState<'table' \| 'json'>\('table'\)/.test(browser));
check('数据源兼容三种形态', /Array\.isArray\(dataResult\.list\)/.test(browser) && /dataResult\.data\?\.list/.test(browser));
check('表格列数按字段并集统计', /const dataColCount = useMemo/.test(browser));
check('嵌套值折叠为摘要', /\{text: `\[\$\{v\.length\}\]`/.test(table) || /\[\\\$\{v\.length\}\]|\[\$\{v\.length\}\]/g.test(table));
check('空值显示 — 而非空白', /text: '—'/.test(table));
check('点列头排序', /function toggleSort/.test(table));
check('序号列可配', /showIndex/.test(table));
check('全屏模态存在', /tableFullscreen &&/.test(browser));
check('新窗口入口存在', /openDataTableWindow/.test(browser));
check('独立窗口读取后即删 sessionStorage', /sessionStorage\.removeItem\(key\)/.test(win));
check('数据过大降级提示', /dataNewWindowTooBig/.test(browser));
check('hcm-table 路由已注册', /qs\.has\('hcm-table'\)/.test(root));

// 独立窗口主题（实测 bug：暗色下新窗口整片是白的）
const theme = readFileSync(join(here, '..', 'src', 'utils', 'standaloneTheme.ts'), 'utf8');
check('独立窗口补主题类名', /classList\.toggle\('dark'/.test(theme));
check('表格窗口应用独立主题', /useStandaloneTheme\(\)/.test(win));
check('Root 统一处理独立窗口主题', /applyStandaloneTheme/.test(root));

console.log(failed ? `\n✗ ${failed} 项失败` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
