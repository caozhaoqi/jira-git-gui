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
  /** 桶内按函数（类型）拆分的计数，count 降序 → 堆叠柱从底往上由大到小 */
  segs: CfDistBucketSeg[];
}

/** 堆叠柱的一段：某函数在该时间桶内的日志数。 */
export interface CfDistBucketSeg {
  name: string;
  count: number;
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
 * 按时间分桶（柱内再按函数拆分，供堆叠着色）。
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
  const perBucket = new Map<number, Map<string, number>>();
  for (let i = 0; i < n; i++) {
    buckets.push({ ts: start + i * stepMs, count: 0, err: 0, segs: [] });
    perBucket.set(start + i * stepMs, new Map());
  }
  for (const r of rows) {
    const t = cfRowTime(r);
    if (Number.isNaN(t)) continue;
    const idx = Math.min(Math.floor((t - start) / stepMs), n - 1);
    if (idx < 0) continue;
    const b = buckets[idx];
    b.count += 1;
    if (cfRowIsError(r)) b.err += 1;
    const name = cfRowType(r, '') || '(未知)';
    const seg = perBucket.get(b.ts)!;
    seg.set(name, (seg.get(name) || 0) + 1);
  }
  for (const b of buckets) {
    const seg = perBucket.get(b.ts)!;
    b.segs = Array.from(seg, ([name, count]) => ({ name, count }))
      .sort((a, b2) => b2.count - a.count || a.name.localeCompare(b2.name));
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

// ─────────────────────────────────────────────────────────────────────────────
// 函数配色：同一函数在任何视图、任何过滤条件下都拿到同一个颜色。
//
// 为什么用「名字哈希」而不是「按数量排名的下标」：类型列表会随时间范围钻取 /
// 类型过滤重排，按排名取色会让同一个函数前后变色，用户刚建立的
// 「颜色=函数」心智模型被打破；哈希只依赖函数名本身，永远稳定。
// 具体色值定义在 styles/panels.css 的 --cfd-c1..--cfd-c10（浅色/深色两套），
// 这里只返回变量引用，主题切换由 CSS 负责。
// ─────────────────────────────────────────────────────────────────────────────

/** 函数色板容量（与 panels.css 的 --cfd-cN 一一对应）。 */
export const CF_TYPE_PALETTE_SIZE = 10;

/** djb2 字符串哈希 → 稳定非负整数。 */
export function cfNameHash(name: string): number {
  let h = 5381;
  for (let i = 0; i < name.length; i++) h = ((h << 5) + h + name.charCodeAt(i)) | 0;
  return Math.abs(h);
}

/** 函数名 → `var(--cfd-cN)`。同一名字恒定同一色；N ∈ [1, CF_TYPE_PALETTE_SIZE]。 */
export function cfTypeColorVar(name: string): string {
  const idx = (cfNameHash(name || '(未知)') % CF_TYPE_PALETTE_SIZE) + 1;
  return `var(--cfd-c${idx})`;
}

/** 「其他/未知」用的中性色（灰色，避免和任何真实函数撞色）。 */
export const CF_TYPE_REST_COLOR = 'var(--text-faint)';
