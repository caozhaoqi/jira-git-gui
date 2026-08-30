// 进度 ETA 估算工具：把「已完成量 / 耗时」换算成「预计剩余时间」。
// 两类输入：
//  - etaFromTotal：已知真实总量 total（克隆 / 下载 / 合并）。
//  - etaFromFraction：只有完成百分比 frac（扫描阶段后端只给出目录进度 pct，
//    文件数 done 与总量不同单位，用 frac 反推总量）。

/** 把秒数格式化为中文人类可读文案；非正/非法返回空串 */
export function formatEta(sec: number): string {
  if (!isFinite(sec) || sec <= 0) return '';
  const s = Math.round(sec);
  if (s < 1) return '不到 1 秒';
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h} 时 ${mm} 分` : `${h} 时`;
}

export class EtaTracker {
  private t0 = 0;
  private d0 = 0;
  private started = false;

  reset(done = 0, now = Date.now()): void {
    this.t0 = now;
    this.d0 = done;
    this.started = true;
  }

  /** 已知真实总量 total 时的 ETA（秒） */
  etaFromTotal(done: number, total: number, now = Date.now()): number | null {
    if (!this.started) return null;
    const elapsed = (now - this.t0) / 1000;
    const adv = done - this.d0;
    if (elapsed < 0.5 || adv <= 0 || total <= done || total <= 0) return null;
    const rate = adv / elapsed; // 单位/秒
    return Math.max(0, (total - done) / rate);
  }

  /** 已知完成比例 frac∈(0,1) 时的 ETA（秒），用于只有百分比、没有真实总量的阶段 */
  etaFromFraction(done: number, frac: number, now = Date.now()): number | null {
    if (!this.started) return null;
    if (!(frac > 0 && frac < 1)) return null; // 比例无效（含边界）直接不给估算
    const elapsed = (now - this.t0) / 1000;
    const adv = done - this.d0;
    if (elapsed < 0.5 || adv <= 0) return null;
    const f = Math.min(0.999, Math.max(0.001, frac));
    const totalEst = done / f;
    const rate = adv / elapsed;
    return Math.max(0, (totalEst - done) / rate);
  }
}
