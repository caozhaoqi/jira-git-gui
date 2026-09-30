import { useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { apiPost } from '../../api/client';
import type {
  KibanaHistBucket, KibanaHistogramResp,
} from '../../api/types';
import { useT } from '../../i18n';
import { KibanaLogStream } from './KibanaLogStream';
import { KibanaContext, type KibanaQuery } from './context';

interface Props {
  site: string;
  query: KibanaQuery;
  wrap: boolean;
  onToggleWrap: () => void;
  refreshSec: number;
  /** 点「查询」时自增，用于强制重新拉取 */
  reloadKey?: number;
  /** 点直方图柱条时回调（start/end 为该柱覆盖的绝对时间段），由父级写入草稿并应用 */
  onTimeRangePick?: (patch: Partial<KibanaQuery>) => void;
  /** 时间范围钻取栈深度（>0 时显示「⤺ 返回上一级」按钮） */
  rangeStackLen?: number;
  /** 「⤺ 返回上一级」：弹出上一级时间范围 */
  onPopRange?: () => void;
}

function fmtTick(iso: string): string {  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 把 ES interval 字符串（10s/1m/5m/2h…）解析成毫秒，用于推算最后一根柱的右边界 */
function intervalToMs(iv: string): number {
  const m = /^(\d+)([smhd])$/.exec(iv || '');
  if (!m) return 60_000;
  const mult: Record<string, number> = { s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 };
  return Number(m[1]) * (mult[m[2]] || 60_000);
}

/** 检索视角：顶部时间直方图（总量 + 错误量，点柱条按时间段过滤）+ 结果日志流。 */
export function KibanaDiscover({ site, query, wrap, onToggleWrap, refreshSec, reloadKey = 0, onTimeRangePick, rangeStackLen = 0, onPopRange }: Props) {
  const { t } = useT();
  // 只取 addBusy 稳定引用（勿把 ctx 对象放进依赖数组，理由同 Explorer）
  const addBusy = useContext(KibanaContext)?.addBusy;
  const [hist, setHist] = useState<KibanaHistBucket[]>([]);
  const [histInterval, setHistInterval] = useState('1m');
  const [histLoading, setHistLoading] = useState(false);
  const [error, setError] = useState('');

  const req = useMemo(() => ({
    site,
    start: query.start, end: query.end, namespace: query.namespace,
    container: query.container, app: query.app, host: query.host,
    pod: query.pod, pods: [] as string[],
    keyword: query.keyword, exclude_keyword: query.excludeKeyword,
    levels: query.levels,
  }), [site, query]);

  const qKey = JSON.stringify([site, query]);
  const loadHist = useCallback(async () => {
    setHistLoading(true);
    addBusy?.(1);
    try {
      const d = await apiPost<KibanaHistogramResp>('/api/kibana/histogram', req);
      if (d.ok === false) { setError(d.error || ''); return; }
      setHist(d.buckets || []);
      setHistInterval((d as any).interval || '1m');
      setError('');
    } catch (ex: any) {
      setError(ex.message || String(ex));
    } finally {
      setHistLoading(false);
      addBusy?.(-1);
    }
  }, [req, addBusy]);

  useEffect(() => { loadHist(); }, [qKey, loadHist, reloadKey]);

  const { maxCount } = useMemo(() => {
    let mc = 0;
    for (const b of hist) { mc = Math.max(mc, b.count); }
    return { maxCount: mc || 1 };
  }, [hist]);

  // 抽样展示 x 轴刻度（最多 ~8 个）
  const tickIdx = useMemo(() => {
    const step = Math.max(1, Math.ceil(hist.length / 8));
    return hist.map((_, i) => i).filter((i) => i % step === 0);
  }, [hist]);

  /** 点柱条 → 按该柱覆盖的绝对时间段过滤（写入草稿并立即应用）。
   *  右边界 = 下一根柱的起点；最后一根用 interval 推算。 */
  const pickBucket = useCallback((i: number) => {
    const b = hist[i];
    if (!b?.ts || !onTimeRangePick) return;
    const startMs = new Date(b.ts).getTime();
    if (Number.isNaN(startMs)) return;
    const nextMs = i + 1 < hist.length ? new Date(hist[i + 1].ts).getTime() : startMs + intervalToMs(histInterval);
    const endMs = Number.isNaN(nextMs) ? startMs + intervalToMs(histInterval) : nextMs;
    onTimeRangePick({
      start: new Date(startMs).toISOString(),
      end: new Date(endMs).toISOString(),
    });
  }, [hist, histInterval, onTimeRangePick]);

  // 注：导出用 KibanaLogStream 内置的「Download」按钮（doExport，同走 /api/kibana/export
  // size=20000），这里不再通过 extraActions 传第二个 —— 曾因此出现两个并排的 Download。

  return (
    <div className="kb-discover">
      <div className={`kb-hist${histLoading ? ' loading' : ''}`}>
        <div className="kb-hist-head">
          <span>{t('kibana.histogram')}{histLoading ? ' …' : ''}</span>
          {rangeStackLen > 0 && onPopRange && (
            <button className="btn btn-ghost btn-sm kb-zoomout" onClick={onPopRange}
                    title={t('kibana.zoomOut')}>
              ⤺ {t('kibana.zoomOut')}
            </button>
          )}
          {hist.length > 0 && (
            <span className="kb-hist-meta">
              {hist.length} × {' · '}
              {hist.reduce((s, b) => s + b.count, 0).toLocaleString()}
            </span>
          )}
        </div>
        {error && <div className="kb-stream-err">{error}</div>}
        <div className="kb-hist-bars">
          {hist.length === 0 && <div className="empty-hint">{t('kibana.noData')}</div>}
          {hist.map((b, i) => (
            <div
              key={i}
              className={`kb-bar${onTimeRangePick ? ' clickable' : ''}`}
              title={`${fmtTick(b.ts)} · ${b.count} (${b.errors} err)` +
                     (onTimeRangePick ? ` · ${t('kibana.histClickHint')}` : '')}
              onClick={() => pickBucket(i)}
              role={onTimeRangePick ? 'button' : undefined}
              tabIndex={onTimeRangePick ? 0 : undefined}
              onKeyDown={onTimeRangePick
                ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickBucket(i); } }
                : undefined}
            >
              <div className="kb-bar-inner">
                <div className="kb-bar-err"
                     style={{ height: `${(b.errors / maxCount) * 100}%` }} />
                <div className="kb-bar-fill"
                     style={{ height: `${(b.count / maxCount) * 100}%`,
                              background: b.errors > 0 ? 'var(--danger)' : undefined }} />
              </div>
              {tickIdx.includes(i) && (
                <div className="kb-bar-label">{fmtTick(b.ts)}</div>
              )}
            </div>
          ))}
        </div>
      </div>

      <KibanaLogStream
        req={req}
        refreshSec={refreshSec}
        wrap={wrap}
        onToggleWrap={onToggleWrap}
        headerExtra={<span className="kb-cur-pod">🔍 {t('kibana.discover')}</span>}
      />
    </div>
  );
}
