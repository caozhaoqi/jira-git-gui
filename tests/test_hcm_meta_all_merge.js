// 回归测试：HCM 元数据“全部”加载问题
// 复现网关行为：biz_type 为空/缺失时只返回 1 条默认布局；指定具体 biz_type 才返回该类型多条。
// 验证修复：选择“全部”时按各已知 biz_type 分别拉取并合并去重，结果应为各类型并集（>10 条），
//           而非单次空 biz_type 查询的 1 条。
const assert = require('assert');

const KNOWN_BIZ_TYPES = ['list', 'info', 'view', 'base', 'panel', 'dataset'];

// 模拟 HCM 网关 hcm.paas.object.layout.list
function mockGateway(params) {
  const bt = params && params.filter_dict ? params.filter_dict.biz_type : (params && params.biz_type);
  if (!bt || bt === '' || bt === 'all') {
    // 空 / 缺失 biz_type：网关只返回 1 条默认布局
    return { list: [{ id: 'default-layout', name: 'default.layout.json', type: 'SYSTEM' }], count: 1 };
  }
  const sizes = { list: 12, info: 8, view: 6, base: 5, panel: 3, dataset: 2 };
  const n = sizes[bt] || 0;
  const list = [];
  for (let i = 0; i < n; i++) {
    list.push({ id: `${bt}-${i}`, name: `Employee.meta.${bt}.item${i}.json`, type: 'SYSTEM' });
  }
  return { list, count: n };
}

// 复刻 HcmMetaFileBrowser 的 loadFiles 合并逻辑
async function loadMetaFiles(bizType, gateway) {
  const typesToQuery = bizType ? [bizType] : KNOWN_BIZ_TYPES;
  const fetchOne = async (bt) => {
    const res = gateway({
      filter_dict: { model: 'Employee', biz_type: bt },
      biz_type: bt,
    });
    return res.list || [];
  };
  const chunks = await Promise.all(typesToQuery.map(fetchOne));
  const seen = new Set();
  const list = [];
  for (const chunk of chunks) {
    for (const it of chunk) {
      const key = it.id ?? it.name ?? it.key ?? JSON.stringify(it);
      if (seen.has(key)) continue;
      seen.add(key);
      list.push(it);
    }
  }
  return list;
}

(async () => {
  // 1) 原 bug：单次空 biz_type 查询只返回 1 条（复现“选择全部加载1条”）
  const singleEmpty = mockGateway({ filter_dict: { model: 'Employee', biz_type: '' } });
  assert.strictEqual(singleEmpty.list.length, 1, '空 biz_type 网关应只返回 1 条（复现原始 bug 行为）');

  // 2) 指定 list 应返回 10+ 条（复现“选择list加载10条以上”）
  const listOnly = await loadMetaFiles('list', mockGateway);
  assert.ok(listOnly.length >= 10, `选择 list 应 >=10 条，实际 ${listOnly.length}`);

  // 3) 选择“全部”合并后应返回各类型并集（远大于 1，且 > list 的 10+）
  const all = await loadMetaFiles('', mockGateway);
  assert.ok(all.length > listOnly.length, `“全部”应多于仅 list（${all.length} > ${listOnly.length}）`);
  assert.strictEqual(all.length, 12 + 8 + 6 + 5 + 3 + 2, `“全部”并集应为 36 条，实际 ${all.length}`);

  // 4) 去重：同一 key 不应重复
  const keys = all.map((it) => it.id);
  assert.strictEqual(new Set(keys).size, keys.length, '合并结果不应有重复 key');

  console.log('ALL HCM META ALL-MERGE TESTS PASSED');
  console.log(`  - 空 biz_type 单次查询: 1 条 (原始 bug)`);
  console.log(`  - 选择 list: ${listOnly.length} 条`);
  console.log(`  - 选择全部(合并): ${all.length} 条 (各类型并集)`);
})().catch((e) => {
  console.error('TEST FAILED:', e.message);
  process.exit(1);
});
