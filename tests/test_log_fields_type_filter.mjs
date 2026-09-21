// -*- coding: utf-8 -*-
// 「日志类型」过滤的取值与匹配回归（utils/logFields.ts）—— 锁住「自动刷新让类型过滤失效」的根治点。
//
// 背景：CF 实时刷新（SSE cf_log_update）在后端流的过滤条件是「开启时快照」；若开启时
// log_type 为空/旧值，流会持续推来**其它类型**的日志。前端合并前必须按当前 log_type
// 再过滤一次，否则表现为「自动刷新使日志类型过滤失效」。
//
// 关键语义（都在这里锁住）：
//   * 未设过滤 → 全部保留
//   * 设了过滤 → 按「记录模型对应的类型字段」做大小写无关的包含匹配
//       dynamic_log → log_type；SyncOuterRecord → name（该模型没有 log_type）
//   * 取不到类型字段的行**不武断丢弃**（宁可多留，也不隐藏服务端已返回的行）
//
// 运行：node tests/test_log_fields_type_filter.mjs
// （Node 22.18+ 默认支持 type stripping，可直接 import .ts）
const { modelTypeField, logRowTypeValue, logRowMatchesType } = await import(
  '../frontend/web-react/src/utils/logFields.ts'
);

let failed = 0;
function check(name, cond, extra = '') {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    failed++;
    console.log(`  ✗ ${name} ${extra}`);
  }
}

console.log('modelTypeField：类型字段随记录模型变化');
check('dynamic_log → log_type', modelTypeField('dynamic_log') === 'log_type');
check('SyncOuterRecord → name', modelTypeField('SyncOuterRecord') === 'name');
check('大小写不敏感', modelTypeField('SYNCOUTERRECORD') === 'name');
check('未知/空模型 → log_type 兜底', modelTypeField('') === 'log_type' && modelTypeField('whatever') === 'log_type');

console.log('logRowTypeValue：按模型取行自身的类型值');
check(
  'dynamic_log 取 log_type',
  logRowTypeValue({ log_type: 'want_fn' }, 'dynamic_log') === 'want_fn',
);
check(
  'SyncOuterRecord 取 name（无 log_type）',
  logRowTypeValue({ name: 'ConvGdyyEmp', log_type: 'ignored' }, 'SyncOuterRecord') === 'ConvGdyyEmp',
);
check(
  '字段缺失时走兜底链（camelCase logType）',
  logRowTypeValue({ logType: 'camel_fn' }, 'dynamic_log') === 'camel_fn',
);

console.log('logRowMatchesType：未设过滤 → 全部保留');
check('空过滤保留异类行', logRowMatchesType({ log_type: 'other_fn' }, '') === true);
check('空白过滤保留异类行', logRowMatchesType({ log_type: 'other_fn' }, '   ') === true);

console.log('logRowMatchesType：设了过滤 → 只留匹配行（本 bug 的核心断言）');
check('同类型命中', logRowMatchesType({ log_type: 'want_fn' }, 'want_fn', 'dynamic_log') === true);
check(
  '异类行必须被挡掉（旧流推来的日志）',
  logRowMatchesType({ log_type: 'other_fn' }, 'want_fn', 'dynamic_log') === false,
);
check(
  '大小写无关',
  logRowMatchesType({ log_type: 'Want_FN' }, 'want_fn', 'dynamic_log') === true,
);
check(
  '包含匹配（过滤值是子串）',
  logRowMatchesType({ log_type: 'salary_seal_delay_payment_vvv1' }, 'seal_delay', 'dynamic_log') === true,
);
check(
  'SyncOuterRecord 按 name 过滤命中',
  logRowMatchesType({ name: 'ConvGdyyEmp' }, 'ConvGdyyEmp', 'SyncOuterRecord') === true,
);
check(
  'SyncOuterRecord 按 name 过滤挡掉异类',
  logRowMatchesType({ name: 'OtherSync' }, 'ConvGdyyEmp', 'SyncOuterRecord') === false,
);

console.log('logRowMatchesType：取不到类型字段 → 不丢弃');
check('无任何类型字段时保留', logRowMatchesType({ content: 'x' }, 'want_fn', 'dynamic_log') === true);
check('空字符串类型值保留', logRowMatchesType({ log_type: '' }, 'want_fn', 'dynamic_log') === true);
check('null 行保留', logRowMatchesType(null, 'want_fn', 'dynamic_log') === true);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
