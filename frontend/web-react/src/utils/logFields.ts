// 日志记录「类型 / 时间 / 内容」字段的统一取值兜底。
//
// 背景：不同记录模型的字段命名并不统一，拿 dynamic_log 的约定去套别的模型会整列空白：
//   - dynamic_log    ：log_type（类型）  + create_time / created_at（时间）
//   - SyncOuterRecord：name（描述）      + update_time（同步时间）—— 该模型没有 log_type
// 因此展示与过滤都要按多字段名依次回退，新增模型时只需在这里补字段即可。

// 记录模型 -> 「类型 / 描述」过滤字段（与后端 api.cf.cf_logs._MODEL_TYPE_FIELDS 保持一致）。
// 过滤时必须按模型选字段，否则拿 log_type 去过滤 SyncOuterRecord 会过滤不到 / 字段不存在。
const MODEL_TYPE_FIELDS: Record<string, string> = {
  dynamic_log: 'log_type',
  syncouterrecord: 'name',
};

/** 返回该记录模型用于「类型 / 描述」过滤的字段名（未知模型默认 log_type）。 */
export function modelTypeField(model: string): string {
  const key = (model || '').trim().toLowerCase();
  return MODEL_TYPE_FIELDS[key] || 'log_type';
}

export interface LogRowLike {
  log_type?: string;
  logType?: string;
  name?: string;
  title?: string;
  description?: string;
  desc?: string;
  type?: string;
  create_time?: string | number;
  createTime?: string;
  created_at?: string;
  create_date?: string;
  update_time?: string | number;
  updateTime?: string;
  updated_at?: string;
  content?: any;
  message?: any;
  data?: any;
  [k: string]: any;
}

/** 类型 / 描述：模型自身字段 → 调用方给的兜底值（如查询时填的 log_type）→ '(未知)'。 */
export function logRowType(row: LogRowLike | null | undefined, fallback = ''): string {
  if (!row) return fallback || '(未知)';
  return (
    row.log_type || row.logType || row.name || row.title ||
    row.description || row.desc || row.type ||
    fallback || '(未知)'
  );
}

/** 时间：dynamic_log 的 create_* 优先，其次 SyncOuterRecord 的 update_*。 */
export function logRowTime(row: LogRowLike | null | undefined): string {
  if (!row) return '';
  const v =
    row.create_time ?? row.createTime ?? row.created_at ?? row.create_date ??
    row.update_time ?? row.updateTime ?? row.updated_at ?? '';
  return v === 0 ? '' : String(v ?? '');
}

/** 内容：content → message → data（对象统一 JSON 化，保持与 CfPanel 原有行为一致）。 */
export function logRowContent(row: LogRowLike | null | undefined): string {
  if (!row) return '';
  const c = row.content ?? row.message ?? row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c) : String(c);
}
