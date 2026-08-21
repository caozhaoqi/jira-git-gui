import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client';
import type { K8sEventsResp, K8sEvent } from '../../api/types';
import { useK8s } from './context';

function fmtTime(iso?: string): string {
  if (!iso) return '';
  return (iso || '').replace('T', ' ').replace('Z', '').slice(0, 19);
}

export function K8sEvents() {
  const { target } = useK8s();
  const [ns, setNs] = useState('');
  const [kind, setKind] = useState('');
  const [name, setName] = useState('');
  const [limit, setLimit] = useState(200);
  const [allNs, setAllNs] = useState(false);
  const [auto, setAuto] = useState(false);
  const [events, setEvents] = useState<K8sEvent[]>([]);
  const [summary, setSummary] = useState('');
  const [busy, setBusy] = useState(false);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const q = new URLSearchParams({ env: target.env, limit: String(limit || 200) });
      if (ns.trim()) q.set('namespace', ns.trim());
      if (kind.trim()) q.set('kind', kind.trim());
      if (name.trim()) q.set('name', name.trim());
      if (allNs) q.set('all_ns', '1');
      const d = await api<K8sEventsResp>('/api/k8s/events?' + q.toString());
      if (!d.ok) { setSummary('失败：' + (d.error || '')); return; }
      setEvents(d.events || []);
      setSummary('共 ' + (d.total ?? 0) + ' 条' + (d.warning ? ` · ⚠ Warning ${d.warning}` : ''));
    } catch (ex: any) {
      setSummary('失败：' + ex.message);
    } finally {
      setBusy(false);
    }
  }, [target.env, ns, kind, name, limit, allNs]);

  useEffect(() => {
    if (auto) {
      load();
      timerRef.current = window.setInterval(() => { load(); }, 10000);
    }
    return () => {
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    };
  }, [auto, load]);

  return (
    <div className="k8s-events">
      <div className="k8s-ev-cfg">
        <input className="input input-sm" value={ns} onChange={(e) => setNs(e.target.value)} placeholder="命名空间" />
        <input className="input input-sm" value={kind} onChange={(e) => setKind(e.target.value)} placeholder="类型" />
        <input className="input input-sm" value={name} onChange={(e) => setName(e.target.value)} placeholder="对象名" />
        <input className="input input-sm" type="number" value={limit} onChange={(e) => setLimit(Number(e.target.value))} style={{ width: 90 }} />
        <label className="chk"><input type="checkbox" checked={allNs} onChange={(e) => setAllNs(e.target.checked)} /> 全命名空间</label>
        <label className="chk"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> 自动(10s)</label>
        <button className="btn btn-sm" onClick={load} disabled={busy || !target.env}>刷新</button>
      </div>
      <div className="k8s-ev-summary">{summary}</div>
      <div className="k8s-table-wrap">
        {events.length === 0 ? <div className="empty-hint">无事件</div> :
          <table className="k8s-ev-table">
            <thead><tr><th>时间</th><th>类型</th><th>原因</th><th>对象</th><th>来源</th><th>次数</th><th>消息</th></tr></thead>
            <tbody>
              {events.map((e, i) => {
                const obj = `${e.object_kind || ''}/${e.object_name || ''}${e.object_ns ? ` (${e.object_ns})` : ''}`;
                return (
                  <tr key={i} className={'ev-' + (e.type || 'Normal')}>
                    <td className="ev-time">{fmtTime(e.last_seen)}</td>
                    <td className="ev-type">{e.type || ''}</td>
                    <td className="ev-reason">{e.reason || ''}</td>
                    <td className="ev-obj" title={obj}>{obj}</td>
                    <td>{e.source || ''}</td>
                    <td className="ev-count">{String(e.count || 1)}</td>
                    <td className="ev-msg" title={e.message || ''}>{e.message || ''}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>}
      </div>
    </div>
  );
}
