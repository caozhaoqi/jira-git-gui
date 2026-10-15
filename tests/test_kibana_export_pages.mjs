// Kibana 日志下载的分页拉取逻辑（components/kibana/kibanaExport.ts）无头测试。
//
// 为什么需要它：下载是「分页拉全量」，最容易崩的是三条边界——
//   1) 短页（返回数 < 10000）必须判定为末页，否则多打一次空请求、
//      更糟的是 total 多加 0 行但 parts 多一个空串；
//   2) 行数上限必须真的停手——本仓历史上这段逻辑写成「后置判断」，
//      当 total 恰好等于上限时会多拉一页，用户填 10000 就下载到 20000 行；
//   3) 后续页失败要保留已拉到的部分（不能整段丢弃），但首页失败必须抛错
//      （否则用户会拿到一个 0 行的空文件还以为下载成功了）。
//
// 运行：node tests/test_kibana_export_pages.mjs
import { fetchExportContent, EXPORT_PAGE, EXPORT_MAX_ROWS }
  from '../frontend/web-react/src/components/kibana/kibanaExport.ts';

let failed = 0;
function check(name, condition, extra = '') {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}${extra ? ` — ${extra}` : ''}`);
  }
}

const REQ = {
  site: 'prod', start: 'now-1h', end: '', namespace: 'default',
  container: 'app-a', app: 'app-a', host: 'node-1',
  keyword: 'timeout', exclude_keyword: 'healthz', levels: ['ERROR', 'WARN'],
};

/** 造一个假 apiPost，按后端真实语义建模（见 api/kibana/routes_kibana_logs.py:191）：
 *   - 0 行命中 → `{ok:true, content:'', count:0}`（不是错误！）
 *   - 失败 → `{ok:false, error, content:''}`
 *  `pages` = 每页返回行数数组；`opts.infiniteFull` = 每页都拉满（模拟海量日志，
 *  用来验证上限真的能停手）；`opts.failOnCall` = 第几次请求失败（0 = 首次）。 */
function fakePost(pages, opts = {}) {
  const calls = [];
  const impl = async (path, body) => {
    calls.push({ path, body });
    const i = calls.length - 1;
    if (opts.failOnCall !== undefined && i === opts.failOnCall) {
      return { ok: false, error: opts.failMsg || 'backend down', content: '' };
    }
    const n = opts.infiniteFull
      ? EXPORT_PAGE
      : (i < pages.length ? pages[i] : 0);
    return {
      ok: true,
      count: n,
      content: n > 0 ? `page-${i}:${n}` : '',
      filename: i === 0 ? 'kibana-prod.log' : undefined,
    };
  };
  impl.calls = calls;
  return impl;
}

(async () => {
  // ---- 1. 单页短页即结束（最常用路径） ---------------------------------- //
  {
    const post = fakePost([300]);
    const r = await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post);
    check('单页短页：只请求一次', post.calls.length === 1, `calls=${post.calls.length}`);
    check('单页短页：内容与行数正确', r.total === 300 && r.parts.length === 1, `total=${r.total}`);
    check('单页短页：未触顶', !r.capped);
    check('单页短页：带回了文件名', r.filename === 'kibana-prod.log');
    check('单页短页：请求体含筛选条件', post.calls[0].body.keyword === 'timeout'
      && post.calls[0].body.container === 'app-a'
      && post.calls[0].body.size === EXPORT_PAGE
      && post.calls[0].body.from_ === 0);
  }

  // ---- 2. 多页：满页继续 from_ 递进，短页收尾 -------------------------- //
  {
    const post = fakePost([EXPORT_PAGE, EXPORT_PAGE, 250]);
    const r = await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post);
    check('多页：共发 3 次请求', post.calls.length === 3, `calls=${post.calls.length}`);
    check('多页：from_ 依次 0/10000/20000',
      post.calls.map((c) => c.body.from_).join(',') === '0,10000,20000',
      post.calls.map((c) => c.body.from_).join(','));
    check('多页：总行数为三页之和', r.total === EXPORT_PAGE * 2 + 250, `total=${r.total}`);
    check('多页：内容片段数 = 页数', r.parts.length === 3);
    check('多页：未触顶', !r.capped);
  }

  // ---- 3. 上限必须真的停手（回归：total 恰好等于上限时不多拉一页）------- //
  {
    const post = fakePost(null, { infiniteFull: true });
    const r = await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post);
    check('上限=20万：恰好 20 页后停手', post.calls.length === 20, `calls=${post.calls.length}`);
    check('上限=20万：标记 capped', r.capped);
    check('上限=20万：总行数达标', r.total === EXPORT_MAX_ROWS, `total=${r.total}`);

    // 用户自定义上限恰好等于一页：只拉一页、不越界（旧实现会多拉一页）
    const post2 = fakePost(null, { infiniteFull: true });
    const r2 = await fetchExportContent(REQ, EXPORT_PAGE, undefined, post2);
    check('上限=1页：只请求一次', post2.calls.length === 1, `calls=${post2.calls.length}`);
    check('上限=1页：标记 capped', r2.capped);
    check('上限=1页：行数达标', r2.total === EXPORT_PAGE, `total=${r2.total}`);

    // 上限填得比一页还小：仍要放行第一页（否则一页都拉不到）
    const post3 = fakePost(null, { infiniteFull: true });
    const r3 = await fetchExportContent(REQ, 5, undefined, post3);
    check('上限=5行（小于一页）：仍拉到第一页', post3.calls.length === 1 && r3.total === EXPORT_PAGE,
      `calls=${post3.calls.length}`);
    check('上限=5行：标记 capped', r3.capped);
  }

  // ---- 4. 上限夹取（0/NaN/负数/超大）----------------------------------- //
  {
    for (const [bad, desc] of [[0, '0'], [NaN, 'NaN'], [-100, '负数'], [null, 'null']]) {
      const post = fakePost([300]);
      const r = await fetchExportContent(REQ, bad, undefined, post);
      check(`上限=${desc} 时按默认页大小放行`, post.calls.length === 1 && r.total === 300);
    }
    const post = fakePost(null, { infiniteFull: true });
    const r = await fetchExportContent(REQ, 10 ** 9, undefined, post);
    check('上限=10^9 时夹到 20 万', r.capped && r.total === EXPORT_MAX_ROWS && post.calls.length === 20,
      `total=${r.total} calls=${post.calls.length}`);
  }

  // ---- 5. 失败语义：首页抛错、后续页保留部分 ---------------------------- //
  {
    const post = fakePost([EXPORT_PAGE], { failOnCall: 0 });
    let threw = '';
    try {
      await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post);
    } catch (ex) {
      threw = String(ex.message || ex);
    }
    check('首页失败：抛出错误（不产出空文件）', threw.length > 0, `threw=${threw}`);

    const post2 = fakePost([EXPORT_PAGE, 300], { failOnCall: 2 });
    const r2 = await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post2);
    check('后续页失败：保留已拉到的两页', r2.parts.length === 2 && r2.total === EXPORT_PAGE + 300,
      `parts=${r2.parts.length} total=${r2.total}`);
    check('后续页失败：不算 capped', !r2.capped);

    const post3 = fakePost([], { failOnCall: 0, failMsg: 'forbidden' });
    let msg = '';
    try {
      await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post3);
    } catch (ex) {
      msg = String(ex.message || ex);
    }
    check('首页失败：透传后端错误文案', msg.includes('forbidden'), `msg=${msg}`);
  }

  // ---- 6. onProgress 回调每页上报一次累计值 ----------------------------- //
  {
    const post = fakePost([EXPORT_PAGE, 300]);
    const seen = [];
    await fetchExportContent(REQ, EXPORT_MAX_ROWS, (n) => seen.push(n), post);
    check('onProgress：按累计行数上报', seen.join(',') === `${EXPORT_PAGE},${EXPORT_PAGE + 300}`,
      seen.join(','));
  }

  // ---- 7. 0 行命中不是错误（回归：content 为空曾被误判成「导出失败」）--- //
  {
    const post = fakePost([0]);
    const r = await fetchExportContent(REQ, EXPORT_MAX_ROWS, undefined, post);
    check('0 行命中：不抛错', true);
    check('0 行命中：只请求一次（不反复重试）', post.calls.length === 1, `calls=${post.calls.length}`);
    check('0 行命中：total=0 且未触顶', r.total === 0 && !r.capped, `total=${r.total}`);
    check('0 行命中：未带出文件名也合法', r.filename === 'kibana-prod.log');
  }

  console.log(failed === 0 ? '\n全部通过' : `\n✗ ${failed} 项失败`);
  process.exit(failed === 0 ? 0 : 1);
})();
