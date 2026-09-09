import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet } from '../../api/client';
import type { KibanaPodRow, KibanaPodsResp } from '../../api/types';
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

/** 容器视角：左侧按应用分组的 Pod 树（带日志量 / 错误红标），右侧日志流。 */
export function KibanaExplorer({ site, query, wrap, onToggleWrap, refreshSec }: Props) {
  const { t } = useT();
  const [pods, setPods] = useState<KibanaPodRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string>('');

  const qKey = JSON.stringify([
    site, query.start, query.end, query.namespace, query.container,
    query.app, query.host, query.keyword, query.excludeKeyword, query.levels,
  ]);

  const loadPods = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const q = new URLSearchParams({
        start: query.start || 'now-1h', end: query.end || '',
        namespace: query.namespace, container: query.container, app: query.app,
        host: query.host, keyword: query.keyword,
        excludeKeyword: query.excludeKeyword,
        levels: query.levels.join(','),
      });
      const d = await apiGet<KibanaPodsResp>(`/api/kibana/pods?${q.toString()}`);
      if (d.ok === false) { setError(d.error || ''); return; }
      const list = d.pods || [];
      setPods(list);
      setTotal(d.total ?? 0);
      // 选定 Pod 不在列表内（被筛选掉）时清空选择
      if (selected && !list.some((p) => p.pod === selected)) setSelected('');
    } catch (ex: any) {
      setError(ex.message || String(ex));
    } finally {
      setLoading(false);
    }
  }, [query, selected]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { loadPods(); }, [qKey, loadPods]);
  // 站点切换时清空选择
  useEffect(() => { setSelected(''); }, [site]);

  const grouped = useMemo(() => {
    const m = new Map<string, KibanaPodRow[]>();
    for (const p of pods) {
      const key = p.app || p.container || '—';
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(p);
    }
    return [...m.entries()]
      .map(([app, list]) => ({
        app,
        list: list.sort((a, b) => b.errors - a.errors || b.count - a.count),
      }))
      .sort((a, b) => {
        const ae = a.list.reduce((s, p) => s + p.errors, 0);
        const be = b.list.reduce((s, p) => s + p.errors, 0);
        return be - ae || a.app.localeCompare(b.app);
      });
  }, [pods]);

  const req = useMemo(() => ({
    site,
    start: query.start, end: query.end, namespace: query.namespace,
    container: query.container, app: query.app, host: query.host,
    pod: selected, pods: [] as string[],
    keyword: query.keyword, excludeKeyword: query.excludeKeyword,
    levels: query.levels,
  }), [site, query, selected]);

  return (
    <div className="kb-explorer">
      <div className="kb-tree">
        <div className="kb-tree-head">
          <span>{t('kibana.pods')}</span>
          <span className="kb-tree-total">{total}</span>
          <div className="spacer" />
          <button className="btn btn-ghost btn-sm" onClick={loadPods}
                  disabled={loading} title={t('common.refresh')}>↻</button>
        </div>
        {error && <div className="kb-stream-err">{error}</div>}
        <div className="kb-tree-body">
          {loading && pods.length === 0 && (
            <div className="empty-hint">{t('common.loading')}</div>
          )}
          {!loading && pods.length === 0 && !error && (
            <div className="empty-hint">{t('kibana.noPods')}</div>
          )}
          {grouped.map(({ app, list }) => (
            <div key={app} className="kb-tree-group">
              <div className="kb-tree-group-head">
                <span className="kb-g-app">{app}</span>
                <span className="kb-g-count">{list.length} pods</span>
              </div>
              {list.map((p) => (
                <button
                  key={p.pod}
                  className={`kb-tree-pod${selected === p.pod ? ' sel' : ''}`}
                  onClick={() => setSelected(p.pod)}
                >
                  <span className="kb-pod-name">{p.pod}</span>
                  <span className="kb-pod-meta">
                    {p.count > 999 ? `${(p.count / 1000).toFixed(1)}k` : p.count}
                    {p.errors > 0 && <b className="kb-pod-err">{p.errors}</b>}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>

      <div className="kb-explorer-log">
        <KibanaLogStream
          req={req}
          refreshSec={refreshSec}
          wrap={wrap}
          onToggleWrap={onToggleWrap}
          emptyHint={selected ? t('kibana.noLogs') : t('kibana.pickPodHint')}
          headerExtra={
            <span className="kb-cur-pod">
              {selected ? `📦 ${selected}` : `📦 ${t('kibana.allPods')}`}
            </span>
          }
        />
      </div>
    </div>
  );
}
