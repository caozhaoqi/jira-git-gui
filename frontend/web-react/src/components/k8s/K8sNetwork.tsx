import { useCallback, useState } from 'react';
import { apiPost } from '../../api/client';
import type { K8sNetworkResp, K8sNetCheck, K8sNetIntranet } from '../../api/types';
import { useK8s } from './context';
import { useT } from '../../i18n';

function statusIcon(s?: string): string {
  return s === 'ok' ? '✓' : s === 'fail' ? '✕' : '!';
}

export function K8sNetwork() {
  const { target, addToast } = useK8s();
  const { t } = useT();
  const [hosts, setHosts] = useState('');
  const [res, setRes] = useState<K8sNetworkResp | null>(null);
  const [busy, setBusy] = useState(false);

  const run = useCallback(async () => {
    const list = hosts.split('\n').map((s) => s.trim()).filter(Boolean);
    setBusy(true);
    try {
      const d = await apiPost<K8sNetworkResp>('/api/k8s/network', { env: target.env, extra_hosts: list });
      if (!d.ok) { setRes({ ...d, summary: t('k8s.network.fail') + (d.error || '') }); return; }
      setRes(d);
    } catch (ex: any) {
      setRes({ summary: t('k8s.network.fail') + ex.message });
      addToast(ex.message, 'error');
    } finally {
      setBusy(false);
    }
  }, [hosts, target.env, addToast, t]);

  const checks: K8sNetCheck[] = res?.checks || [];
  const intranet: K8sNetIntranet[] = res?.intranet || [];

  return (
    <div className="k8s-net">
      <div className="k8s-net-cfg">
        <label>{t('k8s.network.extraHosts')}</label>
        <textarea className="input" rows={4} value={hosts} onChange={(e) => setHosts(e.target.value)} placeholder={'example.com\n10.0.0.1'} />
        <button className="btn btn-sm" onClick={run} disabled={busy || !target.env}>{t('k8s.network.check')}</button>
      </div>
      {res && <div className="k8s-net-summary">{res.summary}</div>}
      <div className="k8s-net-checks">
        {checks.length === 0 ? <div className="empty-hint">{t('k8s.network.runHint')}</div> :
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
            <div><div className="k8s-chk-name">{r.target}</div><div className="k8s-chk-detail">{r.ok ? t('k8s.network.reachable', { ms: r.ms ?? 0 }) : t('k8s.network.unreachable')}</div></div>
          </div>
        ))}
      </div>
      {res && (
        <div className={'k8s-net-verdict ' + (res.cluster_ok ? 'ok' : 'fail')}>
          {res.cluster_ok ? t('k8s.network.verdictOk') : t('k8s.network.verdictFail')}
        </div>
      )}
    </div>
  );
}
