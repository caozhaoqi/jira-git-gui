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
