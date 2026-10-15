/**
 * Kibana 日志「实时查找 / 关键词高亮」的纯函数。
 *
 * 单独抽成零依赖模块，便于用 Node 直接跑单测（同 `kibanaExport.ts` 的理由：
 * 不能 import api/client，也不能 import i18n/index.ts）。
 */

/**
 * 从主筛选条的「关键词」里取出「可以按字面高亮」的短语。
 *
 * 规则与后端 `core/kibana/queries.py::keyword_filter` **逐条对齐**：
 *   - 含 `*` / `?` / `:` 或 AND/OR/NOT 操作符 → 后端走 `query_string`
 *     （通配符 / 字段限定 / 布尔组合），返回的行**不一定逐字包含**关键词
 *     （如 `level:ERROR`、`hcm*`）→ 此时高亮会一处都匹配不上，反而误导，故返回 ''。
 *   - 其余 → 后端走 `match_phrase`，命中行的正文里逐字包含该短语 → 可安全高亮。
 *
 * ⚠️ 这个函数只回答「能不能按字面高亮」，不负责过滤：通配符查询返回的行仍是
 * 用户想看的结果，绝不能因为「高亮词匹配不上」就把行过滤掉。
 */
export function literalPhraseOf(keyword: string | undefined | null): string {
  const kw = (keyword || '').trim();
  if (!kw) return '';
  // 与 keyword_filter 的 query_string 判定保持同一套特征
  if (/[*?:]/.test(kw)) return '';
  if (/\b(?:AND|OR|NOT)\b/.test(kw)) return '';
  return kw;
}

/**
 * 高亮 / 过滤用的实际查询词：用户在实时查找框里输入的内容优先，
 * 没有输入时回落到主筛选关键词里可逐字高亮的那部分。
 * 返回 '' 表示「既没有查找词也没有可高亮的关键词」。
 */
export function effectiveFindText(findText: string, keyword?: string | null): string {
  return (findText || '').trim() || literalPhraseOf(keyword);
}
