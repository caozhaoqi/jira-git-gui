// 进度 ETA 估算工具：把「已完成量 / 近期速率」换算成「预计剩余时间」。
//
// 三条设计原则，都是被真实 bug 逼出来的：
//
// 1. **速率用 EMA，不用全程平均。**
//    旧实现是 rate = (done - d0) / elapsed，即「任务开始至今的累计平均速率」。
//    速率一变（网络抖动、并发爬坡、前期慢后期快）它就严重滞后——典型症状是
//    开头显示「30 分钟」、后半程突然跳到「2 分钟」。这里改用指数滑动平均跟踪
//    **近期**速率，并对单次变化限幅，避免数字乱跳。
//
// 2. **「已完成量」与「总量」必须同单位。**
//    旧实现还有一个 etaFromTotal / etaFromFraction 之外的用法：拿「目录进度比例」
//    去除「已扫描文件数」反推总量（totalEst = done / frac）。这在文件分布不均匀的
//    仓库里会给出离谱结果——一个大目录就能让总量估值翻几倍，表现为「剩余时间
//    越走越长」。因此这里**只保留 etaFromTotal**：调用方必须给同单位的真实总量，
//    拿不到可信总量时返回 null（界面不显示），而不是给个数字骗人。
//
// 3. **不给不可靠的估算。** 样本太少、速率还没建立时返回 null。

/** ETA 估算的可调参数 */
export interface EtaOptions {
  /** EMA 平滑系数，越大越跟随近期速率。默认 0.3 */
  alpha?: number;
  /** 预热时长（毫秒）：这段时间内只建立速率、不产出估算。默认 1500 */
  warmupMs?: number;
  /** 预热样本数：至少这么多次进度更新后才产出估算。默认 3 */
  minSamples?: number;
  /**
   * 单次采样允许的最大速率变化**倍数**（含涨与跌）。默认 1.6。
   *
   * ⚠️ 限幅施加在**速率**上而不是 ETA 上，这点很关键：恒速前进时 ETA 本该
   * 随剩余量线性下降，若直接对 ETA 做百分比限幅，会把这个正常的下降误判成
   * 「跳变」而拦住，导致数字越到后面越滞后。
   */
  maxRateJump?: number;
}

type ResolvedOptions = Required<EtaOptions>;

const DEFAULTS: ResolvedOptions = {
  alpha: 0.3,
  warmupMs: 1500,
  minSamples: 3,
  maxRateJump: 1.6,
};

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
  private opt: ResolvedOptions;
  private t0 = 0;
  private tPrev = 0;
  private dPrev = 0;
  /** EMA 速率（单位/秒）；0 表示尚未建立 */
  private rate = 0;
  private samples = 0;
  private started = false;

  constructor(opt: EtaOptions = {}) {
    this.opt = { ...DEFAULTS, ...opt };
  }

  reset(done = 0, now = Date.now()): void {
    this.t0 = now;
    this.tPrev = now;
    this.dPrev = done;
    this.rate = 0;
    this.samples = 0;
    this.started = true;
  }

  /**
   * 记录一次进度采样并更新 EMA 速率。
   * {@link etaFromTotal} 内部会调用，一般无需手动调用。
   */
  sample(done: number, now = Date.now()): void {
    if (!this.started) {
      this.reset(done, now);
      return;
    }
    const dt = (now - this.tPrev) / 1000;
    const dd = done - this.dPrev;
    this.tPrev = now;
    this.dPrev = done;
    // 时间倒流 / 进度回退：丢弃该样本，不让脏数据污染速率
    if (dt <= 0 || dd < 0) return;
    this.samples++;
    const { alpha, maxRateJump } = this.opt;
    if (this.rate <= 0) {
      // 首个有效样本：直接用瞬时速率建立基准
      if (dd > 0) this.rate = dd / dt;
      return;
    }
    if (dd === 0) {
      // 进度没推进：速率按最大跌幅衰减（长期停滞时会趋近 0，ETA 随之上升——
      // 这比假装一切正常要诚实，且受 maxRateJump 约束不会瞬间爆掉）
      this.rate /= maxRateJump;
      return;
    }
    const inst = dd / dt;
    const smoothed = this.rate * (1 - alpha) + inst * alpha;
    // 限幅：单次采样速率最多变化 maxRateJump 倍
    const hi = this.rate * maxRateJump;
    const lo = this.rate / maxRateJump;
    this.rate = Math.min(hi, Math.max(lo, smoothed));
  }

  /** 当前是否已具备产出估算的可信度（样本够多、已过预热、速率已建立） */
  isConfident(now = Date.now()): boolean {
    if (!this.started) return false;
    if (this.samples < this.opt.minSamples) return false;
    if (now - this.t0 < this.opt.warmupMs) return false;
    return this.rate > 0;
  }

  /**
   * 已知真实总量 total 时的 ETA（秒）。
   *
   * ⚠️ ``done`` 与 ``total`` **必须是同一单位**（都是文件数、都是字节数…）。
   * 单位不一致会让估算完全失真，这正是旧版扫描 ETA「越走越长」的根因。
   *
   * 每个进度事件只应调用一次（内部会记录采样点）。
   * 无法给出可靠估算时返回 null——调用方应据此隐藏 ETA，而不是显示 0。
   */
  etaFromTotal(done: number, total: number, now = Date.now()): number | null {
    if (!this.started) return null;
    this.sample(done, now);
    if (total <= 0 || done >= total) return null;
    if (!this.isConfident(now)) return null;
    const raw = (total - done) / this.rate;
    if (!isFinite(raw) || raw < 0) return null;
    return raw;
  }
}
