import { useMemo, useState } from 'react';
import type { CfLogsRow } from '../api/types';
import { useT } from '../i18n';
import {
  buildTimeBuckets, cfRowIsError, cfRowTime, countByType, fmtBucketFull, fmtBucketTick, fmtStep, pickStepMs,
  type CfDistBucket,
} from '../utils/cfDist';

interface Props {
  /** 当前过滤链（时间窗口 + 类型 + 关键词）之后的**全部**行；父级只传这一份，不要传分页后的页内行 */
  rows: CfLogsRow[];
  /** 查询时填的类型过滤值，作为类型兜底（与列表口径一致） */
  typeFallback?: string;
  /** 点时间柱：按该柱覆盖的绝对时间段过滤（毫秒） */
  onTimeRangePick?: (startMs: number, endMs: number) => void;
  /** 时间范围钻取栈深度（>0 时显示「返回上一级」） */
  rangeStackLen?: number;
  /** 返回上一级时间范围 */
  onPopRange?: () => void;
  /** 已选中的类型（前端过滤，多选） */
  typePick: string[];
  /** 切换某个类型的选中态 */
  onToggleType: (name: string) => void;
  /** 清空类型过滤 */
  onClearTypes: () => void;
}

/** 类型分布条最多直接展示的条数，其余折叠成一行「其他」计数 */
const MAX_TYPE_BARS = 10;
/** 目标柱数：桶宽按跨度自适应，保证柱数不超过它 */
const TARGET_BARS = 48;

/**
 * 云函数日志「分布」卡片：时间直方图（点柱钻取时间段）+ 类型分布（点击按类型过滤）。
 *
 * ⚠️ 数据源是**当前已加载**的日志行（纯前端聚合），不是服务端全量，
 * 因此头部显式标注「基于 N 条」，避免被误读成全量统计。
 */
export function CfDistribution({
  rows, typeFallback = '', onTimeRangePick, rangeStackLen = 0, onPopRange,
  typePick, onToggleType, onClearTypes,
}: Props) {
  const { t } = useT();
  // 默认收起：面板信息密度已较高，需要看分布时点标题展开（与「高级选项」同一策略）
  const [open, setOpen] = useState(false);

  const { buckets, stepMs, errTotal, types } = useMemo(() => {
    const step = pickStepMs(timeSpanMs(rows), TARGET_BARS);
    let errs = 0;
    for (const r of rows) if (cfRowIsError(r)) errs += 1;
    return {
      buckets: buildTimeBuckets(rows, step),
      stepMs: step,
      errTotal: errs,
      types: countByType(rows, typeFallback),
    };
  }, [rows, typeFallback]);

  const maxCount = useMemo(() => {
    let mc = 0;
    for (const b of buckets) mc = Math.max(mc, b.count);
    return mc || 1;
  }, [buckets]);

  // x 轴刻度抽样（最多 8 个）
  const tickIdx = useMemo(() => {
    const step = Math.max(1, Math.ceil(buckets.length / 8));
    return new Set(buckets.map((_, i) => i).filter((i) => i % step === 0));
  }, [buckets]);

  const shownTypes = types.slice(0, MAX_TYPE_BARS);
  const restCount = types.slice(MAX_TYPE_BARS).reduce((s, x) => s + x.count, 0);
  const restLen = Math.max(0, types.length - MAX_TYPE_BARS);

  const total = rows.length;

  const pickBucket = (b: CfDistBucket) => {
    if (!onTimeRangePick || !b.count) return;
    onTimeRangePick(b.ts, b.ts + stepMs);
  };

  return (
    <div className={`card-soft cf-dist-card${open ? '' : ' collapsed'}`}>
      <div className="panel-header">
        <button
          className="clash-collapser"
          onClick={() => setOpen((v) => !v)}
          title={open ? t('cf.distCollapse') : t('cf.distExpand')}
          aria-expanded={open}
        >
          <i className="cfd-caret">{open ? '▾' : '▸'}</i>
          <h2 className="section-title">{t('cf.distTitle')}</h2>
        </button>
        <span className="cf-dist-meta">
          {t('cf.distBasedOn', { n: total })}
          {buckets.length > 0 && ` · ${t('cf.distBucket')} ${fmtStep(stepMs)}`}
          {errTotal > 0 && <span className="cf-dist-meta-err"> · {t('cf.distErrCount', { n: errTotal })}</span>}
        </span>
        {typePick.length > 0 && (
          <button className="btn btn-ghost btn-sm cf-dist-clear" onClick={onClearTypes}
                  title={t('cf.distClearType')}>
            ✕ {t('cf.distClearType')} ({typePick.length})
          </button>
        )}
        {rangeStackLen > 0 && onPopRange && (
          <button className="btn btn-ghost btn-sm cf-dist-zoomout" onClick={onPopRange}
                  title={t('cf.distZoomOut')}>
            ⤺ {t('cf.distZoomOut')}
          </button>
        )}
      </div>

      {open && (
        <div className="cf-dist-body">
          {total === 0 ? (
            <div className="empty-hint">{t('cf.distEmpty')}</div>
          ) : (
            <>
              {/* ===== 时间分布（柱状；点柱按该时段过滤）===== */}
              <div className="cf-dist-section">
                <div className="cf-dist-subtitle">
                  <span>🕒 {t('cf.distTime')}</span>
                  {onTimeRangePick && <span className="cf-dist-hint">{t('cf.distClickHint')}</span>}
                </div>
                {buckets.length === 0 ? (
                  <div className="empty-hint">{t('cf.distNoTime')}</div>
                ) : (
                  <div className="cf-dist-bars">
                    {buckets.map((b, i) => (
                      <div
                        key={b.ts}
                        className={`cf-dist-bar${onTimeRangePick && b.count ? ' clickable' : ''}`}
                        title={`${fmtBucketFull(b.ts, stepMs)} · ${b.count}${b.err ? ` (${b.err} err)` : ''}` +
                               (onTimeRangePick && b.count ? ` · ${t('cf.distClickHint')}` : '')}
                        onClick={() => pickBucket(b)}
                        role={onTimeRangePick && b.count ? 'button' : undefined}
                        tabIndex={onTimeRangePick && b.count ? 0 : undefined}
                        onKeyDown={onTimeRangePick && b.count
                          ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickBucket(b); } }
                          : undefined}
                      >
                        <div className="cf-dist-bar-inner">
                          <div className="cf-dist-bar-err" style={{ height: `${(b.err / maxCount) * 100}%` }} />
                          <div className="cf-dist-bar-fill"
                               style={{
                                 height: `${(b.count / maxCount) * 100}%`,
                                 background: b.err > 0 ? 'var(--danger)' : undefined,
                               }} />
                        </div>
                        {tickIdx.has(i) && <div className="cf-dist-bar-label">{fmtBucketTick(b.ts, stepMs)}</div>}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* ===== 类型分布（横向条；点击按该类型过滤，再点取消）===== */}
              <div className="cf-dist-section">
                <div className="cf-dist-subtitle">
                  <span>🏷 {t('cf.distType')}</span>
                  <span className="cf-dist-hint">{t('cf.distTypesHint')}</span>
                </div>
                <div className="cf-dist-types">
                  {shownTypes.map((it) => {
                    const pct = total ? (it.count / total) * 100 : 0;
                    const on = typePick.includes(it.name);
                    return (
                      <button
                        key={it.name}
                        className={`cf-dist-type${on ? ' active' : ''}`}
                        onClick={() => onToggleType(it.name)}
                        title={`${it.name} · ${it.count}${it.err ? ` (${it.err} err)` : ''}`}
                      >
                        <span className="cf-dist-type-name">{on ? '✓ ' : ''}{it.name}</span>
                        <span className="cf-dist-type-track">
                          <span className="cf-dist-type-fill"
                                style={{ width: `${pct}%`, background: it.err > 0 ? 'var(--danger)' : undefined }} />
                        </span>
                        <span className="cf-dist-type-count">{it.count} · {pct.toFixed(1)}%</span>
                      </button>
                    );
                  })}
                  {restLen > 0 && (
                    <div className="cf-dist-type-rest">
                      {t('cf.distMoreTypes', { n: restLen, c: restCount })}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** 行集的时间跨度（毫秒）；无有效时间戳或跨度为 0 时回退 1 小时（避免桶宽退化）。 */
function timeSpanMs(rows: CfLogsRow[]): number {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const r of rows) {
    const ms = cfRowTime(r);
    if (Number.isNaN(ms)) continue;
    if (ms < min) min = ms;
    if (ms > max) max = ms;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return 3_600_000;
  return max - min;
}
