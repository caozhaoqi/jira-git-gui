// Regression tests for model-aware HCM filter_dict construction.
import {
  buildHcmFilterDict,
  buildSyncLedgerFilterDict,
  defaultHcmTextFilterField,
  entityTypeLabel,
  isSyncLedgerModel,
  metaFilterCandidates,
  parseGroupCount,
  pickDefaultFilterKey,
  selectHcmFilterFields,
  staticSyncSelectOptions,
  syncLedgerFieldsFor,
} from '../frontend/web-react/src/utils/hcmObjectFilters.ts';

let failed = 0;
function check(name, condition) {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}`);
  }
}

console.log('buildHcmFilterDict');
const filters = buildHcmFilterDict([
  { field: ' employee_no ', value: 'A001', operator: 'eq' },
  { field: 'name', value: '张三', operator: 'contains' },
  { field: ' ', value: 'ignored', operator: 'eq' },
]);
check('keeps exact field key and value', filters.employee_no === 'A001');
// 服务端自行给 like 值补 %，前端只传原值（见 utils/hcmObjectFilters.ts 注释）
check('contains sends bare value in { like } (server adds % )', filters.name?.like === '张三');
check('skips empty field keys', Object.keys(filters).length === 2);
check('does not mutate filter input values', filters.employee_no === 'A001');

console.log('selectHcmFilterFields');
const fields = [
  { key: 'id', is_filter: false },
  { key: 'employee_no', is_filter: true },
  { key: 'name', is_filter: true },
];
check(
  'prefers explicitly filterable fields when present',
  selectHcmFilterFields(fields).map((field) => field.key).join(',') === 'employee_no,name',
);
const legacyFields = [{ key: 'id' }, { key: 'name', is_filter: false }];
check(
  'falls back to all model fields for custom/legacy metadata',
  selectHcmFilterFields(legacyFields).length === 2,
);
check('empty metadata returns empty suggestions', selectHcmFilterFields([]).length === 0);
check(
  'quick filter prefers model-configured blur text field',
  defaultHcmTextFilterField([
    { key: 'id', type: 'string' },
    { key: 'employee_no', type: 'string', is_filter: true },
    { key: 'full_name', type: 'string', is_filter: true, is_blur: true },
  ]) === 'full_name',
);
check(
  'quick filter falls back to a text field for models without blur config',
  defaultHcmTextFilterField([{ key: 'id', type: 'string' }, { key: 'code', type: 'string' }]) === 'code',
);

console.log('metaFilterCandidates');
const metaCands = metaFilterCandidates([
  { key: 'id', name: 'ID', type: 'string' },
  { key: 'name', name: '姓名', type: 'string', is_blur: true, is_filter: true },
  { key: 'mobile', name: '手机', type: 'string', is_encrypt: true, is_filter: true },
]);
check(
  'maps meta fields to candidates and skips encrypted in default',
  pickDefaultFilterKey(metaCands) === 'name',
);
check(
  'encrypted-only text fields do not become default',
  pickDefaultFilterKey([{ key: 'id', type: 'string' }, { key: 'secret', type: 'string', is_encrypted: true }]) === 'id',
);

// 同步台帐类模型：框架列（model_name 常为空）不能当默认；应落到业务字段 u_source_name
console.log('metaFilterCandidates / business-first default (sync staging shape)');
const syncFields = [
  { key: 'id', type: 'integer', is_list: false, is_info: false },
  { key: 'company_id', type: 'integer', is_list: false, is_info: false },
  { key: 'model_name', type: 'string', is_list: false, is_info: false },
  { key: 'data_object_text1', type: 'text', is_list: false, is_info: false },
  { key: 'u_interface_name', type: 'string', is_list: true, is_info: true },
  { key: 'u_source_name', type: 'string', is_list: true, is_info: true },
];
const syncCands = metaFilterCandidates(syncFields);
check('does not default to empty framework column model_name', pickDefaultFilterKey(syncCands) === 'u_source_name');
check(
  'business fields are ordered before framework columns',
  syncCands.findIndex((c) => c.key === 'u_source_name') < syncCands.findIndex((c) => c.key === 'model_name'),
);
check('marks is_list/is_info as business', syncCands.find((c) => c.key === 'u_source_name')?.is_business === true);
check('framework column is not business', syncCands.find((c) => c.key === 'model_name')?.is_business === false);
check(
  'prefers name-like business field for core models',
  pickDefaultFilterKey(
    metaFilterCandidates([
      { key: 'id', type: 'integer' },
      { key: 'number', type: 'string', is_list: true },
      { key: 'name', type: 'string', is_list: true },
    ]),
  ) === 'name',
);

console.log('sync ledger (U_SyncRaw*) filters');
const ledgerFields = [
  { key: 'id', type: 'integer' },
  { key: 'model_name', type: 'string' },
  { key: 'u_entity_type', type: 'string' },
  { key: 'u_source_name', type: 'string' },
  { key: 'u_hcm_id', type: 'string' },
  { key: 'u_interface_code', type: 'string' },
];
check('detects sync ledger model by u_entity_type + u_source_name', isSyncLedgerModel(ledgerFields) === true);
check('non-sync model is not detected', isSyncLedgerModel([{ key: 'name' }, { key: 'number' }]) === false);
check(
  'only planned fields that exist are offered, in plan order',
  syncLedgerFieldsFor(ledgerFields).map((f) => f.key).join(',') ===
    'u_entity_type,u_source_name,u_hcm_id,u_interface_code',
);
check(
  'select fields are exact, text fields are contains',
  JSON.stringify(buildSyncLedgerFilterDict({ u_entity_type: 'EMPLOYEE', u_source_name: '张' }, syncLedgerFieldsFor(ledgerFields))) ===
    JSON.stringify({ u_entity_type: 'EMPLOYEE', u_source_name: { like: '张' } }),
);
check(
  'empty values are skipped',
  Object.keys(buildSyncLedgerFilterDict({ u_source_name: '  ', u_hcm_id: '' }, syncLedgerFieldsFor(ledgerFields))).length === 0,
);
check(
  'entity type labels are humanized',
  entityTypeLabel('EMPLOYEE') === '人员' && entityTypeLabel('POSITION') === '岗位' && entityTypeLabel('WEIRD') === 'WEIRD',
);
check('is_deleted has static options', staticSyncSelectOptions('u_is_deleted').length === 2);

console.log('parseGroupCount (hcm.model.count group_by)');
const gc = parseGroupCount(
  { result: { list: [
    { 'count__*': 6758, u_entity_type: 'EMPLOYEE', '*': 6758 },
    { 'count__*': 1741, u_entity_type: 'POSITION' },
    { 'count__*': 1, u_entity_type: 'ORG' },
  ] } },
  'u_entity_type',
);
check('parses {result:{list}} and sorts by count desc', gc.map((x) => x.value).join(',') === 'EMPLOYEE,POSITION,ORG');
check('keeps counts', gc[0].count === 6758);
check(
  'handles double-nested result and skips null values',
  parseGroupCount({ result: { result: { list: [{ 'count__*': 3, u_entity_type: 'DEPT' }, { u_entity_type: null }] } } }, 'u_entity_type')
    .map((x) => x.value)
    .join(',') === 'DEPT',
);
check('non-array response yields empty', parseGroupCount(null, 'u_entity_type').length === 0);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
