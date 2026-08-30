// -*- coding: utf-8 -*-
// 前端 K8sFileDownloader（分片下载 / 断点续传）无头测试。
//
// 为什么需要它：用户报的「10MB 只下载了几百 KB」根因在前端（直接把截断后的
// editContent 塞进 Blob），后端加接口只是前提，真正要保证的是：
//   * offset 只在成功接到数据后才前进（失败重试不会重来已下载的部分）
//   * 所有分片按序拼接后与源文件逐字节一致
//   * 暂停/继续不会丢已下载的字节，取消会立刻中断
// 这些逻辑在浏览器里很难稳定复现，所以用 esbuild 把 TS 打成 CJS，
// 再用 stub 的 fetch 在本地跑一遍。
//
// 运行：node tests/test_k8s_file_download_dl.js
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const FE = path.join(ROOT, 'frontend', 'web-react');
const ENTRY = path.join(FE, 'src', 'utils', 'k8sFileDownload.ts');

function build() {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'dl-')), 'k8sFileDownload.cjs');
  const esbuild = path.join(FE, 'node_modules', '.bin', 'esbuild');
  execFileSync(esbuild, [
    ENTRY, '--bundle', '--format=cjs', '--platform=node',
    '--outfile=' + out, '--log-level=error',
  ], { stdio: ['ignore', 'ignore', 'inherit'] });
  return out;
}

const mod = require(build());
const { K8sFileDownloader, base64ToBytes, fmtSpeed, fmtEta, DEFAULT_CHUNK_SIZE } = mod;

// ---- 断言小工具 ------------------------------------------------------------ //
let failed = 0;
function check(cond, msg) {
  if (cond) {
    console.log('[OK] ' + msg);
  } else {
    failed++;
    console.log('[FAIL] ' + msg);
  }
}

// ---- 假服务端 -------------------------------------------------------------- //
/**
 * @param {Buffer} file 源文件
 * @param {object} opts
 *   - failAt: Set<number>  指定第几片（0-based）要失败一次
 *   - delay: number        每片人为延迟毫秒数（太快的假服务会让「暂停/取消」
 *                          还没触发下载就跑完了，测不出真实行为）
 */
function makeFetch(file, opts = {}) {
  const calls = [];
  const failAt = opts.failAt || new Set();
  const delay = opts.delay || 0;
  const failedOnce = new Set();

  return async function fakeFetch(url, init) {
    const body = JSON.parse(init.body);
    if (String(url).includes('/file/stat')) {
      return json({ ok: true, size: file.length, mtime: 1700000000 });
    }
    if (String(url).includes('/file/download')) {
      calls.push({ offset: body.offset, length: body.length });
      const idx = calls.length - 1;
      if (delay) await new Promise((r) => setTimeout(r, delay));
      if (failAt.has(idx) && !failedOnce.has(idx)) {
        failedOnce.add(idx);
        return json({ ok: false, error: '模拟 exec 抖动' }, 500);
      }
      const slice = file.subarray(body.offset, body.offset + body.length);
      return json({
        ok: true,
        data: slice.toString('base64'),
        offset: body.offset,
        length: slice.length,
        requested: body.length,
        eof: slice.length < body.length,
      });
    }
    throw new Error('未预期的 URL: ' + url);
  };

  function json(obj, status = 200) {
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: '',
      text: async () => JSON.stringify(obj),
    };
  }
}

function md5(buf) {
  return crypto.createHash('md5').update(buf).digest('hex');
}

async function collect(dl) {
  const blob = await dl.run();
  if (!blob) return null;
  return Buffer.from(await blob.arrayBuffer());
}

// ---- 用例 ------------------------------------------------------------------ //
(async function main() {
  // 1. 10MB 完整下载：重组后必须与源文件逐字节一致
  {
    const file = crypto.randomBytes(10 * 1024 * 1024);
    globalThis.fetch = makeFetch(file);
    const prog = [];
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/big.bin' },
      { onProgress: (p) => prog.push({ ...p }) },
    );
    const got = await collect(dl);
    check(got && got.length === file.length, `10MB 全量下载长度一致 (${got && got.length})`);
    check(got && md5(got) === md5(file), '10MB 全量下载 md5 一致（分片无丢失/无乱序）');
    const last = prog[prog.length - 1];
    check(last.status === 'done', '结束时状态为 done');
    check(last.percent === 100, `结束时百分比为 100%（实际 ${last.percent}）`);
    check(last.chunkTotal === Math.ceil(file.length / DEFAULT_CHUNK_SIZE), '分片总数计算正确');
    check(
      prog.slice(0, -1).every((p, i) => i === 0 || p.received >= prog[i - 1].received),
      'received 单调不减（进度条不会回退）',
    );
    console.log(`     请求分片数=${last.chunkIndex}，单片=${DEFAULT_CHUNK_SIZE}`);
  }

  // 2. 断点续传：第 2、4 片各失败一次，offset 不能回退，内容仍完整
  {
    // 5MB+123 → 按 1MB 分片共 6 片（下标 0..5），让第 2、4 片各抖一次
    const file = crypto.randomBytes(5 * 1024 * 1024 + 123);
    globalThis.fetch = makeFetch(file, { failAt: new Set([2, 4]) });
    const prog = [];
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/m.bin' },
      { maxRetries: 3, onProgress: (p) => prog.push({ ...p }) },
    );
    const got = await collect(dl);
    check(got && md5(got) === md5(file), '失败后重试：内容仍与源文件一致（断点续传生效）');
    const last = prog[prog.length - 1];
    check(last.retries === 2, `重试次数统计正确（期望 2，实际 ${last.retries}）`);
    check(last.status === 'done', '重试后最终状态为 done');
    const retried = prog.filter((p) => /重试中/.test(p.message || ''));
    check(retried.length === 2, `失败时给出重试提示（${retried.length} 次）`);
    console.log(`     重试提示样例：${(retried[0] || {}).message}`);
  }

  // 3. 重试耗尽 → 失败，且返回 null
  {
    const file = crypto.randomBytes(2 * 1024 * 1024);
    const alwaysFail = async () => ({
      ok: false, status: 500, statusText: '',
      text: async () => JSON.stringify({ ok: false, error: 'Pod 不可达' }),
    });
    globalThis.fetch = async (url) =>
      String(url).includes('/stat')
        ? { ok: true, status: 200, statusText: '', text: async () => JSON.stringify({ ok: true, size: file.length }) }
        : alwaysFail();
    const prog = [];
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/x.bin' },
      { maxRetries: 2, onProgress: (p) => prog.push({ ...p }) },
    );
    const got = await collect(dl);
    check(got === null, '重试耗尽后返回 null（不会产出残缺文件）');
    const last = prog[prog.length - 1];
    check(last.status === 'error', '重试耗尽后状态为 error');
    check(/Pod 不可达/.test(last.message || ''), `错误信息带上后端原因：${last.message}`);
  }

  // 4. 暂停 / 继续：暂停时不再发请求，继续后从原 offset 接着下，内容完整
  {
    const CHUNK = 256 * 1024;
    const file = crypto.randomBytes(3 * 1024 * 1024); // 12 片
    const fake = makeFetch(file, { delay: 20 });
    const offsets = [];
    globalThis.fetch = async (url, init) => {
      if (String(url).includes('/download')) offsets.push(JSON.parse(init.body).offset);
      return fake(url, init);
    };
    const prog = [];
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/p.bin' },
      { chunkSize: CHUNK, onProgress: (p) => prog.push({ ...p }) },
    );
    const p = dl.run();
    await new Promise((r) => setTimeout(r, 90)); // 约下了 4 片
    dl.pause();
    await new Promise((r) => setTimeout(r, 30)); // 让在途那一片落定
    const pausedAt = offsets.length;
    await new Promise((r) => setTimeout(r, 120));
    check(pausedAt > 0 && pausedAt < 12, `确实停在中途（已请求 ${pausedAt}/12 片）`);
    check(offsets.length <= pausedAt + 1, `暂停后不再继续取片（${pausedAt} → ${offsets.length}）`);
    const pausedFrame = prog[prog.length - 1];
    check(pausedFrame.status === 'paused', `暂停时状态为 paused（实际 ${pausedFrame.status}）`);
    dl.resume();
    const blob = await p;
    const buf = blob ? Buffer.from(await blob.arrayBuffer()) : null;
    check(buf && md5(buf) === md5(file), '继续后内容仍与源文件一致（未丢已下载字节）');
    check(offsets[pausedAt] === pausedAt * CHUNK, `继续后从暂停处的 offset 接着请求（${offsets[pausedAt]}）`);
  }

  // 5. 取消：立刻中断并返回 null
  {
    const file = crypto.randomBytes(8 * 1024 * 1024); // 128KB × 64 片
    globalThis.fetch = makeFetch(file, { delay: 20 });
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/c.bin' },
      { chunkSize: 128 * 1024 },
    );
    const p = dl.run();
    await new Promise((r) => setTimeout(r, 90)); // 下载途中
    dl.cancel();
    const blob = await p;
    check(blob === null, '取消后返回 null（不落盘半成品）');
    check(dl.isActive === false, '取消后 isActive 为 false');
  }

  // 6. 工具函数
  {
    const raw = Buffer.from([0, 1, 127, 128, 254, 255, 65, 10, 13, 0]);
    const back = base64ToBytes(raw.toString('base64'));
    check(Buffer.from(back).equals(raw), 'base64ToBytes 二进制往返一致（含 NUL / 高位字节）');
    check(fmtSpeed(2048) === '2.0 KB/s', `fmtSpeed(2048) = ${fmtSpeed(2048)}`);
    check(fmtSpeed(0) === '—', `fmtSpeed(0) = ${fmtSpeed(0)}`);
    check(fmtEta(0, 1024 * 1024, 1024 * 1024) === '1s', `fmtEta 1MB@1MB/s = ${fmtEta(0, 1024 * 1024, 1024 * 1024)}`);
    check(fmtEta(0, 90 * 1024 * 1024, 1024 * 1024) === '1m30s', `fmtEta 90s = ${fmtEta(0, 90 * 1024 * 1024, 1024 * 1024)}`);
  }

  // 7. stat 失败时退化为「未知大小」，但仍要把文件下完
  {
    const file = crypto.randomBytes(1024 * 1024 + 7);
    globalThis.fetch = async (url, init) => {
      if (String(url).includes('/stat')) {
        return { ok: false, status: 500, statusText: '', text: async () => JSON.stringify({ ok: false, error: 'stat 炸了' }) };
      }
      const body = JSON.parse(init.body);
      const slice = file.subarray(body.offset, body.offset + body.length);
      return {
        ok: true, status: 200, statusText: '',
        text: async () => JSON.stringify({
          ok: true, data: slice.toString('base64'),
          offset: body.offset, length: slice.length, eof: slice.length < body.length,
        }),
      };
    };
    const prog = [];
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/u.bin' },
      { onProgress: (p) => prog.push({ ...p }) },
    );
    const got = await collect(dl);
    check(got && md5(got) === md5(file), 'stat 失败也能下完（退化为未知大小）');
    check(prog[0].percent === -1, 'stat 失败时 percent 为 -1（走不确定态进度条）');
  }

  // 8. 超大文件：命中内存上限时提前失败，不把标签页撑爆
  {
    const fakeSize = 4 * 1024 * 1024 * 1024; // 谎报 4GB，实际不会去取任何分片
    globalThis.fetch = async (url) =>
      String(url).includes('/stat')
        ? { ok: true, status: 200, statusText: '', text: async () => JSON.stringify({ ok: true, size: fakeSize }) }
        : (() => { throw new Error('超限后不应再取分片'); })();
    const prog = [];
    const dl = new K8sFileDownloader(
      { env: 'dev', pod: 'p1', path: '/tmp/huge.log' },
      { maxSize: 1024 * 1024 * 1024, onProgress: (p) => prog.push({ ...p }) },
    );
    const blob = await dl.run();
    check(blob === null, '超过内存上限时返回 null（不尝试下载）');
    const last = prog[prog.length - 1];
    check(last.status === 'error', `超限时状态为 error（实际 ${last.status}）`);
    check(/上限/.test(last.message || ''), `超限提示清晰：${last.message}`);
  }

  console.log('');
  if (failed) {
    console.log(`FAILED: ${failed} 个断言未通过`);
    process.exit(1);
  }
  console.log('ALL K8S CHUNKED DOWNLOAD TESTS PASSED');
})();
