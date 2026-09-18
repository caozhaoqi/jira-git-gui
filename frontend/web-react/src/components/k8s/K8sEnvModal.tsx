import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import type { K8sEnvsResp, K8sEnv } from '../../api/types';
import { useT } from '../../i18n';

interface EnvForm {
  name: string;
  label: string;
  kubeconfig: string;
  context: string;
  namespace: string;
  intranet: string;
  ssh_host: string;
  ssh_port: string;
  ssh_user: string;
  ssh_password: string;
}

const EMPTY: EnvForm = {
  name: '', label: '', kubeconfig: '', context: '', namespace: 'default', intranet: '',
  ssh_host: '', ssh_port: '22', ssh_user: '', ssh_password: '',
};

export function K8sEnvModal({ onClose }: { onClose: () => void }) {
  const { t } = useT();
  const [list, setList] = useState<K8sEnv[]>([]);
  const [form, setForm] = useState<EnvForm>(EMPTY);
  const [msg, setMsg] = useState('');

  const loadList = useCallback(async () => {
    try {
      const d = await apiGet<K8sEnvsResp>('/api/k8s/env');
      setList(d.environments || []);
    } catch (ex: any) {
      setMsg(t('k8s.env.listFail') + ex.message);
    }
  }, [t]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  const fill = (e: K8sEnv) => {
    setForm({
      name: e.name,
      label: e.label || '',
      kubeconfig: e.kubeconfig || '',
      context: e.context || '',
      namespace: e.namespace || 'default',
      intranet: (e.intranet_hosts || []).join('\n'),
      ssh_host: e.ssh_host || '',
      ssh_port: e.ssh_port || '22',
      ssh_user: e.ssh_user || '',
      ssh_password: e.ssh_password || '',
    });
    setMsg('');
  };

  const save = async () => {
    if (!form.name.trim()) { setMsg(t('k8s.env.nameRequired')); return; }
    const body = {
      name: form.name.trim(),
      label: form.label.trim(),
      kubeconfig: form.kubeconfig.trim(),
      context: form.context.trim(),
      namespace: form.namespace.trim() || 'default',
      intranet_hosts: form.intranet.split('\n').map((s) => s.trim()).filter(Boolean),
      ssh_host: form.ssh_host.trim(),
      ssh_port: form.ssh_port.trim() || '22',
      ssh_user: form.ssh_user.trim(),
      ssh_password: form.ssh_password,
    };
    try {
      await apiPost('/api/k8s/env', body);
      setMsg(t('k8s.env.saved'));
      await loadList();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
    }
  };

  const switchEnv = async () => {
    if (!form.name.trim()) { setMsg(t('k8s.env.nameRequired')); return; }
    try {
      await apiPost('/api/k8s/env/switch', { name: form.name.trim() });
      setMsg(t('k8s.env.switched'));
      await loadList();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
    }
  };

  const del = async () => {
    if (!form.name.trim()) return;
    try {
      await apiPost('/api/k8s/env/delete', { name: form.name.trim() });
      setMsg(t('k8s.env.deleted'));
      setForm(EMPTY);
      await loadList();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{t('k8s.env.title')}</h3>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          <div className="k8s-env-list">
            {list.length === 0 && <div className="empty-hint">{t('k8s.env.empty')}</div>}
            {list.map((e) => (
              <div key={e.name} className="k8s-env-item" onClick={() => fill(e)}>
                <span className="nm">{e.label || e.name}</span>
                <span className="nm">({e.name})</span>
                <span className="kc">{e.ssh_host ? `SSH ${e.ssh_user || 'root'}@${e.ssh_host}${e.ssh_port && e.ssh_port !== '22' ? ':' + e.ssh_port : ''}` : (e.kubeconfig || t('k8s.env.noKubeconfigShort'))}</span>
                {e.is_current && <span className="cur">{t('k8s.env.current')}</span>}
              </div>
            ))}
          </div>
          <div className="k8s-env-form">
            <div className="form-row"><label>{t('k8s.env.id')}</label><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.label')}</label><input className="input" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.kubeconfig')}</label><input className="input" value={form.kubeconfig} onChange={(e) => setForm({ ...form, kubeconfig: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.context')}</label><input className="input" value={form.context} onChange={(e) => setForm({ ...form, context: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.namespace')}</label><input className="input" value={form.namespace} onChange={(e) => setForm({ ...form, namespace: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.intranet')}</label><textarea className="input" rows={3} value={form.intranet} onChange={(e) => setForm({ ...form, intranet: e.target.value })} /></div>
            {/* SSH 远程环境：填了 Host 即经账密 SSH 登录远端，在远端执行 kubectl（用远端自身 kubeconfig） */}
            <div className="form-row"><label>{t('k8s.env.sshHost')}</label><input className="input" placeholder={t('k8s.env.sshHint')} value={form.ssh_host} onChange={(e) => setForm({ ...form, ssh_host: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.sshPort')}</label><input className="input" value={form.ssh_port} onChange={(e) => setForm({ ...form, ssh_port: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.sshUser')}</label><input className="input" autoComplete="off" value={form.ssh_user} onChange={(e) => setForm({ ...form, ssh_user: e.target.value })} /></div>
            <div className="form-row"><label>{t('k8s.env.sshPassword')}</label><input className="input" type="password" autoComplete="new-password" value={form.ssh_password} onChange={(e) => setForm({ ...form, ssh_password: e.target.value })} /></div>
          </div>
        </div>
        <div className="modal-footer">
          <span className="k8s-env-msg">{msg}</span>
          <div className="spacer" />
          <button className="btn btn-sm" onClick={save}>{t('k8s.env.save')}</button>
          <button className="btn btn-sm btn-ghost" onClick={switchEnv}>{t('k8s.env.switch')}</button>
          <button className="btn btn-sm btn-ghost" onClick={del}>{t('k8s.env.delete')}</button>
        </div>
      </div>
    </div>
  );
}
