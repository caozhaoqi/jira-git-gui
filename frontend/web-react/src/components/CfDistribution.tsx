import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { TabIcon } from './icons';
import type { CfLogsRow } from '../api/types';
import { useT } from '../i18n';
import {
  assignTypeColors,
  buildTimeBuckets, cfRowIsError, cfRowTime, cfTypeColorVar, countByType,
  fmtBucketFull, fmtBucketTick, fmtStep, pickStepMs,
  UNKNOWN_TYPE_LABEL,
  type CfDistBucket,
} from '../utils/cfDist';

interface Props {
  /** 当前过滤链（时间窗口 + 类型 + 关键词）之后的**全部**行；父级只传这一份，不要传分页后的页内行 */
  rows: CfLogsRow[];
  /** 查询时填的类型过滤值，作为类型兜底（与列表口径一致） */
  typeFallback?: string;
  /** 分布图数据源说明：'ready'=伴生查询拉回的全类型集已到位；
   *  'partial'=全类型但只是最新样本（翻页触顶，未覆盖列表全部时段）；
   *  'loading'=伴生查询进行中（先按已加载行展示）；'none'=主结果本就是全类型 */
  allTypes?: 'none' | 'loading' | 'ready' | 'partial';
  /** 当前每页加载条数（父级 cfg.page_size） */
  pageSize?: number;
  /** 改加载条数：父级改 cfg 并重查（服务端分页，必须重查才生效） */
  onPageSizeChange?: (n: number) => void;
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
  /** 点类型=切换服务端类型过滤（伴生全类型视图下）而非前端多选 */
  switchOnTypeClick?: boolean;
}

/** 类型分布条最多直接展示的条数，其余折叠成一行「其他」计数 */
const MAX_TYPE_BARS = 10;
/** 目标柱数：桶宽按跨度自适应的上限。放到 120 而非 48——时间轴是**可横向滑动**的，
 *  桶密到放不下就滚动浏览，而不是把整段压成几十根、把突发细节抹平。 */
const TARGET_BARS = 120;

/**
 * 云函数日志「分布」卡片：时间直方图（堆叠柱，点柱钻取时间段）+ 类型分布（点击按类型过滤）。
 *
 * 数据源由父级决定：主查询**带了类型过滤**时，父级会另发一次不带类型过滤的「伴生查询」，
 * 把全类型行集传给本组件——否则分布图只能看到服务端过滤后的那一个类型
 * （「同期还有什么别的函数在写日志」看不出来）。此时点某个类型 = 切换服务端类型过滤
 * （switchOnTypeClick），再点一次当前类型则清除过滤看全量。
 *
 * 配色：`assignTypeColors` 按函数名稳定取色并**同视图防撞色**，时间柱按函数
 * 堆叠、类型条同色——两个视图互相印证，「哪根柱/哪根条是哪个函数」一眼可查。
 * 错误不抢颜色的戏：柱底一根红色 flag 表示「该时段有疑似错误」，明细在悬停提示里。
 *
 * ⚠️ 数据源是**当前已加载**的日志行（纯前端聚合），不是服务端全量，
 * 因此头部显式标注「基于 N 条」，避免被误读成全量统计。
 */
export function CfDistribution({
  rows, typeFallback = '', allTypes = 'none', pageSize, onPageSizeChange, onTimeRangePick, rangeStackLen = 0, onPopRange,
  typePick, onToggleType, onClearTypes, switchOnTypeClick = false,
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

  // 防撞色表：当前视图内的函数两两不同色（纯哈希取模在 10 格色板上极易撞色，
  // 曾实测 emp_information_statistics_req 与 BackfillNavkou 撞成同色）。
  // 堆叠柱分段与类型条共用同一张表，两个视图颜色互相印证的约定不变。
  const colorMap = useMemo(() => assignTypeColors(types.map((it) => it.name)), [types]);
  const typeColor = (name: string) => colorMap.get(name) ?? cfTypeColorVar(name);

  // —— 时间轴横向滑动 ——
  // 桶密到放不下时（细粒度 + 长跨度）横向滚动浏览，而不是压缩柱宽抹平细节。
  // 三种平移方式：竖滚轮（转横移）/ 鼠标拖拽 / 原生滚动条；触屏走原生触摸滚动。
  const scrollRef = useRef<HTMLDivElement>(null);
  const atRightRef = useRef(true); // 是否停在最新端（最新桶在最右）；新数据只在该状态下自动跟随
  const dragRef = useRef<{ x: number; scroll: number; moved: boolean } | null>(null);
  const didDragRef = useRef(false);

  // 竖滚轮 → 横向平移（必须 passive:false 才能 preventDefault 掉页面滚动）
  // ⚠️ 依赖 [open]：卡片默认收起时 .cf-dist-scroll 不在 DOM，挂载时挂不上监听；
  // 展开后重新执行才能把 wheel 监听 attach 到滚动容器上。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth) return; // 放得下：不劫持滚轮
      const d = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      if (!d) return;
      e.preventDefault();
      el.scrollLeft += d;
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [open]);

  // 新数据到达时若正停在最新端则保持贴右；用户翻去看历史时不打扰。
  // 同样依赖 [open]：展开瞬间滚动容器才挂载，此时也要贴到最新端。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !atRightRef.current) return;
    el.scrollLeft = el.scrollWidth;
  }, [buckets, open]);

  const onAxisScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    atRightRef.current = el.scrollWidth - el.scrollLeft - el.clientWidth < 8;
  };
  const onAxisPointerDown = (e: ReactPointerEvent) => {
    const el = scrollRef.current;
    if (!el || e.pointerType !== 'mouse' || e.button !== 0) return;
    didDragRef.current = false;
    dragRef.current = { x: e.clientX, scroll: el.scrollLeft, moved: false };
  };
  const onAxisPointerMove = (e: ReactPointerEvent) => {
    const d = dragRef.current;
    const el = scrollRef.current;
    if (!d || !el) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 4) {
      d.moved = true;
      didDragRef.current = true;
    }
    if (d.moved) {
      el.style.cursor = 'grabbing';
      el.scrollLeft = d.scroll - dx;
    }
  };
  const onAxisPointerUp = () => {
    const el = scrollRef.current;
    if (el) el.style.cursor = '';
    dragRef.current = null;
  };

  // x 轴刻度抽样（最多 8 个）：固定抽首个 + 每隔 step 一个 + **最后一个**，
  // 否则右端（往往是正在写入的最新桶）没有刻度锚定，看起来像柱「溢出」到图外。
  const tickIdx = useMemo(() => {
    const step = Math.max(1, Math.ceil(buckets.length / 8));
    const s = new Set(buckets.map((_, i) => i).filter((i) => i % step === 0));
    if (buckets.length > 0) s.add(buckets.length - 1);
    return s;
  }, [buckets]);

  /**
   * 桶高比例：**对数刻度** `log(1+n)/log(1+max)`。
   * 日志量呈长尾——最新桶动辄几千条、历史桶个位数，线性刻度下除最新柱外
   * 全部低于 1px，整张图看起来像「坏了」。对数刻度让个位数桶也有可见高度
   * （真实数量看悬停提示，title 里逐函数列出）。
   */
  const barH = (n: number) => Math.log1p(Math.max(0, n)) / Math.log1p(maxCount);

  const shownTypes = types.slice(0, MAX_TYPE_BARS);
  const restCount = types.slice(MAX_TYPE_BARS).reduce((s, x) => s + x.count, 0);
  const restLen = Math.max(0, types.length - MAX_TYPE_BARS);

  // 「加载条数」下拉：常用档位 + 当前值（历史遗留的自定义值也补一个选项，避免 select 空选）
  const PAGE_SIZE_PRESETS = [100, 200, 500, 1000];
  const pageSizeValue = Number.isFinite(pageSize) ? Math.min(1000, Math.max(1, Math.trunc(pageSize as number))) : 200;
  const pageSizeOptions = PAGE_SIZE_PRESETS.includes(pageSizeValue)
    ? PAGE_SIZE_PRESETS
    : [...PAGE_SIZE_PRESETS, pageSizeValue].sort((a, b) => a - b);

  const total = rows.length;

  const pickBucket = (b: CfDistBucket) => {
    // 拖拽平移结束后浏览器会补一个 click，此时不能触发「按该时段钻取」
    if (didDragRef.current) {
      didDragRef.current = false;
      return;
    }
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
          {onPageSizeChange && (
            <>
              {' · '}
              {t('cf.distPageSize')}
              {' '}
              <select
                className="sel cf-dist-page-size"
                value={pageSizeValue}
                onChange={(e) => onPageSizeChange(Number(e.target.value))}
                title={t('cf.distPageSizeHint')}
                aria-label={t('cf.distPageSize')}
              >
                {pageSizeOptions.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            </>
          )}
          {allTypes === 'ready' && ` · ${t('cf.distAllTypes')}`}
          {allTypes === 'partial' && ` · ${t('cf.distAllTypesPartial')}`}
          {allTypes === 'loading' && ` · ${t('cf.distAllTypesLoading')}`}
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
              {/* ===== 时间分布（堆叠柱状；点柱按该时段过滤）===== */}
              <div className="cf-dist-section">
                <div className="cf-dist-subtitle">
                  <span><TabIcon name="clock" size={13} /> {t('cf.distTime')}</span>
                  {onTimeRangePick
                    ? <span className="cf-dist-hint">{t('cf.distStackHint')}</span>
                    : <span className="cf-dist-hint">{t('cf.distColorHint')}</span>}
                </div>
                {buckets.length === 0 ? (
                  <div className="empty-hint">{t('cf.distNoTime')}</div>
                ) : (
                  <div
                    className="cf-dist-scroll"
                    ref={scrollRef}
                    onScroll={onAxisScroll}
                    onPointerDown={onAxisPointerDown}
                    onPointerMove={onAxisPointerMove}
                    onPointerUp={onAxisPointerUp}
                    onPointerLeave={onAxisPointerUp}
                  >
                    <div className="cf-dist-bars">
                      {buckets.map((b, i) => (
                        <div
                          key={b.ts}
                          className={`cf-dist-bar${onTimeRangePick && b.count ? ' clickable' : ''}`}
                          title={bucketTitle(b, stepMs, onTimeRangePick ? t('cf.distClickHint') : '')}
                          onClick={() => pickBucket(b)}
                          role={onTimeRangePick && b.count ? 'button' : undefined}
                          tabIndex={onTimeRangePick && b.count ? 0 : undefined}
                          onKeyDown={onTimeRangePick && b.count
                            ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickBucket(b); } }
                            : undefined}
                        >
                          <div className="cf-dist-bar-inner">
                            {b.segs.map((s) => (
                              <div
                                key={s.name}
                                className="cf-dist-bar-seg"
                                style={{
                                  // 桶整体高度走对数刻度；桶内各段按真实数量占比分摊
                                  height: `${(s.count / b.count) * barH(b.count) * 100}%`,
                                  background: typeColor(s.name),
                                }}
                              />
                            ))}
                            {b.err > 0 && <div className="cf-dist-bar-flag" />}
                          </div>
                          {tickIdx.has(i) && <div className="cf-dist-bar-label">{fmtBucketTick(b.ts, stepMs)}</div>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* ===== 类型分布（横向条；点击按该类型过滤，再点取消）===== */}
              <div className="cf-dist-section">
                <div className="cf-dist-subtitle">
                  <span><TabIcon name="tag" size={13} /> {t('cf.distType')}</span>
                  <span className="cf-dist-hint">{switchOnTypeClick ? t('cf.distTypesSwitchHint') : t('cf.distTypesHint')}</span>
                </div>
                <div className="cf-dist-types">
                  {shownTypes.map((it) => {
                    const pct = total ? (it.count / total) * 100 : 0;
                    const on = typePick.includes(it.name);
                    const color = typeColor(it.name);
                    return (
                      <button
                        key={it.name}
                        className={`cf-dist-type${on ? ' active' : ''}`}
                        onClick={() => onToggleType(it.name)}
                        title={`${it.name} · ${it.count}${it.err ? ` (${it.err} err)` : ''}`}
                      >
                        <span className="cf-dist-type-name">
                          <i className="cf-dist-type-swatch" style={{ background: color }} />
                          {on ? '✓ ' : ''}{it.name}
                        </span>
                        <span className="cf-dist-type-track">
                          <span className="cf-dist-type-fill"
                                style={{ width: `${pct}%`, background: color }} />
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
                  {/* 全部行都取不到类型：给出诊断提示（未知类型已用中性灰，不再冒充红色） */}
                  {types.length === 1 && types[0].name === UNKNOWN_TYPE_LABEL && (
                    <div className="cf-dist-unknown-hint" role="note">
                      {t('cf.distUnknownOnly')}
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

/** 桶悬停提示：时间段 + 总数（含错误数）+ 函数明细（最多列前 4 个，其余折叠计数）。 */
function bucketTitle(b: CfDistBucket, stepMs: number, clickHint: string): string {
  let s = `${fmtBucketFull(b.ts, stepMs)} · ${b.count}${b.err ? ` (${b.err} err)` : ''}`;
  if (b.segs.length) {
    const top = b.segs.slice(0, 4).map((x) => `${x.name}×${x.count}`).join(', ');
    const more = b.segs.length > 4 ? ` +${b.segs.length - 4}` : '';
    s += `\n${top}${more}`;
  }
  if (clickHint && b.count) s += `\n${clickHint}`;
  return s;
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
