import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';
import type { K8sTopResp, K8sTopRow } from '../../api/types';
import { useK8s } from './context';
import { parseTopVal } from '../../utils/format';

export function K8sTop() {
  const { target } = useK8s();
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
      if (!d.ok) { setSummary('失败：' + (d.error || '')); return; }
      setRows(d.rows || []);
      setSummary('共 ' + (d.rows || []).length + ' 个');
    } catch (ex: any) {
      setSummary('失败：' + ex.message);
    } finally {
      setBusy(false);
    }
  }, [target.env, scope, ns]);

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
          <option value="pods">Pod</option>
          <option value="nodes">Node</option>
        </select>
        {scope === 'pods' && <input className="input input-sm" value={ns} onChange={(e) => setNs(e.target.value)} placeholder="命名空间" />}
        <label className="chk"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> 自动(10s)</label>
        <button className="btn btn-sm" onClick={load} disabled={busy || !target.env}>刷新</button>
      </div>
      <div className="k8s-top-summary">{summary}</div>
      <div className="k8s-table-wrap">
        {rows.length === 0 ? <div className="empty-hint">无数据（集群需启用 metrics-server）</div> :
          <table className="k8s-top-table">
            <thead>
              {scope === 'nodes'
                ? <tr><th>节点</th><th>CPU</th><th>CPU%</th><th>内存</th><th>内存%</th></tr>
                : <tr><th>Pod</th><th>命名空间</th><th>CPU</th><th>内存</th></tr>}
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
