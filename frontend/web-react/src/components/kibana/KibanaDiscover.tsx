import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiPost } from '../../api/client';
import type {
  KibanaHistBucket, KibanaHistogramResp,
} from '../../api/types';
import { useT } from '../../i18n';
import { KibanaLogStream } from './KibanaLogStream';
import type { KibanaQuery } from './context';

interface Props {
  site: string;
  query: KibanaQuery;
  wrap: boolean;
  onToggleWrap: () => void;
  refreshSec: number;
}

function fmtTick(iso: string): string {  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 检索视角：顶部时间直方图（总量 + 错误量）+ 结果日志流。 */
export function KibanaDiscover({ site, query, wrap, onToggleWrap, refreshSec }: Props) {
  const { t } = useT();
  const [hist, setHist] = useState<KibanaHistBucket[]>([]);
  const [error, setError] = useState('');

  const req = useMemo(() => ({
    site,
    start: query.start, end: query.end, namespace: query.namespace,
    container: query.container, app: query.app, host: query.host,
    pod: query.pod, pods: [] as string[],
    keyword: query.keyword, excludeKeyword: query.excludeKeyword,
    levels: query.levels,
  }), [site, query]);

  const qKey = JSON.stringify([site, query]);
  const loadHist = useCallback(async () => {
    try {
      const d = await apiPost<KibanaHistogramResp>('/api/kibana/histogram', req);
      if (d.ok === false) { setError(d.error || ''); return; }
      setHist(d.buckets || []);
      setError('');
    } catch (ex: any) {
      setError(ex.message || String(ex));
    }
  }, [req]);

  useEffect(() => { loadHist(); }, [qKey, loadHist]);

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

  const exportAll = useCallback(async () => {
    try {
      const d = await apiPost<{ ok?: boolean; error?: string; content?: string;
        filename?: string; count?: number }>('/api/kibana/export', { ...req, size: 20000 });
      if (d.ok === false || !d.content) { setError(d.error || t('kibana.exportFailed')); return; }
      const blob = new Blob([d.content], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = d.filename || 'kibana-logs.log'; a.click();
      URL.revokeObjectURL(url);
    } catch (ex: any) { setError(ex.message || String(ex)); }
  }, [req, t]);

  return (
    <div className="kb-discover">
      <div className="kb-hist">
        <div className="kb-hist-head">
          <span>{t('kibana.histogram')}</span>
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
            <div key={i} className="kb-bar" title={`${fmtTick(b.ts)} · ${b.count} (${b.errors} err)`}>
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
        extraActions={
          <button className="btn btn-ghost btn-sm" onClick={exportAll}>
            {t('common.download')}
          </button>
        }
      />
    </div>
  );
}
