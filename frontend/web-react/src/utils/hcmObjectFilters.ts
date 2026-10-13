export type HcmFilterOperator = 'eq' | 'contains';

export interface HcmObjectFilterRow {
  field: string;
  value: string;
  operator?: HcmFilterOperator;
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

/** 统一默认字段选择：优先 模糊+文本+非加密 且非 id；否则文本；否则任一非加密非 id；最后兜底。 */
export function pickDefaultFilterKey(cands: HcmFilterCandidate[]): string {
  return (
    cands.find((c) => c.is_blur && c.is_text && !c.is_encrypted && c.key !== 'id')?.key ||
    cands.find((c) => c.is_text && !c.is_encrypted && c.key !== 'id')?.key ||
    cands.find((c) => !c.is_encrypted && c.key !== 'id')?.key ||
    cands[0]?.key ||
    ''
  );
}

/**
 * 以模型自身元数据（hcm.model.meta 的 fields）派生可筛选字段候选。
 * 仅作为 hcm.paas.object.query.fields 为空时的兜底来源（动态/自定义模型常见）。
 */
export function metaFilterCandidates(fields: HcmFilterField[]): HcmFilterCandidate[] {
  return selectHcmFilterFields(fields).map((f) => ({
    key: f.key,
    label: f.name ?? undefined,
    is_blur: f.is_blur === true,
    is_text: isTextFieldType(f.type),
    is_encrypted: f.is_encrypt === true,
  }));
}

/**
 * 扁平化 hcm.paas.object.query.fields 的响应。
 *
 * 该接口返回「分组」数组：{ key, name, info_fields[], list_fields[], model_fields[] }，
 * 每个字段项为 { key, label, component, data_type, is_encrypt, ... }。同时兼容两种返回：
 *   1) 分组数组（info/list/model `*_fields` 子数组）；
 *   2) 平铺字段数组（每项直接是 { key, label?, name? }）。
 * 按 key 去重，忽略没有 key 的项；分组对象本身不会被误当字段。
 */
export function queryFieldCandidates(resp: unknown): HcmFilterCandidate[] {
  let arr: any[] = [];
  if (Array.isArray(resp)) {
    arr = resp;
  } else if (resp && typeof resp === 'object') {
    // 兼容网关可能的外层包裹（{ data/list/fields/groups/result: [...] }）
    const wrap = resp as Record<string, unknown>;
    const hit = ['list', 'fields', 'groups', 'data', 'result'].find((k) => Array.isArray(wrap[k]));
    arr = (hit ? (wrap[hit] as any[]) : []);
  }
  const out: HcmFilterCandidate[] = [];
  const seen = new Set<string>();
  const push = (raw: any) => {
    if (!raw || typeof raw.key !== 'string' || !raw.key) return;
    if (seen.has(raw.key)) return;
    seen.add(raw.key);
    const comp = String(raw.component || '').toLowerCase();
    out.push({
      key: raw.key,
      label: raw.label || raw.name || undefined,
      is_text: comp.includes('input') || comp.includes('text') || isTextFieldType(raw.data_type) || isTextFieldType(raw.type),
      is_encrypted: raw.is_encrypt === true,
    });
  };
  for (const item of arr) {
    if (!item || typeof item !== 'object') continue;
    const groupArrays = Object.keys(item).filter((k) => k.endsWith('_fields') && Array.isArray((item as any)[k]));
    if (groupArrays.length) {
      for (const k of groupArrays) (item as any)[k].forEach(push);
    } else {
      push(item);
    }
  }
  return out;
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
