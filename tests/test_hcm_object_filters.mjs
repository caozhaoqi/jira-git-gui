// Regression tests for model-aware HCM filter_dict construction.
import {
  buildHcmFilterDict,
  buildSyncLedgerCsv,
  buildSyncLedgerFilterDict,
  buildSyncLedgerJson,
  csvEscape,
  defaultHcmTextFilterField,
  entityTypeLabel,
  exportFileName,
  fetchAllRows,
  isSyncLedgerModel,
  metaFilterCandidates,
  parseGroupCount,
  parseTotalCount,
  pickDefaultFilterKey,
  selectHcmFilterFields,
  staticSyncSelectOptions,
  syncLedgerExportColumns,
  syncLedgerFieldsFor,
  toCsvText,
  SYNC_COPY_MAX_ROWS,
  SYNC_EXPORT_MAX_ROWS,
  SYNC_LEDGER_FIELDS,
} from '../frontend/web-react/src/utils/hcmObjectFilters.ts';
// 三份字典只做类型 import，run 时可被 Node 的类型擦除直接加载（零运行时依赖）。
const { zh } = await import('../frontend/web-react/src/i18n/zh.ts');
const { en } = await import('../frontend/web-react/src/i18n/en.ts');
const { ja } = await import('../frontend/web-react/src/i18n/ja.ts');

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
check(
  'static options carry an i18n labelKey (component falls back to the Chinese label)',
  staticSyncSelectOptions('u_is_deleted').every((o) => typeof o.labelKey === 'string' && o.labelKey.startsWith('hcm.')),
);
check('unknown select fields have no static options', staticSyncSelectOptions('u_interface_code').length === 0);

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

// --------------------------------------------------------------------------- //
//  台帐导出：列规划 / CSV / JSON / 跨页拉全
// --------------------------------------------------------------------------- //
console.log('parseTotalCount (hcm.model.count count_by [*])');
check('reads count__* from flat list', parseTotalCount({ list: [{ 'count__*': 675, '*': 675 }] }) === 675);
check('reads count from nested result', parseTotalCount({ result: { list: [{ 'count__*': 93895 }] } }) === 93895);
check('falls back to * when count__* missing', parseTotalCount({ list: [{ '*': 42 }] }) === 42);
check('empty / bad responses yield 0', parseTotalCount(null) === 0 && parseTotalCount({ list: [] }) === 0);

console.log('syncLedgerExportColumns');
const exportMeta = [
  { key: 'id', name: 'id', type: 'integer', is_list: false, is_info: false },
  { key: 'company_id', name: '企业ID', type: 'integer', is_list: false, is_info: false },
  { key: 'u_raw_json', name: '原始报文', type: 'JSON', is_list: true, is_info: true },
  { key: 'u_entity_type', name: '实体类型', type: 'string', is_list: true, is_info: true },
  { key: 'u_source_name', name: '源名称', type: 'string', is_list: true, is_info: true },
];
const exportRows = [
  { u_raw_json: { a: 1 }, u_entity_type: 'EMPLOYEE', u_source_name: '张波', id: 1 },
  { u_raw_json: { a: 2 }, u_entity_type: 'EMPLOYEE', u_source_name: '张科', id: 2 },
];
const cols = syncLedgerExportColumns(exportMeta, exportRows);
check(
  'business columns come first in meta order, then id',
  cols.map((c) => c.key).join(',') === 'u_raw_json,u_entity_type,u_source_name,id',
);
check('uses the Chinese field label as header', cols.find((c) => c.key === 'u_source_name')?.label === '源名称');
check(
  'exclude drops the huge raw-payload column',
  syncLedgerExportColumns(exportMeta, exportRows, { exclude: ['u_raw_json'] })
    .map((c) => c.key)
    .join(',') === 'u_entity_type,u_source_name,id',
);
check(
  'framework columns absent from rows are not invented',
  !syncLedgerExportColumns(exportMeta, exportRows).some((c) => c.key === 'company_id'),
);

console.log('csvEscape / toCsvText');
check('plain values pass through', csvEscape('张波') === '张波' && csvEscape(12) === '12');
check('null / undefined become empty cells', csvEscape(null) === '' && csvEscape(undefined) === '');
check('commas and quotes are quoted + doubled', csvEscape('a,b') === '"a,b"' && csvEscape('say "hi"') === '"say ""hi"""');
check('embedded newlines stay inside one quoted cell', csvEscape('l1\nl2') === '"l1\nl2"');
check('objects are JSON-encoded', csvEscape({ a: 1 }) === '"{""a"":1}"');
check('rows join with CRLF', toCsvText(['a', 'b'], [[1, 2], [3, 4]]) === 'a,b\r\n1,2\r\n3,4');

console.log('buildSyncLedgerCsv / buildSyncLedgerJson');
const csv = buildSyncLedgerCsv(exportMeta, exportRows, { exclude: ['u_raw_json'] });
check('CSV starts with UTF-8 BOM for Excel', csv.startsWith('\ufeff'));
check('CSV header uses labels and excludes raw payload', csv.replace(/^\ufeff/, '').split('\r\n')[0] === '实体类型,源名称,id');
check('CSV keeps one line per row', csv.trim().split('\r\n').length === 3);
check('CSV carries the data', csv.includes('张波') && csv.includes('EMPLOYEE'));
// 复制到剪贴板走 bom:false——BOM 会被粘成一个乱码字符
const csvNoBom = buildSyncLedgerCsv(exportMeta, exportRows, { exclude: ['u_raw_json'], bom: false });
check('bom:false omits the BOM (clipboard path)', !csvNoBom.startsWith('\ufeff') && csvNoBom.startsWith('实体类型'));
check(
  'bom:false keeps the same content as the file version',
  csvNoBom === csv.replace(/^\ufeff/, ''),
);

const jsonText = buildSyncLedgerJson({
  model: 'U_SyncRawGdyy',
  filter: { u_entity_type: 'EMPLOYEE', u_source_name: { like: '张' } },
  total: 675,
  rows: exportRows,
  exportedAt: new Date('2026-09-23T05:47:00Z'),
});
const parsedJson = JSON.parse(jsonText);
check('JSON export records the filter that produced it', parsedJson.filter_dict.u_source_name.like === '张');
check('JSON export records total + row_count', parsedJson.total === 675 && parsedJson.row_count === 2);
check('JSON export keeps the rows verbatim', parsedJson.rows[0].u_source_name === '张波');
check('JSON export stamps the model', parsedJson.model === 'U_SyncRawGdyy');
const jsonTrimmed = JSON.parse(
  buildSyncLedgerJson({ model: 'M', filter: {}, total: 2, rows: exportRows, exclude: ['u_raw_json'] }),
);
check(
  'JSON exclude drops the raw-payload column too (same switch as CSV)',
  jsonTrimmed.rows.every((r) => !('u_raw_json' in r)) && jsonTrimmed.excluded_fields.join(',') === 'u_raw_json',
);
check('JSON exclude does not mutate the source rows', 'u_raw_json' in exportRows[0]);

console.log('exportFileName');
check(
  'stamps model + local time',
  exportFileName('U_SyncRawGdyy', 'csv', new Date(2026, 8, 23, 13, 47, 5)) === 'U_SyncRawGdyy_20260923_134705.csv',
);
check('sanitizes unsafe model chars', exportFileName('a/b c', 'json', new Date(2026, 0, 2, 3, 4, 5)) === 'a_b_c_20260102_030405.json');

console.log('导出/复制上限常量');
check('copy cap is smaller than export cap', SYNC_COPY_MAX_ROWS < SYNC_EXPORT_MAX_ROWS);

console.log('fetchAllRows');
/** 构造一个假的分页后端：rows 为全量数据，serverCap 模拟服务端把 page_size 收敛到更小值。 */
function fakePager(rows, { serverCap = Infinity, reportCount = true, repeatPage = false } = {}) {
  const calls = [];
  const fetchPage = async (pageIndex, pageSize) => {
    calls.push({ pageIndex, pageSize });
    const size = Math.min(pageSize, serverCap);
    const start = (pageIndex - 1) * size;
    const list = repeatPage && pageIndex === 2 ? rows.slice(0, size) : rows.slice(start, start + size);
    return { list, count: reportCount ? rows.length : undefined };
  };
  return { fetchPage, calls };
}
const mkRows = (n, offset = 0) => Array.from({ length: n }, (_, i) => ({ id: offset + i + 1 }));

const pagerA = fakePager(mkRows(250));
const allA = await fetchAllRows(pagerA.fetchPage, { pageSize: 100 });
check('pulls every page until the last partial page', allA.rows.length === 250);
check('reports server total', allA.total === 250);
check('reports page count', allA.pages === 3);
check('not flagged as truncated', allA.truncated === false);
check('asks for increasing page_index', pagerA.calls.map((c) => c.pageIndex).join(',') === '1,2,3');

const pagerExact = fakePager(mkRows(200));
const allExact = await fetchAllRows(pagerExact.fetchPage, { pageSize: 100 });
check('stops on exact multiple of page size without an extra request', allExact.rows.length === 200 && allExact.pages === 2);

// 服务端把 page_size 收敛到 100：不能因为「首屏 100 < 请求 500」就误判成末页而截断
const pagerCap = fakePager(mkRows(250), { serverCap: 100 });
const allCap = await fetchAllRows(pagerCap.fetchPage, { pageSize: 500 });
check('server-side page_size cap does not truncate the export', allCap.rows.length === 250);
check('adapts the step to the capped size', pagerCap.calls[1].pageSize === 100);

const pagerNoCount = fakePager(mkRows(250), { reportCount: false });
const allNoCount = await fetchAllRows(pagerNoCount.fetchPage, { pageSize: 100 });
check('works without a server count (stops on empty page)', allNoCount.rows.length === 250 && allNoCount.total === 250);

const pagerEmpty = fakePager([]);
const allEmpty = await fetchAllRows(pagerEmpty.fetchPage, { pageSize: 100 });
check('empty result set yields no rows', allEmpty.rows.length === 0 && allEmpty.total === 0);

const pagerMax = fakePager(mkRows(5000));
const allMax = await fetchAllRows(pagerMax.fetchPage, { pageSize: 100, maxRows: 250 });
check('maxRows caps the export', allMax.rows.length === 250 && allMax.truncated === true);
check('maxRows keeps the first rows', allMax.rows[0].id === 1 && allMax.rows[249].id === 250);

const pagerRepeat = fakePager(mkRows(1000), { reportCount: false, repeatPage: true });
const allRepeat = await fetchAllRows(pagerRepeat.fetchPage, { pageSize: 100 });
check('repeated identical page does not loop forever', allRepeat.pages === 2);

const progress = [];
const pagerProg = fakePager(mkRows(250));
await fetchAllRows(pagerProg.fetchPage, {
  pageSize: 100,
  onProgress: (loaded, total) => progress.push(`${loaded}/${total}`),
});
check('reports progress with the total as denominator', progress.join(',') === '100/250,200/250,250/250');

// 台帐筛选面板的 i18n 覆盖：HcmObjectBrowser 按 `hcm.syncFields.<key>` / `hcm.syncPlaceholders.<key>`
// 取展示名与占位符（utils 层必须零运行时依赖，故 key 在组件层拼）。这里锁定
// 「SYNC_LEDGER_FIELDS 里每个字段在三份字典里都有展示名」——防止新增台帐字段只翻了中文。
console.log('台帐筛选字段 i18n 覆盖（zh / en / ja）');
const LOCALE_DICTS = { zh, en, ja };
const dictAt = (dict, path) => path.split('.').reduce((n, k) => (n && typeof n === 'object' ? n[k] : undefined), dict);
for (const f of SYNC_LEDGER_FIELDS) {
  for (const [locale, dict] of Object.entries(LOCALE_DICTS)) {
    const label = dictAt(dict, `hcm.syncFields.${f.key}`);
    check(
      `[${locale}] hcm.syncFields.${f.key} is a non-empty string`,
      typeof label === 'string' && label.trim().length > 0,
    );
    const ph = dictAt(dict, `hcm.syncPlaceholders.${f.key}`);
    if (f.placeholder) {
      check(
        `[${locale}] hcm.syncPlaceholders.${f.key} is a non-empty string`,
        typeof ph === 'string' && ph.trim().length > 0,
      );
    } else {
      check(`[${locale}] hcm.syncPlaceholders.${f.key} absent (no placeholder planned)`, ph === undefined);
    }
  }
}
// 静态 select 选项（未删除/已删除）的 labelKey 三语都要有值
for (const opt of staticSyncSelectOptions('u_is_deleted')) {
  for (const [locale, dict] of Object.entries(LOCALE_DICTS)) {
    const v = dictAt(dict, opt.labelKey);
    check(`[${locale}] ${opt.labelKey} is a non-empty string`, typeof v === 'string' && v.trim().length > 0);
  }
}
// 台帐面板 / 导出工具条的状态文案占位符必须齐全（组件按 {{v}} 插值，缺了会原样吐出 {{v}}）
const PLACEHOLDER_KEYS = [
  ['hcm.payloadParseFailed', ['msg']],
  ['hcm.fetchingRows', ['n']],
  ['hcm.fetchingProgress', ['loaded', 'total']],
  ['hcm.exportedRows', ['n']],
  ['hcm.exportTruncated', ['n']],
  ['hcm.exportFailed', ['msg']],
  ['hcm.copyTruncated', ['n', 'btn']],
  ['hcm.copiedRows', ['n', 'fmt', 'note']],
  ['hcm.copyFailed', ['msg']],
  ['hcm.downloadedFile', ['name', 'kb']],
  ['hcm.downloadFailed', ['msg']],
];
for (const [key, vars] of PLACEHOLDER_KEYS) {
  for (const [locale, dict] of Object.entries(LOCALE_DICTS)) {
    const tpl = dictAt(dict, key);
    const missing = vars.filter((v) => !new RegExp(`\\{\\{\\s*${v}\\s*\\}\\}`).test(tpl || ''));
    check(`[${locale}] ${key} has all placeholders ${vars.join('/')}`, typeof tpl === 'string' && missing.length === 0);
  }
}

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
