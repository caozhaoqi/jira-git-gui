/** Kibana 日志导出（下载）的分页拉取逻辑。
 *
 *  为什么单独抽成模块：这段循环同时涉及「短页结束」「安全上限」「后续页失败
 *  保留部分结果」三条边界，改后端上限时错过一次。放在 .tsx 里既无法无头测
 *  试、也读不顺，所以拆成**无第三方依赖**的纯函数（与 utils/hcmObjectFilters.ts
 *  同一策略），网络请求由调用方注入（测试见 tests/test_kibana_export_pages.mjs）。
 */

import type { KibanaExportResp, KibanaLogsReq } from '../../api/types';

/** 每页行数（后端单次上限 2 万，取 1 万留余量：Console Proxy 单次约 4s） */
export const EXPORT_PAGE = 10000;

/** 导出安全总行数上限：防止超大时间段把内存/后端打爆 */
export const EXPORT_MAX_ROWS = 200000;

/** 注入用的导出请求函数（默认由调用方接线 apiPost） */
export type KibanaExportPost = (
  path: string,
  body: unknown,
) => Promise<KibanaExportResp>;

export type ExportFetchResult = {
  /** 各页日志片段（调用方自行 join 落盘） */
  parts: string[];
  /** 实际拉到的总行数 */
  total: number;
  /** 后端建议的文件名（可能为空） */
  filename: string;
  /** 是否触到 `maxRows` 上限（命中时结果可能是半程的） */
  capped: boolean;
};

/**
 * 按给定筛选条件分页拉取全部匹配日志。
 *
 * - 短页（返回数 < EXPORT_PAGE）视为末页；
 * - `maxRows` 夹到 1–EXPORT_MAX_ROWS；**至少会发第一页请求**（from=0 必放行），
 *   否则用户把上限填得比一页还小时会一页都拉不到；
 * - 以 `ok === false` 判定失败（⚠️ 不能用「content 为空」判失败：后端对
 *   **0 行命中**同样返回 `content: ''`，那样合法的空导出会被误报「导出失败」）；
 * - 首页即失败抛错（调用方提示「导出失败」并保留对话框可重试）；
 *   后续页失败保留已拉到的部分，按正常结果返回。
 */
export async function fetchExportContent(
  req: Omit<KibanaLogsReq, 'size' | 'from_' | 'order'>,
  maxRows: number,
  onProgress: ((total: number) => void) | undefined,
  post: KibanaExportPost,
): Promise<ExportFetchResult> {
  const cap = Math.max(1, Math.min(Math.trunc(maxRows) || EXPORT_PAGE, EXPORT_MAX_ROWS));
  const parts: string[] = [];
  let from = 0;
  let total = 0;
  let filename = '';
  let capped = false;

  while (true) {
    // 预检上限：已拉到 cap 就不再发下一页（第一页 from=0 必须放行）
    if (from > 0 && total >= cap) { capped = true; break; }
    const d = await post('/api/kibana/export', { ...req, size: EXPORT_PAGE, from_: from });
    if (d.ok === false) {
      if (!parts.length) throw new Error(d.error || 'export failed');
      break; // 后续页失败：保留已拉到的部分
    }
    parts.push(d.content || '');
    filename = filename || d.filename || '';
    const n = d.count ?? 0;
    total += n;
    onProgress?.(total);
    if (n < EXPORT_PAGE) break; // 短页 = 末页（含 0 行命中）
    from += n;
    if (total >= cap) { capped = true; break; }
  }

  return { parts, total, filename, capped };
}
