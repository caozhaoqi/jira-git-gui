import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import type { K8sEnvsResp, K8sEnv } from '../../api/types';

interface EnvForm {
  name: string;
  label: string;
  kubeconfig: string;
  context: string;
  namespace: string;
  intranet: string;
}

const EMPTY: EnvForm = { name: '', label: '', kubeconfig: '', context: '', namespace: 'default', intranet: '' };

export function K8sEnvModal({ onClose }: { onClose: () => void }) {
  const [list, setList] = useState<K8sEnv[]>([]);
  const [form, setForm] = useState<EnvForm>(EMPTY);
  const [msg, setMsg] = useState('');

  const loadList = useCallback(async () => {
    try {
      const d = await apiGet<K8sEnvsResp>('/api/k8s/env');
      setList(d.environments || []);
    } catch (ex: any) {
      setMsg('环境列表失败：' + ex.message);
    }
  }, []);

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
    });
    setMsg('');
  };

  const save = async () => {
    if (!form.name.trim()) { setMsg('请填写环境标识'); return; }
    const body = {
      name: form.name.trim(),
      label: form.label.trim(),
      kubeconfig: form.kubeconfig.trim(),
      context: form.context.trim(),
      namespace: form.namespace.trim() || 'default',
      intranet_hosts: form.intranet.split('\n').map((s) => s.trim()).filter(Boolean),
    };
    try {
      await apiPost('/api/k8s/env', body);
      setMsg('已保存');
      await loadList();
    } catch (ex: any) {
      setMsg('失败：' + ex.message);
    }
  };

  const switchEnv = async () => {
    if (!form.name.trim()) { setMsg('请先填写环境标识'); return; }
    try {
      await apiPost('/api/k8s/env/switch', { name: form.name.trim() });
      setMsg('已切换');
      await loadList();
    } catch (ex: any) {
      setMsg('失败：' + ex.message);
    }
  };

  const del = async () => {
    if (!form.name.trim()) return;
    try {
      await apiPost('/api/k8s/env/delete', { name: form.name.trim() });
      setMsg('已删除');
      setForm(EMPTY);
      await loadList();
    } catch (ex: any) {
      setMsg('失败：' + ex.message);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>K8s 环境管理</h3>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">
          <div className="k8s-env-list">
            {list.length === 0 && <div className="empty-hint">暂无环境</div>}
            {list.map((e) => (
              <div key={e.name} className="k8s-env-item" onClick={() => fill(e)}>
                <span className="nm">{e.label || e.name}</span>
                <span className="nm">({e.name})</span>
                <span className="kc">{e.kubeconfig || '(无 kubeconfig)'}</span>
                {e.is_current && <span className="cur">当前</span>}
              </div>
            ))}
          </div>
          <div className="k8s-env-form">
            <div className="form-row"><label>标识</label><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div className="form-row"><label>标签</label><input className="input" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} /></div>
            <div className="form-row"><label>kubeconfig</label><input className="input" value={form.kubeconfig} onChange={(e) => setForm({ ...form, kubeconfig: e.target.value })} /></div>
            <div className="form-row"><label>context</label><input className="input" value={form.context} onChange={(e) => setForm({ ...form, context: e.target.value })} /></div>
            <div className="form-row"><label>命名空间</label><input className="input" value={form.namespace} onChange={(e) => setForm({ ...form, namespace: e.target.value })} /></div>
            <div className="form-row"><label>内网主机</label><textarea className="input" rows={3} value={form.intranet} onChange={(e) => setForm({ ...form, intranet: e.target.value })} /></div>
          </div>
        </div>
        <div className="modal-footer">
          <span className="k8s-env-msg">{msg}</span>
          <div className="spacer" />
          <button className="btn btn-sm" onClick={save}>保存</button>
          <button className="btn btn-sm btn-ghost" onClick={switchEnv}>切换</button>
          <button className="btn btn-sm btn-ghost" onClick={del}>删除</button>
        </div>
      </div>
    </div>
  );
}
