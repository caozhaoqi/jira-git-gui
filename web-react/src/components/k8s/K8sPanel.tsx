import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet } from '../../api/client';
import { useAppStore } from '../../store/useAppStore';
import type { K8sEnvsResp, K8sEnv } from '../../api/types';
import { K8sContext, type K8sTarget } from './context';
import { K8sSnapshot } from './K8sSnapshot';
import { K8sYaml } from './K8sYaml';
import { K8sNetwork } from './K8sNetwork';
import { K8sEvents } from './K8sEvents';
import { K8sTop } from './K8sTop';
import { K8sShell } from './K8sShell';
import { K8sFiles } from './K8sFiles';
import { K8sEnvModal } from './K8sEnvModal';
import { K8sDescribeModal, type DescribeSeed } from './K8sDescribeModal';

type SubTab = 'snapshot' | 'yaml' | 'network' | 'events' | 'top' | 'shell' | 'files';

const SUBTABS: { key: SubTab; label: string }[] = [
  { key: 'snapshot', label: '📸 快照' },
  { key: 'yaml', label: '📝 Pod YAML' },
  { key: 'network', label: '🌐 网络检测' },
  { key: 'events', label: '📡 事件' },
  { key: 'top', label: '📊 资源 Top' },
  { key: 'shell', label: '💻 Shell' },
  { key: 'files', label: '📁 文件' },
];

function envTagClass(name: string): string {
  const v = (name || '').toLowerCase();
  if (v.includes('prod') || v === 'prd' || v.includes('生产')) return 'k8s-env-tag env-prod';
  if (v.includes('test') || v.includes('uat') || v.includes('stag') || v.includes('测试') || v.includes('预发')) return 'k8s-env-tag env-test';
  if (v.includes('dev') || v.includes('开发') || v.includes('development')) return 'k8s-env-tag env-dev';
  return 'k8s-env-tag env-other';
}

function envTagText(envs: { name: string; label?: string }[], cur: string): string {
  const e = envs.find((x) => x.name === cur);
  return e ? `${e.label || e.name} (${e.name})` : cur || '—';
}

export function K8sPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);

  const [envs, setEnvs] = useState<K8sEnv[]>([]);
  const [target, setTargetState] = useState<K8sTarget>({ env: '', pod: '', container: '', namespace: '' });
  const [sub, setSub] = useState<SubTab>('snapshot');
  const [envModalOpen, setEnvModalOpen] = useState(false);
  const [describeSeed, setDescribeSeed] = useState<DescribeSeed | null>(null);
  const [kcText, setKcText] = useState('');

  const reloadEnvs = useCallback(async () => {
    try {
      const d = await apiGet<K8sEnvsResp>('/api/k8s/env');
      const list = d.environments || [];
      setEnvs(list);
      const cur = d.current || (list[0] && list[0].name) || '';
      setTargetState((t) => ({ ...t, env: cur }));
      const c = list.find((e) => e.name === cur);
      setKcText(c && c.kubeconfig ? 'kubeconfig: ' + c.kubeconfig : '未配置 kubeconfig');
      if (d.error) pushLog(`加载 K8s 环境失败：${d.error}`, 'error');
    } catch (ex: any) {
      pushLog(`加载 K8s 环境失败：${ex.message}`, 'error');
    }
  }, [pushLog]);

  useEffect(() => {
    reloadEnvs();
  }, [reloadEnvs]);

  const setTarget = useCallback((t: Partial<K8sTarget>) => {
    setTargetState((prev) => {
      const next = { ...prev, ...t };
      if (t.env && t.env !== prev.env) {
        // 切换环境：重置 pod/container/namespace（namespace 沿用默认）
        next.pod = '';
        next.container = '';
      }
      return next;
    });
  }, []);

  const openDescribe = useCallback((kind: string, name: string, namespace?: string) => {
    setDescribeSeed({ kind: kind || 'pod', name: name || '', namespace: namespace || '' });
  }, []);

  const ctx = useMemo(
    () => ({ envs, target, setTarget, reloadEnvs, pushLog, addToast, openDescribe }),
    [envs, target, setTarget, reloadEnvs, pushLog, addToast, openDescribe]
  );

  const onEnvChange = (name: string) => {
    setTarget({ env: name });
    const c = envs.find((e) => e.name === name);
    setKcText(c && c.kubeconfig ? 'kubeconfig: ' + c.kubeconfig : '未配置 kubeconfig');
  };

  return (
    <K8sContext.Provider value={ctx}>
      <div className="k8s-panel">
        <div className="k8s-envbar card-soft">
          <label className="field-inline">
            环境
            <select className="sel" value={target.env} onChange={(e) => onEnvChange(e.target.value)}>
              {envs.length === 0 && <option value="">（无环境）</option>}
              {envs.map((e) => (
                <option key={e.name} value={e.name}>{e.label || e.name} ({e.name})</option>
              ))}
            </select>
          </label>
          <span className={envTagClass(target.env)}>{envTagText(envs, target.env)}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setEnvModalOpen(true)}>管理环境</button>
          <span className="k8s-env-kc panel-sub">{kcText}</span>
        </div>

        <div className="k8s-subtabs">
          {SUBTABS.map((t) => (
            <button
              key={t.key}
              className={'k8s-subtab' + (sub === t.key ? ' active' : '')}
              data-sub={t.key}
              onClick={() => setSub(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="k8s-subpane active" style={{ display: 'block' }}>
          {sub === 'snapshot' && <K8sSnapshot />}
          {sub === 'yaml' && <K8sYaml />}
          {sub === 'network' && <K8sNetwork />}
          {sub === 'events' && <K8sEvents />}
          {sub === 'top' && <K8sTop />}
          {sub === 'shell' && <K8sShell />}
          {sub === 'files' && <K8sFiles />}
        </div>

        {envModalOpen && <K8sEnvModal onClose={() => setEnvModalOpen(false)} />}
        {describeSeed && <K8sDescribeModal seed={describeSeed} onClose={() => setDescribeSeed(null)} />}
      </div>
    </K8sContext.Provider>
  );
}
