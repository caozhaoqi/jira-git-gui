// 关键词高亮口径（components/kibana/kibanaFind.ts）无头测试。
//
// 为什么需要它：Kibana 主筛选的「关键词」现在会驱动日志行高亮。判定规则必须与后端
// `core/kibana/queries.py::keyword_filter` 逐条一致——
//   - 普通词 → 后端 match_phrase，命中行正文逐字包含该词 → 高亮安全；
//   - 含 * ? : 或 AND/OR/NOT → 后端 query_string（通配符/字段限定/布尔），
//     返回的行不一定逐字包含关键词 → 此时若拿它去「过滤」就会把用户刚查出来的
//     行全部过滤光（一行日志都看不到），所以必须既不高亮也不过滤。
// 这条回归曾以「搜索后一行都不显示」的形式出现过，故在此锁死。
//
// 运行：node tests/test_kibana_find.mjs
import { literalPhraseOf, effectiveFindText }
  from '../frontend/web-react/src/components/kibana/kibanaFind.ts';

let failed = 0;
function check(name, condition, extra = '') {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}${extra ? ` — ${extra}` : ''}`);
  }
}

console.log('literalPhraseOf: 后端走 match_phrase 的词 → 原样返回（可逐字高亮）');
check('普通单词', literalPhraseOf('INFO') === 'INFO');
check('多词短语（空格分词仍走 match_phrase）', literalPhraseOf('POST /api') === 'POST /api');
check('前后空白被裁掉', literalPhraseOf('  hcm  ') === 'hcm');
check('含点/斜杠等普通字符不受影响', literalPhraseOf('api/hcm.model.list') === 'api/hcm.model.list');
check('下划线/数字/大小写混合', literalPhraseOf('Emp_0012a') === 'Emp_0012a');
check('小写 and/or/not 不是操作符（ES query_string 只认大写）',
  literalPhraseOf('error and warn') === 'error and warn');

console.log('literalPhraseOf: 后端走 query_string 的词 → 返回空串（禁止高亮/过滤）');
check('通配符 *', literalPhraseOf('hcm*') === '');
check('通配符 ?', literalPhraseOf('h?m') === '');
check('字段限定 :', literalPhraseOf('level:ERROR') === '');
check('字段限定 + 值', literalPhraseOf('log:hcm') === '');
check('AND 操作符', literalPhraseOf('hcm AND error') === '');
check('OR 操作符', literalPhraseOf('hcm OR warn') === '');
check('NOT 操作符', literalPhraseOf('hcm NOT debug') === '');
check('行首 AND', literalPhraseOf('AND hcm') === '');
check('空 / undefined / null', literalPhraseOf('') === '' && literalPhraseOf(undefined) === ''
  && literalPhraseOf(null) === '');

console.log('effectiveFindText: 查找框优先，否则回落关键词');
check('查找框有值 → 用查找词', effectiveFindText('GET', 'hcm') === 'GET');
check('查找框只有空白 → 仍回落到关键词', effectiveFindText('   ', 'hcm') === 'hcm');
check('查找框空 + 关键词是通配符 → 空串（既不高亮也不过滤）',
  effectiveFindText('', 'hcm*') === '');
check('查找框空 + 无关键词 → 空串', effectiveFindText('', '') === '');
check('查找框空 + 多词短语 → 用该短语', effectiveFindText('', 'POST /api') === 'POST /api');

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
