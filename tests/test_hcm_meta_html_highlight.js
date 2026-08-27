// 语法校验 web/hcm-meta.html 的两段脚本（仅语法解析 vm.Script，不执行 DOM）。
const fs = require('fs');
const vm = require('vm');

const file = '/Users/caozhaoqi/PycharmProjects/jira-git-gui/web/hcm-meta.html';
const s = fs.readFileSync(file, 'utf8');

let ok = true;
const fail = (where, e) => { ok = false; console.error('SYNTAX-FAIL [' + where + ']:', e && e.message); };

// 1) 主页面静态 <script> 块
const reMain = /<script>([\s\S]*?)<\/script>/g;
let m, n = 0;
while ((m = reMain.exec(s))) { n++; try { new vm.Script(m[1]); } catch (e) { fail('main#' + n, e); } }
console.log('main script blocks:', n);

// 2) 详情窗口内联脚本（renderDetailDoc 内 '...'+'...' 拼接表达式）
const di = s.indexOf('const DATA=');
const tag = s.lastIndexOf("'<script>'", di);          // '<script>'+ 起点
const afterOpen = tag + "'<script>'".length + 1;       // 跳过 '<script>'+ 之后的换行
// 详情结尾为 '<\/script'（注意：注释里也有一处 <\/script>，需从 afterOpen 之后查找避开）
const close = s.indexOf("'<\\/script", afterOpen);
console.log('di=', di, 'tag=', tag, 'afterOpen=', afterOpen, 'close=', close);
if (close < 0) { fail('detail', new Error('未找到详情内联脚本结尾')); }
else {
  const endPos = s.lastIndexOf('+', close);   // 去掉结尾的 + '<\/script>...
  const inner = s.slice(afterOpen, endPos);
  try { new vm.Script(inner); console.log('detail inline script: SYNTAX-OK'); }
  catch (e) { fail('detail-inline', e); }
}

console.log(ok ? 'ALL-JS-OK' : 'JS-FAIL');
process.exit(ok ? 0 : 1);
