// 云函数日志「分布」可视化用的纯计算工具。
//
// 设计要点：
// - 数据源是前端已加载的日志行（HCM 侧没有可用的聚合接口，服务端全量聚合受
//   nginx 60s 超时限制不可行），因此这里全部是纯前端聚合，且要显式标注「基于 N 条」。
// - 本模块**不得 import 任何组件**（CfPanel ⇄ CfDistribution 若互相 import 会形成
//   循环依赖，模块求值顺序问题曾导致白屏）；只依赖零副作用的 utils/logFields。
import { logRowTime, logRowType, type LogRowLike } from './logFields';

/** 时间分布的一根柱：ts = 桶左边界（毫秒），count = 桶内日志数，err = 其中疑似错误数。 */
export interface CfDistBucket {
  ts: number;
  count: number;
  err: number;
}

/** 类型分布的一项。 */
export interface CfDistTypeItem {
  name: string;
  count: number;
  /** 该类型中疑似错误的行数 */
  err: number;
}

/** 疑似错误/异常的关键字（中英双语，覆盖常见后端与 HCM 云函数报错文案）。 */
export const CF_ERR_RE =
  /(^|[^a-z])(error|exception|failed|failure|fatal|panic|timeout|timed out|traceback)([^a-z]|$)|异常|错误|失败|超时|报错|失败/i;

/** 把日志时间字符串解析为毫秒时间戳；无法解析返回 NaN。兼容 "2026-09-15 10:00:00"、ISO、unix 秒/毫秒。 */
export function parseCfTime(s: string | number | undefined | null): number {
  if (s == null) return NaN;
  const raw = String(s).trim();
  if (!raw) return NaN;
  if (/^\d+$/.test(raw)) {
    const n = Number(raw);
    if (n >= 1e12) return n; // 毫秒
    if (n >= 1e9) return n * 1000; // 秒
    return NaN;
  }
  let t = new Date(raw.replace(' ', 'T')).getTime();
  if (isNaN(t)) t = new Date(raw).getTime();
  return t;
}

/** 取某行的内容文本（与 CfPanel 的 cfContent 同口径：content → message → data）。 */
export function cfRowContent(row: LogRowLike): string {
  const r = row as Record<string, unknown>;
  const c = r.content ?? r.message ?? r.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c) : String(c);
}

/** 某行是否疑似错误（内容命中错误关键字）。 */
export function cfRowIsError(row: LogRowLike): boolean {
  return CF_ERR_RE.test(cfRowContent(row));
}

/** 某行的时间戳（毫秒，无法解析为 NaN）。字段兜底走 logFields（create_* → update_*）。 */
export function cfRowTime(row: LogRowLike): number {
  return parseCfTime(logRowTime(row));
}

/** 某行的类型/描述值（字段兜底走 logFields：log_type → name → title …）。 */
export function cfRowType(row: LogRowLike, fallback = ''): string {
  return logRowType(row, fallback);
}

// 候选桶宽（毫秒），从 10s 到 30d，按「跨度 / 桶宽 <= 目标柱数」挑最小的一个。
const STEP_CANDIDATES = [
  10_000,
  30_000,
  60_000,
  2 * 60_000,
  5 * 60_000,
  10 * 60_000,
  15 * 60_000,
  30 * 60_000,
  3_600_000,
  2 * 3_600_000,
  3 * 3_600_000,
  6 * 3_600_000,
  12 * 3_600_000,
  86_400_000,
  2 * 86_400_000,
  7 * 86_400_000,
  30 * 86_400_000,
];

/** 按时间跨度自适应选择桶宽，保证柱数不超过 targetBars。 */
export function pickStepMs(spanMs: number, targetBars = 48): number {
  if (!(spanMs > 0)) return 60_000;
  for (const s of STEP_CANDIDATES) {
    if (spanMs / s <= targetBars) return s;
  }
  return STEP_CANDIDATES[STEP_CANDIDATES.length - 1];
}

/** 桶宽 → 人类可读标签（10s / 5m / 3h / 1d）。 */
export function fmtStep(stepMs: number): string {
  const sec = Math.round(stepMs / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hour = Math.round(min / 60);
  if (hour < 24) return `${hour}h`;
  return `${Math.round(hour / 24)}d`;
}

/**
 * 把日志行按时间分桶。
 * - 所有桶等宽、按 stepMs 对齐，跨度覆盖所有有效时间戳。
 * - 无有效时间戳时返回空数组（调用方负责显示空态）。
 */
export function buildTimeBuckets(rows: LogRowLike[], stepMs: number): CfDistBucket[] {
  const times: number[] = [];
  for (const r of rows) {
    const t = cfRowTime(r);
    if (!Number.isNaN(t)) times.push(t);
  }
  if (!times.length || !(stepMs > 0)) return [];
  let min = times[0];
  let max = times[0];
  for (const t of times) {
    if (t < min) min = t;
    if (t > max) max = t;
  }
  const start = Math.floor(min / stepMs) * stepMs;
  const end = Math.floor(max / stepMs) * stepMs;
  const n = Math.min(Math.round((end - start) / stepMs) + 1, 400); // 兜底上限，防极端跨度爆柱
  const buckets: CfDistBucket[] = [];
  for (let i = 0; i < n; i++) buckets.push({ ts: start + i * stepMs, count: 0, err: 0 });
  for (const r of rows) {
    const t = cfRowTime(r);
    if (Number.isNaN(t)) continue;
    const idx = Math.min(Math.floor((t - start) / stepMs), n - 1);
    if (idx < 0) continue;
    buckets[idx].count += 1;
    if (cfRowIsError(r)) buckets[idx].err += 1;
  }
  return buckets;
}

/** 按类型分组统计（降序；同数量按名称排序，保证渲染稳定）。 */
export function countByType(rows: LogRowLike[], fallback = ''): CfDistTypeItem[] {
  const map = new Map<string, CfDistTypeItem>();
  for (const r of rows) {
    const name = cfRowType(r, fallback) || '(未知)';
    const item = map.get(name) || { name, count: 0, err: 0 };
    item.count += 1;
    if (cfRowIsError(r)) item.err += 1;
    map.set(name, item);
  }
  return Array.from(map.values()).sort((a, b) =>
    b.count - a.count || a.name.localeCompare(b.name),
  );
}

/** 桶左边界 → 展示用刻度（MM/DD HH:mm；桶宽小于 1 分钟时补秒，避免刻度重复）。 */
export function fmtBucketTick(ms: number, stepMs = 60_000): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n: number) => String(n).padStart(2, '0');
  const hm = `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  return stepMs < 60_000 ? `${hm}:${p(d.getSeconds())}` : hm;
}

/** 桶左边界 → 悬停提示（完整时间 + 数量 + 错误数）。 */
export function fmtBucketFull(ms: number, stepMs: number): string {
  const d = new Date(ms);
  const e = new Date(ms + stepMs);
  const p = (n: number) => String(n).padStart(2, '0');
  const one = (x: Date) =>
    `${x.getFullYear()}-${p(x.getMonth() + 1)}-${p(x.getDate())} ${p(x.getHours())}:${p(x.getMinutes())}`;
  return `${one(d)} ~ ${one(e)}`;
}
