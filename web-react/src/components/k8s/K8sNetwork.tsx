import { useCallback, useState } from 'react';
import { apiPost } from '../../api/client';
import type { K8sNetworkResp, K8sNetCheck, K8sNetIntranet } from '../../api/types';
import { useK8s } from './context';

function statusIcon(s?: string): string {
  return s === 'ok' ? '✓' : s === 'fail' ? '✕' : '!';
}

export function K8sNetwork() {
  const { target, addToast } = useK8s();
  const [hosts, setHosts] = useState('');
  const [res, setRes] = useState<K8sNetworkResp | null>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(async () => {
    const list = hosts.split('\n').map((s) => s.trim()).filter(Boolean);
    setBusy(true);
    try {
      const d = await apiPost<K8sNetworkResp>('/api/k8s/network', { env: target.env, extra_hosts: list });
      if (!d.ok) { setRes({ ...d, summary: '失败：' + (d.error || '') }); return; }
      setRes(d);
    } catch (ex: any) {
      setRes({ summary: '失败：' + ex.message });
      addToast(ex.message, 'error');
    } finally {
      setBusy(false);
    }
  }, [hosts, target.env, addToast]);

  const checks: K8sNetCheck[] = res?.checks || [];
  const intranet: K8sNetIntranet[] = res?.intranet || [];

  return (
    <div className="k8s-net">
      <div className="k8s-net-cfg">
        <label>额外检测主机（每行一个）</label>
        <textarea className="input" rows={4} value={hosts} onChange={(e) => setHosts(e.target.value)} placeholder={'example.com\n10.0.0.1'} />
        <button className="btn btn-sm" onClick={run} disabled={busy || !target.env}>检测</button>
      </div>
      {res && <div className="k8s-net-summary">{res.summary}</div>}
      <div className="k8s-net-checks">
        {checks.length === 0 ? <div className="empty-hint">点击「检测」运行网络连通性检查</div> :
          checks.map((c, i) => (
            <div key={i} className="k8s-check">
              <div className={`k8s-chk-ico ${c.status || ''}`}>{statusIcon(c.status)}</div>
              <div><div className="k8s-chk-name">{c.name}</div><div className="k8s-chk-detail">{c.detail || ''}</div></div>
            </div>
          ))}
      </div>
      <div className="k8s-net-intranet">
        {intranet.map((r, i) => (
          <div key={i} className="k8s-check">
            <div className={`k8s-chk-ico ${r.ok ? 'ok' : 'fail'}`}>{r.ok ? '✓' : '✕'}</div>
            <div><div className="k8s-chk-name">{r.target}</div><div className="k8s-chk-detail">{r.ok ? `可达 (延迟 ${r.ms}ms)` : '不可达'}</div></div>
          </div>
        ))}
      </div>
      {res && (
        <div className={'k8s-net-verdict ' + (res.cluster_ok ? 'ok' : 'fail')}>
          {res.cluster_ok ? '判定：当前可连接该环境集群与内网，可正常运维。' : '判定：未连通集群（可能未接入对应内网/VPN 或 kubeconfig 缺失）。请确认后重试。'}
        </div>
      )}
    </div>
  );
}
