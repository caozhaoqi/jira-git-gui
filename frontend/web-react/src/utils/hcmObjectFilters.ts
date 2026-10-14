export type HcmFilterOperator = 'eq' | 'contains';

export interface HcmObjectFilterRow {
  field: string;
  value: string;
  operator?: HcmFilterOperator;
}

// --------------------------------------------------------------------------- //
//  同步台帐（U_SyncRaw*）专用筛选字段规划
//  这些表的业务列是 u_* 前缀；按业务含义命名，避免用户面对裸字段名。
// --------------------------------------------------------------------------- //

export type SyncLedgerFieldKind = 'select' | 'text';

export interface SyncLedgerFilterField {
  key: string;
  label: string;
  kind: SyncLedgerFieldKind;
  placeholder?: string;
}

/** 台帐筛选字段规划（仅渲染当前模型 meta 里真实存在的字段）。 */
export const SYNC_LEDGER_FIELDS: SyncLedgerFilterField[] = [
  { key: 'u_entity_type', label: '实体类型', kind: 'select' },
  { key: 'u_source_name', label: '数据名', kind: 'text', placeholder: '如 张三 / 北京轨道运营' },
  { key: 'u_hcm_id', label: 'HCM ID', kind: 'text', placeholder: 'HCM 系统内写入的 id' },
  { key: 'u_source_code', label: '源编码', kind: 'text', placeholder: '工号 / 组织编码' },
  { key: 'u_hcm_code', label: 'HCM 编码', kind: 'text', placeholder: 'HCM 侧编码' },
  { key: 'u_source_parent_code', label: '上级编码', kind: 'text', placeholder: '上级组织编码' },
  { key: 'u_interface_code', label: '接口', kind: 'select' },
  { key: 'u_is_deleted', label: '是否删除', kind: 'select' },
  { key: 'u_sync_batch', label: '同步批次', kind: 'text' },
  { key: 'u_error_msg', label: '错误信息', kind: 'text', placeholder: '按错误信息包含匹配' },
];

/** 该模型是否同步台帐（具备 u_entity_type + u_source_name 业务列）。 */
export function isSyncLedgerModel(fields: HcmFilterField[]): boolean {
  const keys = new Set(fields.map((f) => f.key));
  return keys.has('u_entity_type') && keys.has('u_source_name');
}

/** 只保留模型里真实存在的台帐筛选字段，保持规划顺序。 */
export function syncLedgerFieldsFor(fields: HcmFilterField[]): SyncLedgerFilterField[] {
  const keys = new Set(fields.map((f) => f.key));
  return SYNC_LEDGER_FIELDS.filter((f) => keys.has(f.key));
}

/** 少数 select 字段的静态选项（动态取值拿不到时的兜底，如是否删除）。 */
export function staticSyncSelectOptions(key: string): { value: string; label: string }[] {
  if (key === 'u_is_deleted') {
    return [
      { value: '0', label: '未删除 (0)' },
      { value: '1', label: '已删除 (1)' },
    ];
  }
  return [];
}

/** 实体类型中文标签（best-effort，未知类型原样返回）。 */
export function entityTypeLabel(value: string): string {
  const MAP: Record<string, string> = {
    ORG: '组织',
    DEPT: '部门',
    POSITION: '岗位',
    EMPLOYEE: '人员',
    POS_REL: '岗位关系',
    DEPT_REL: '部门关系',
    ORG_REL: '组织关系',
    ORG_LEGAL: '法人组织',
    ORG_CONTACT: '组织联系人',
    EMP_ORG: '人员任职',
    EMP_OUTER: '外部人员',
    EMP_EDU: '教育经历',
    EMP_FAMILY: '家庭成员',
  };
  return MAP[value] || value;
}

/** 从任意层级的响应里挖出 list 数组（兼容 {list} / {result:{list}} / 双层包裹）。 */
function digList(resp: unknown): any[] {
  if (!resp || typeof resp !== 'object') return [];
  const o = resp as Record<string, unknown>;
  if (Array.isArray(o.list)) return o.list as any[];
  if (o.result && typeof o.result === 'object') return digList(o.result);
  return [];
}

/**
 * 解析 `hcm.model.count` 的 group_by 结果 → [{value,count}]（按 count 降序）。
 * 请求示例：{ model, filter_dict:{}, group_by:['u_entity_type'], count_by:['*'] }
 * 返回项形如：{ count__*: 6758, u_entity_type: 'EMPLOYEE', '*': 6758 }
 */
export function parseGroupCount(resp: unknown, field: string): { value: string; count: number }[] {
  const list = digList(resp);
  const out: { value: string; count: number }[] = [];
  const seen = new Set<string>();
  for (const item of list) {
    if (!item || typeof item !== 'object') continue;
    const raw = (item as Record<string, unknown>)[field];
    if (raw === null || raw === undefined || raw === '') continue;
    const value = String(raw);
    if (seen.has(value)) continue;
    seen.add(value);
    const c = Number(
      (item as Record<string, unknown>)['count__*'] ?? (item as Record<string, unknown>)['*'] ?? 0,
    );
    out.push({ value, count: Number.isFinite(c) ? c : 0 });
  }
  return out.sort((a, b) => b.count - a.count);
}

/** 组装台帐筛选 filter_dict：select 走精确等于，text 走包含（服务端自行补 %）。 */
export function buildSyncLedgerFilterDict(
  values: Record<string, string>,
  fields: SyncLedgerFilterField[],
): Record<string, string | { like: string }> {
  const dict: Record<string, string | { like: string }> = {};
  for (const f of fields) {
    const v = (values[f.key] ?? '').trim();
    if (!v) continue;
    dict[f.key] = f.kind === 'select' ? v : { like: v };
  }
  return dict;
}

export interface HcmFilterField {
  key: string;
  name?: string | null;
  type?: string;
  is_filter?: boolean;
  is_blur?: boolean;
  is_encrypt?: boolean;
  [key: string]: any;
}

// --------------------------------------------------------------------------- //
//  台帐结果导出（跨页拉全 → CSV / JSON）
// --------------------------------------------------------------------------- //

/** 导出分页拉取时的单页行数。实测网关对 page_size 无硬上限（2000 行 ≈ 2.5MB / 1.5s）。 */
export const SYNC_EXPORT_PAGE_SIZE = 1000;
/** 单次导出的最大行数保护（避免误点全表导出把内存打爆）。 */
export const SYNC_EXPORT_MAX_ROWS = 200000;
/** 单次「复制到剪贴板」的最大行数——剪贴板是一整串文本，太大会卡住渲染进程。 */
export const SYNC_COPY_MAX_ROWS = 20000;
/** 命中行数超过该值时，导出前先让用户确认（全表约 9.4 万行、约 1 分钟）。 */
export const SYNC_EXPORT_CONFIRM_ROWS = 5000;

export interface SyncLedgerExportColumn {
  key: string;
  label: string;
}

/**
 * 解析 `hcm.model.count` 的命中总数。
 * 请求：{ model, filter_dict, count_by:['*'] } → 响应 data.list[0] = { 'count__*': n, '*': n }
 */
export function parseTotalCount(resp: unknown): number {
  const list = digList(resp);
  if (!list.length) return 0;
  const item = list[0] as Record<string, unknown>;
  const n = Number(item?.['count__*'] ?? item?.['*'] ?? item?.['count'] ?? 0);
  return Number.isFinite(n) ? n : 0;
}

/**
 * 规划导出列：先按元数据顺序取「业务列」（is_list/is_info 为真，即台帐的 u_* 字段），
 * 再补数据里出现的其它键（如 id）。exclude 用于剔除体积巨大的原始报文列。
 */
export function syncLedgerExportColumns(
  fields: HcmFilterField[],
  rows: Record<string, any>[],
  opts: { exclude?: string[] } = {},
): SyncLedgerExportColumn[] {
  const exclude = new Set(opts.exclude || []);
  const sample = rows.slice(0, 50).filter((r) => r && typeof r === 'object');
  const present = new Set<string>();
  for (const r of sample) for (const k of Object.keys(r)) present.add(k);

  const labelOf = new Map<string, string>();
  for (const f of fields) {
    if (f?.key && f.name) labelOf.set(f.key, String(f.name));
  }

  const out: SyncLedgerExportColumn[] = [];
  const seen = new Set<string>();
  const push = (key: string) => {
    if (!key || seen.has(key) || exclude.has(key)) return;
    seen.add(key);
    out.push({ key, label: labelOf.get(key) || key });
  };
  // 1) 业务列（元数据顺序）
  for (const f of fields) {
    if (f?.key && (f.is_list === true || f.is_info === true) && present.has(f.key)) push(f.key);
  }
  // 2) 其余出现在数据里的列（保持行内首次出现顺序，如 id）
  for (const r of sample) for (const k of Object.keys(r)) push(k);
  return out;
}

/** 单元格取值 → 字符串（对象/数组转 JSON，null/undefined 转空串）。 */
export function csvCellValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** 单元格转义：含分隔符/引号/换行时用双引号包裹并翻倍内部引号（RFC 4180）。 */
export function csvEscape(value: unknown): string {
  const s = csvCellValue(value);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 二维数据 → CSV 文本（CRLF 行分隔，兼容 Excel）。 */
export function toCsvText(headers: string[], rows: unknown[][]): string {
  const lines = [headers.map(csvEscape).join(',')];
  for (const row of rows) lines.push(row.map(csvEscape).join(','));
  return lines.join('\r\n');
}

/** 生成导出文件名：<模型>_<本地时间戳>.<ext>（模型名里的非法字符替换为下划线）。 */
export function exportFileName(model: string, ext: string, date: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  const stamp =
    `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}` +
    `_${p(date.getHours())}${p(date.getMinutes())}${p(date.getSeconds())}`;
  const safe = String(model || 'hcm').replace(/[^A-Za-z0-9_.-]+/g, '_');
  return `${safe}_${stamp}.${ext}`;
}

/**
 * 台帐导出 CSV。
 * bom=true（默认）时前置 UTF-8 BOM，Excel 直接打开不乱码；复制到剪贴板时应传 false，
 * 否则 BOM 会作为一个乱码字符粘在最前面。
 */
export function buildSyncLedgerCsv(
  fields: HcmFilterField[],
  rows: Record<string, any>[],
  opts: { exclude?: string[]; bom?: boolean } = {},
): string {
  const cols = syncLedgerExportColumns(fields, rows, opts);
  const headers = cols.map((c) => c.label);
  const body = rows.map((r) =>
    cols.map((c) => (r && typeof r === 'object' ? (r as Record<string, any>)[c.key] : '')),
  );
  const csv = toCsvText(headers, body);
  return (opts.bom ?? true) ? `\ufeff${csv}` : csv;
}

/** 台帐导出 JSON（附带模型/筛选条件/导出行数，便于回溯这批数据是怎么筛出来的）。 */
export function buildSyncLedgerJson(params: {
  model: string;
  filter: Record<string, unknown>;
  total: number;
  rows: Record<string, any>[];
  exportedAt?: Date;
  /** 需要剔除的列（如体积巨大的 u_raw_json），与 CSV 导出保持同一套开关语义。 */
  exclude?: string[];
}): string {
  const { model, filter, total, rows, exportedAt } = params;
  const exclude = (params.exclude || []).filter(Boolean);
  const outRows = exclude.length
    ? rows.map((r) => {
        if (!r || typeof r !== 'object') return r;
        const copy: Record<string, any> = { ...r };
        for (const k of exclude) delete copy[k];
        return copy;
      })
    : rows;
  return JSON.stringify(
    {
      model,
      exported_at: (exportedAt || new Date()).toISOString(),
      filter_dict: filter,
      total,
      row_count: outRows.length,
      excluded_fields: exclude,
      rows: outRows,
    },
    null,
    2,
  );
}

export interface FetchAllRowsOptions {
  pageSize?: number;
  maxRows?: number;
  maxPages?: number;
  onProgress?: (loaded: number, total: number) => void;
}

export interface FetchAllRowsResult {
  rows: any[];
  /** 服务端给出的命中总数（拿不到时回退为实际拉取行数）。 */
  total: number;
  pages: number;
  /** 是否因为 maxRows/maxPages 提前截断。 */
  truncated: boolean;
}

/**
 * 按页拉全某次查询的全部行（`hcm.model.list` 单页上限内的循环）。
 *
 * 停止条件（任一满足）：返回空页 / 已拉满 total / 该页不足一页 / 触达 maxRows、maxPages。
 * 服务端若把 page_size 收敛到更小值（首屏返回行数 < 请求值），后续按实际值当步长，
 * 避免被误判成「最后一页」而静默截断。
 */
export async function fetchAllRows(
  fetchPage: (
    pageIndex: number,
    pageSize: number,
  ) => Promise<{ list?: any[] | null; count?: number | null; total?: number | null } | null | undefined>,
  opts: FetchAllRowsOptions = {},
): Promise<FetchAllRowsResult> {
  const maxRows = opts.maxRows ?? SYNC_EXPORT_MAX_ROWS;
  const maxPages = opts.maxPages ?? 1000;
  const requested = Math.max(1, opts.pageSize ?? SYNC_EXPORT_PAGE_SIZE);
  let effective = requested;
  const rows: any[] = [];
  let total = 0;
  let pages = 0;
  let truncated = false;
  let prevSignature = '';

  for (let pageIndex = 1; pageIndex <= maxPages; pageIndex += 1) {
    const resp = await fetchPage(pageIndex, effective);
    const list = Array.isArray(resp?.list) ? (resp!.list as any[]) : [];
    pages = pageIndex;
    if (pageIndex === 1) {
      const t = Number(resp?.count ?? resp?.total ?? NaN);
      if (Number.isFinite(t) && t >= 0) total = t;
    }
    if (!list.length) break;
    if (pageIndex === 1 && list.length < effective) effective = list.length;
    rows.push(...list);
    opts.onProgress?.(rows.length, total);
    if (rows.length >= maxRows) {
      truncated = true;
      rows.length = maxRows;
      break;
    }
    if (total > 0 && rows.length >= total) break;
    if (list.length < effective) break;
    // 防御：相邻两页「行数相同且首行 id 相同」视为服务端重复返回同一页，立即停止避免死循环。
    const sig = `${list.length}:${String(list[0]?.id ?? '')}`;
    if (sig === prevSignature) break;
    prevSignature = sig;
  }
  if (!total) total = rows.length;
  return { rows, total, pages, truncated };
}

/** 筛选字段的统一候选形态（字段键 + 展示名 + 少量用于默认值/排序推断的提示位）。 */
export interface HcmFilterCandidate {
  key: string;
  label?: string;
  is_blur?: boolean;
  is_text?: boolean;
  is_encrypted?: boolean;
  /** 业务字段（meta 里 is_list/is_info 为真），相对 id/company_id/model_name/data_object_* 等框架列。 */
  is_business?: boolean;
}

/** Prefer the model's explicit filter fields; legacy/custom models fall back to all fields. */
export function selectHcmFilterFields<T extends HcmFilterField>(fields: T[]): T[] {
  const configured = fields.filter((field) => field.is_filter === true);
  return configured.length ? configured : fields;
}

/** 判断字段类型是否偏「文本」（用于默认筛选字段优先挑可做 like 的字段）。 */
function isTextFieldType(type: unknown): boolean {
  const s = String(type || '').toLowerCase();
  return s === 'string' || s === 'text';
}

/** 约定俗成的「名称类」字段：默认筛选优先挑它，用户输入关键字命中率最高。 */
const NAME_LIKE_KEYS = ['name', 'u_source_name', 'full_name', 'source_name', 'title'];

/**
 * 统一默认字段选择。优先级（跳过加密字段与 id）：
 *   1) 模糊+文本  → 2) 名称类字段（name/u_source_name…）
 *   → 3) 业务字段中的 name 结尾文本 → 4) 业务文本 → 5) 任意文本 → 6) 任意 → 7) 兜底。
 * 关键点：不要默认落到框架列（如 model_name 常为空）——那会让「输关键字→0 行」。
 */
export function pickDefaultFilterKey(cands: HcmFilterCandidate[]): string {
  const ok = (c: HcmFilterCandidate) => !c.is_encrypted && c.key !== 'id';
  return (
    cands.find((c) => c.is_blur && c.is_text && ok(c))?.key ||
    cands.find((c) => ok(c) && NAME_LIKE_KEYS.includes(c.key))?.key ||
    cands.find((c) => ok(c) && c.is_business && c.is_text && /name$/i.test(c.key))?.key ||
    cands.find((c) => ok(c) && c.is_business && c.is_text)?.key ||
    cands.find((c) => ok(c) && c.is_text)?.key ||
    cands.find((c) => ok(c))?.key ||
    cands[0]?.key ||
    ''
  );
}

/**
 * 以模型自身元数据（hcm.model.meta 的 fields）派生可筛选字段候选。
 * 这是字段下拉/联想的唯一来源：hcm.model.meta 返回的就是该模型自己的字段
 * （key/name/type/is_filter/is_blur/is_list/is_info…）。
 *
 * ⚠️ 不要用 hcm.paas.object.query.fields 替代它——那个接口返回的是「对象清单」
 * (hcm.paas.object) 的字段（class_/model_category/name/description/i18n），
 * 并非某个数据模型的字段；曾误用导致下拉只剩这几个对象字段。
 *
 * 排序：业务字段（is_list/is_info）排在前，框架列（id/company_id/model_name/
 * data_object_* 等）靠后，避免下拉里前 20 项都是无意义的框架列。
 */
export function metaFilterCandidates(fields: HcmFilterField[]): HcmFilterCandidate[] {
  const cands = selectHcmFilterFields(fields).map((f) => ({
    key: f.key,
    label: f.name ?? undefined,
    is_blur: f.is_blur === true,
    is_text: isTextFieldType(f.type),
    is_encrypted: f.is_encrypt === true,
    is_business: f.is_list === true || f.is_info === true,
  }));
  // 稳定排序（ES2019+）：仅把业务字段前置，组内保持元数据原序。
  return [...cands].sort((a, b) => Number(b.is_business === true) - Number(a.is_business === true));
}

/**
 * Pick a useful default for the one-field quick search without assuming a model's field names.
 * @deprecated 统一走 pickDefaultFilterKey(metaFilterCandidates(fields))；此处仅为向后兼容封装。
 */
export function defaultHcmTextFilterField(fields: HcmFilterField[]): string {
  return pickDefaultFilterKey(metaFilterCandidates(fields));
}

/**
 * Build a model.list filter_dict using the documented HCM filter syntax.
 *
 * 服务端语义（见 core/ds/combine_model.py `_get_field_filter`）：
 *  - 标量值 = 精确等于（eq）；数组值 = IN；None/空数组 = IS NULL。
 *  - { like: v } = 模糊匹配，**服务端自行给值两端补 `%`**，前端应传原值 v。
 *    传 { like: "%v%" } 会被二次包裹成 %%v%%，一般仍能命中但非标准写法。
 * 多行条件按 AND 组合（服务端对同一 field 的多个 op 亦为 AND）。
 */
export function buildHcmFilterDict(rows: HcmObjectFilterRow[]): Record<string, string | { like: string }> {
  const filterDict: Record<string, string | { like: string }> = {};
  for (const row of rows) {
    const field = row.field.trim();
    if (!field) continue;
    const value = row.value;
    if (row.operator === 'contains') {
      filterDict[field] = { like: value };
    } else {
      filterDict[field] = value;
    }
  }
  return filterDict;
}
