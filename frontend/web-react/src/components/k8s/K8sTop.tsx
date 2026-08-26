import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';
import type { K8sTopResp, K8sTopRow } from '../../api/types';
import { useK8s } from './context';
import { parseTopVal } from '../../utils/format';
import { useT } from '../../i18n';

export function K8sTop() {
  const { target } = useK8s();
  const { t } = useT();
  const [scope, setScope] = useState<'pods' | 'nodes'>('pods');
  const [ns, setNs] = useState('');
  const [rows, setRows] = useState<K8sTopRow[]>([]);
  const [summary, setSummary] = useState('');
  const [busy, setBusy] = useState(false);
  const [auto, setAuto] = useState(false);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const q = new URLSearchParams({ env: target.env, scope });
      if (ns.trim() && scope === 'pods') q.set('namespace', ns.trim());
      const d = await api<K8sTopResp>('/api/k8s/top?' + q.toString());
      if (!d.ok) { setSummary(t('k8s.top.fail') + (d.error || '')); return; }
      setRows(d.rows || []);
      setSummary(t('k8s.top.summary', { n: (d.rows || []).length }));
    } catch (ex: any) {
      setSummary(t('k8s.top.fail') + ex.message);
    } finally {
      setBusy(false);
    }
  }, [target.env, scope, ns, t]);

  useEffect(() => {
    if (auto) {
      load();
      timerRef.current = window.setInterval(() => load(), 10000);
    }
    return () => {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    };
  }, [auto, load]);

  const maxCpu = Math.max(1e-9, ...rows.map((r) => parseTopVal(r.cpu)));
  const maxMem = Math.max(1e-9, ...rows.map((r) => parseTopVal(r.memory)));

  return (
    <div className="k8s-top">
      <div className="k8s-top-cfg">
        <select className="sel" value={scope} onChange={(e) => setScope(e.target.value as 'pods' | 'nodes')}>
          <option value="pods">{t('k8s.top.pods')}</option>
          <option value="nodes">{t('k8s.top.nodes')}</option>
        </select>
        {scope === 'pods' && <input className="input input-sm" value={ns} onChange={(e) => setNs(e.target.value)} placeholder={t('k8s.top.nsPh')} />}
        <label className="chk"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> {t('k8s.top.auto')}</label>
        <button className="btn btn-sm" onClick={load} disabled={busy || !target.env}>{t('k8s.top.refresh')}</button>
      </div>
      <div className="k8s-top-summary">{summary}</div>
      <div className="k8s-table-wrap">
        {rows.length === 0 ? <div className="empty-hint">{t('k8s.top.empty')}</div> :
          <table className="k8s-top-table">
            <thead>
              {scope === 'nodes'
                ? <tr><th>{t('k8s.top.colNode')}</th><th>{t('k8s.top.colCpu')}</th><th>{t('k8s.top.colCpuPct')}</th><th>{t('k8s.top.colMem')}</th><th>{t('k8s.top.colMemPct')}</th></tr>
                : <tr><th>{t('k8s.top.colPod')}</th><th>{t('k8s.top.colNs')}</th><th>{t('k8s.top.colCpu')}</th><th>{t('k8s.top.colMem')}</th></tr>}
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td className="top-name" title={r.name}>{r.name}</td>
                  {scope === 'pods' && <td>{r.namespace || ''}</td>}
                  <td className="top-cell"><span className="top-val">{r.cpu || ''}</span><div className="k8s-top-bar"><i style={{ width: `${Math.round(parseTopVal(r.cpu) / maxCpu * 100)}%` }} /></div></td>
                  {scope === 'nodes'
                    ? <td className="top-pct">{r.cpu_pct || ''}</td>
                    : null}
                  <td className="top-cell"><span className="top-val">{r.memory || ''}</span><div className="k8s-top-bar mem"><i style={{ width: `${Math.round(parseTopVal(r.memory) / maxMem * 100)}%` }} /></div></td>
                  {scope === 'nodes'
                    ? <td className="top-pct">{r.memory_pct || ''}</td>
                    : null}
                </tr>
              ))}
            </tbody>
          </table>}
      </div>
    </div>
  );
}
