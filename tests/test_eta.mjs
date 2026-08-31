// -*- coding: utf-8 -*-
// ETA 估算回归测试（utils/eta.ts）——差异扫描「倒计时不准确 / 越走越长」的根治验证。
//
// 旧实现的两个毛病，这里各有一条测试锁住：
//   1. 速率用「任务开始至今的累计平均」→ 速率一变就严重滞后（前快后慢时低估 10 倍）。
//   2. 拿「目录进度比例」除「已扫文件数」反推总量 → 单位不一致，文件分布不均时
//      总量估值暴涨（表现为剩余时间越走越长）。
//
// 运行：node tests/test_eta.mjs
// （Node 22.18+ 默认支持 type stripping，可直接 import .ts）

const { EtaTracker, formatEta } = await import(
  '../frontend/web-react/src/utils/eta.ts'
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
function near(a, b, tol) {
  return Math.abs(a - b) <= tol;
}

// --------------------------------------------------------------- 旧实现复刻（用于对照）
/** 旧 etaFromTotal 的速率算法：累计平均 */
function legacyRate(d0, done, t0, now) {
  const elapsed = (now - t0) / 1000;
  return (done - d0) / elapsed;
}
/** 旧扫描 ETA 的总量反推：文件数 ÷ 目录进度比例（单位混用的根源） */
function legacyScanTotalEst(filesDone, frac) {
  const f = Math.min(0.999, Math.max(0.001, frac));
  return filesDone / f;
}

console.log('formatEta 格式化');
{
  check('0 / 负数返回空串（不显示）', formatEta(0) === '' && formatEta(-5) === '');
  check('非有限值返回空串', formatEta(NaN) === '' && formatEta(Infinity) === '');
  check('0.4s → 不到 1 秒', formatEta(0.4) === '不到 1 秒', formatEta(0.4));
  check('45s → 45 秒', formatEta(45) === '45 秒', formatEta(45));
  check('95s → 1 分 35 秒', formatEta(95) === '1 分 35 秒', formatEta(95));
  check('3600s → 1 时（整除不显示 0 分）', formatEta(3600) === '1 时', formatEta(3600));
  check('3725s → 1 时 2 分', formatEta(3725) === '1 时 2 分', formatEta(3725));
}

console.log('\n不输出不可靠的估算');
{
  const t = new EtaTracker();
  check('未 reset 直接调用 → null', t.etaFromTotal(0, 100, 0) === null);

  t.reset(0, 0);
  // 只喂 2 个样本（minSamples=3）
  t.etaFromTotal(10, 1000, 500);
  const tooFew = t.etaFromTotal(20, 1000, 1000);
  check('样本数不足 → null', tooFew === null, String(tooFew));

  // 样本够了但还在预热期内（warmupMs=1500）
  const t2 = new EtaTracker();
  t2.reset(0, 0);
  t2.etaFromTotal(10, 1000, 300);
  t2.etaFromTotal(20, 1000, 600);
  const warming = t2.etaFromTotal(30, 1000, 900);
  check('预热期内 → null', warming === null, String(warming));

  const t3 = new EtaTracker();
  t3.reset(0, 0);
  for (let i = 1; i <= 5; i++) t3.etaFromTotal(i * 10, 1000, i * 400);
  check('过了预热且有样本 → 给出估算', typeof t3.etaFromTotal(50, 1000, 2000) === 'number');

  const t4 = new EtaTracker();
  t4.reset(0, 0);
  for (let i = 1; i <= 5; i++) t4.etaFromTotal(i * 10, 1000, i * 400);
  check('done >= total → null（不返回负数/0）', t4.etaFromTotal(1000, 1000, 2000) === null);
  check('total <= 0 → null', t4.etaFromTotal(10, 0, 2000) === null);
}

console.log('\n匀速场景：估算接近真值');
{
  const t = new EtaTracker();
  t.reset(0, 0);
  // 每秒 50 个，总量 1000 → 20s 完成；t=10s 时剩余 500，真值 10s
  let eta = null;
  for (let i = 1; i <= 10; i++) eta = t.etaFromTotal(i * 50, 1000, i * 1000);
  check('匀速下 ETA 接近 10s', eta != null && near(eta, 10, 1.5), `eta=${eta}`);
}

console.log('\n核心回归：前快后慢时不再严重低估（旧实现的最大毛病）');
{
  // 场景：前 10s 缓存命中扫了 900 个文件（90/s），后 10s 撞上慢目录只扫了 50 个（5/s）。
  // t=20s 时 done=950，剩余 50，真实剩余 = 50/5 = 10s。
  const t = new EtaTracker();
  t.reset(0, 0);
  let eta = null;
  for (let i = 1; i <= 10; i++) eta = t.etaFromTotal(i * 90, 1000, i * 1000);
  for (let i = 1; i <= 10; i++) {
    eta = t.etaFromTotal(900 + i * 5, 1000, (10 + i) * 1000);
  }
  const legacy = (1000 - 950) / legacyRate(0, 950, 0, 20000);
  console.log(`    EMA=${eta?.toFixed(2)}s  旧累计平均=${legacy.toFixed(2)}s  真值=10s`);
  check('EMA 明显优于旧累计平均（旧值严重低估）', eta > legacy * 3, `${eta} vs ${legacy}`);
  check('EMA 更接近真值 10s', Math.abs(eta - 10) < Math.abs(legacy - 10), `${eta} vs ${legacy}`);
  check('EMA 不低估到 1s 量级', eta > 4, `eta=${eta}`);
}

console.log('\n限幅：速率突变时 ETA 不跳变（防「越走越长」乱跳）');
{
  const jump = 1.6;
  const t = new EtaTracker({ maxRateJump: jump, warmupMs: 0, minSamples: 2 });
  t.reset(0, 0);
  // 先跑一段快的：100/s
  let prev = null;
  for (let i = 1; i <= 6; i++) prev = t.etaFromTotal(i * 100, 10000, i * 1000);
  // 突然几乎停滞：每步只前进 1 个
  let maxJump = 0;
  let cur = prev;
  for (let i = 1; i <= 6; i++) {
    const next = t.etaFromTotal(600 + i, 10000, (6 + i) * 1000);
    if (next != null && cur != null && cur > 0) {
      maxJump = Math.max(maxJump, next / cur);
    }
    cur = next;
  }
  console.log(`    停滞时单步最大放大=${maxJump.toFixed(2)}x (速率限幅上限 ${jump}x)`);
  check('单步 ETA 放大不超过速率限幅上限', maxJump <= jump + 1e-9, `${maxJump}`);
  check('停滞时 ETA 确实在上升（诚实而非假装正常）', cur > prev * 2, `${cur} vs ${prev}`);
}

console.log('\n恒速场景：ETA 随剩余量线性下降，不被限幅拖住');
{
  // 这条专门守住「限幅施加在速率上、不在 ETA 上」这个设计决定：
  // 早期版本直接对 ETA 做百分比限幅，会把恒速下正常的线性下降误判成跳变并拦住，
  // 结果是越到后面越滞后（真值 41s 却显示 71s）。
  const t = new EtaTracker({ warmupMs: 0, minSamples: 2 });
  t.reset(0, 0);
  let e10 = null;
  for (let i = 1; i <= 10; i++) e10 = t.etaFromTotal(i * 100, 24145, i * 1000);
  let e100 = null;
  for (let i = 11; i <= 100; i++) e100 = t.etaFromTotal(i * 100, 24145, i * 1000);
  let e200 = null;
  for (let i = 101; i <= 200; i++) e200 = t.etaFromTotal(i * 100, 24145, i * 1000);
  // 恒速 100/s，真值：t=10s→231s  t=100s→141s  t=200s→41s
  console.log(
    `    t=10s→${e10?.toFixed(0)}s  t=100s→${e100?.toFixed(0)}s  ` +
      `t=200s→${e200?.toFixed(0)}s（真值 231 / 141 / 41s）`
  );
  check('恒速下 ETA 单调递减', e10 > e100 && e100 > e200, `${e10}/${e100}/${e200}`);
  check('恒速下 ETA 贴合真值（不被限幅拖住）',
    near(e10, 231, 3) && near(e100, 141, 3) && near(e200, 41, 3),
    `${e10?.toFixed(1)} / ${e100?.toFixed(1)} / ${e200?.toFixed(1)}`);
}

console.log('\n单位一致性：总量估值的两种算法对比（旧扫描 ETA 失真复现）');
{
  // 真实仓库文件分布极不均匀：根下 30 个目录，29 个小目录各 5 个文件，
  // 最后 1 个大目录独占 24000 个文件（占全仓 99%）。大目录最后才轮到。
  const TRUE_TOTAL = 145 + 24000;
  const filesAt = (dirsDone) => (dirsDone <= 29 ? dirsDone * 5 : TRUE_TOTAL);
  const fracAt = (dirsDone) => dirsDone / 30;

  // 旧模型：拿「目录进度比例」去除「已扫文件数」反推总量（单位混用）
  const estAt29 = legacyScanTotalEst(filesAt(29), fracAt(29));
  const estAt30 = legacyScanTotalEst(filesAt(30), fracAt(30));
  console.log(
    `    目录进度 97%（29/30）时：已扫 ${filesAt(29)} 文件，` +
      `旧模型估总量=${estAt29.toFixed(0)}，真实=${TRUE_TOTAL}`
  );
  console.log(
    `    扫完最后一个大目录：旧模型估总量跳到 ${estAt30.toFixed(0)}` +
      `（一步暴涨 ${(estAt30 / estAt29).toFixed(0)} 倍）`
  );
  check('旧模型在目录进度 97% 时低估总量两个数量级', estAt29 < TRUE_TOTAL / 50,
    `估=${estAt29.toFixed(0)} 真=${TRUE_TOTAL}`);
  check('旧模型此时会谎报「几乎扫完」', estAt29 - filesAt(29) < 20,
    `剩余估值=${(estAt29 - filesAt(29)).toFixed(0)}，实际还剩 ${TRUE_TOTAL - filesAt(29)}`);
  check('旧模型总量估值最后一步暴涨 50 倍以上', estAt30 > estAt29 * 50);

  // 新模型：总量用本地实测文件数（与 done 同单位），全程恒定
  const t = new EtaTracker({ warmupMs: 0, minSamples: 2 });
  t.reset(0, 0);
  let etaBig = null;
  // 开始扫那个大目录：1200 文件/秒，一直扫到接近扫完（剩 1200 个时停）
  const BIG_RATE = 1200;
  const SAMPLES = Math.floor((TRUE_TOTAL - 145) / BIG_RATE) - 1; // 19 次，末尾留 1200 个
  for (let i = 1; i <= SAMPLES; i++) {
    etaBig = t.etaFromTotal(145 + i * BIG_RATE, TRUE_TOTAL, i * 1000);
  }
  console.log(`    新模型扫大目录第 ${SAMPLES} 秒：剩余 1200 文件 → ETA=${etaBig?.toFixed(2)}s`);
  check('新模型不会被「目录进度 97%」骗到（仍给出有效估算）', etaBig != null, String(etaBig));
  check('新模型 ETA 贴合真值 1s', near(etaBig, 1, 0.3), `eta=${etaBig}`);
  check('新模型总量恒定 → ETA 不会一步暴涨 50 倍', etaBig < 200, `eta=${etaBig?.toFixed(0)}s`);
}

console.log('\n进度停滞：速率衰减而不是卡住不动');
{
  const t = new EtaTracker({ warmupMs: 0, minSamples: 2 });
  t.reset(0, 0);
  t.etaFromTotal(100, 1000, 1000);
  const before = t.etaFromTotal(200, 1000, 2000);
  // 之后 5 秒完全没进展
  let after = before;
  for (let i = 1; i <= 5; i++) after = t.etaFromTotal(200, 1000, 2000 + i * 1000);
  check('停滞时 ETA 上升', after > before, `${before} → ${after}`);
  check('停滞时仍返回估算（不下误导性的 null）', after != null);
}

console.log('\n时间倒流 / 进度回退不应污染速率');
{
  const t = new EtaTracker({ warmupMs: 0, minSamples: 1 });
  t.reset(0, 0);
  t.etaFromTotal(100, 1000, 1000);
  const good = t.etaFromTotal(200, 1000, 2000);
  t.etaFromTotal(150, 1000, 1500); // 脏样本：时间倒流 + 进度回退
  const after = t.etaFromTotal(300, 1000, 3000);
  check('脏样本被丢弃，估算仍有效', after != null && after < good, `${good} → ${after}`);
}

console.log(
  failed === 0 ? '\n全部通过' : `\n${failed} 项失败`
);
process.exit(failed === 0 ? 0 : 1);
