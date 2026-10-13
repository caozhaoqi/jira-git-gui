// Regression tests for model-aware HCM filter_dict construction.
import {
  buildHcmFilterDict,
  defaultHcmTextFilterField,
  metaFilterCandidates,
  pickDefaultFilterKey,
  queryFieldCandidates,
  selectHcmFilterFields,
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

console.log('queryFieldCandidates');
const qresp = [
  {
    key: 'common_used',
    name: '常用',
    info_fields: [{ key: 'name', label: '姓名', component: 'hc-input-text' }],
    list_fields: [{ key: 'employee_no', label: '工号', component: 'hc-input-text', data_type: 'string' }],
  },
  { key: 'base', name: '基本信息', info_fields: [{ key: 'id', label: 'ID', component: 'hc-input-number' }] },
];
const qcands = queryFieldCandidates(qresp);
check('flattens grouped query.fields response', qcands.map((c) => c.key).join(',') === 'name,employee_no,id');
check('flatten keeps labels', qcands[0].label === '姓名' && qcands[1].label === '工号');
check('picks a text candidate as default', pickDefaultFilterKey(qcands) === 'name');
check('dedupes repeated keys', queryFieldCandidates([{ key: 'x' }, { key: 'x', label: 'dup' }]).length === 1);
check('accepts flat field arrays', queryFieldCandidates([{ key: 'a', name: 'A' }, { no_key: 1 }]).length === 1);
check('unwraps object wrapper (data/list/fields...)', queryFieldCandidates({ data: [{ key: 'a', label: 'A' }] }).map((c) => c.key).join(',') === 'a');
check('non-array response yields empty', queryFieldCandidates(null).length === 0);
check('group objects are not mistaken for fields', queryFieldCandidates([{ key: 'g', name: '组', info_fields: [{ key: 'f' }] }]).some((c) => c.key === 'g') === false);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
