import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiGet } from '../../api/client';
import { useAppStore } from '../../store/useAppStore';
import { useT } from '../../i18n';
import type { K8sEnvsResp, K8sEnv } from '../../api/types';
import { clearK8sEnvAlert, raiseK8sEnvAlert } from '../../utils/appAlerts';
import { envTagClass, envTagText, envKindKey } from '../../utils/k8sEnv';
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

const SUBTABS: { key: SubTab; labelKey: string }[] = [
  { key: 'snapshot', labelKey: 'k8s.subtabs.snapshot' },
  { key: 'yaml', labelKey: 'k8s.subtabs.yaml' },
  { key: 'network', labelKey: 'k8s.subtabs.network' },
  { key: 'events', labelKey: 'k8s.subtabs.events' },
  { key: 'top', labelKey: 'k8s.subtabs.top' },
  { key: 'shell', labelKey: 'k8s.subtabs.shell' },
  { key: 'files', labelKey: 'k8s.subtabs.files' },
];

export function K8sPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const aiDraftToInsert = useAppStore((s) => s.aiDraftToInsert);
  const { t } = useT();

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
      setKcText(c && c.kubeconfig ? 'kubeconfig: ' + c.kubeconfig : t('k8s.noKubeconfig'));
      if (d.error) {
        pushLog(`加载 K8s 环境失败：${d.error}`, 'error');
        raiseK8sEnvAlert(d.error, () => setEnvModalOpen(true));
      } else if (list.length === 0) {
        // 一个环境都没配：下拉框只剩「（无环境）」，此前只写一行日志，
        // 用户不知道要先去配置 kubeconfig。给同一条可操作提醒（直达环境管理）。
        raiseK8sEnvAlert('', () => setEnvModalOpen(true));
      } else {
        clearK8sEnvAlert();
      }
    } catch (ex: any) {
      const msg = ex?.message || String(ex);
      pushLog(`加载 K8s 环境失败：${msg}`, 'error');
      raiseK8sEnvAlert(msg, () => setEnvModalOpen(true));
    }
  }, [pushLog, t]);

  useEffect(() => {
    reloadEnvs();
  }, [reloadEnvs]);

  useEffect(() => {
    if (aiDraftToInsert?.target === 'k8s_yaml') setSub('yaml');
  }, [aiDraftToInsert]);

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

  // 子 tab（如快照）内直达「管理环境」弹窗：环境配置能力此前只在顶部 envbar 暴露，
  // 用户在快照配置区看不到当前环境、也没有新增环境入口。
  const openEnvModal = useCallback(() => setEnvModalOpen(true), []);

  const ctx = useMemo(
    () => ({ envs, target, setTarget, reloadEnvs, pushLog, addToast, openDescribe, openEnvModal }),
    [envs, target, setTarget, reloadEnvs, pushLog, addToast, openDescribe, openEnvModal]
  );

  const onEnvChange = (name: string) => {
    setTarget({ env: name });
    const c = envs.find((e) => e.name === name);
    setKcText(c && c.kubeconfig ? 'kubeconfig: ' + c.kubeconfig : t('k8s.noKubeconfig'));
  };

  return (
    <K8sContext.Provider value={ctx}>
      <div className="k8s-panel">
        <div className="k8s-envbar card-soft">
          <label className="field-inline">
            {t('k8s.snapshot.env')}
            <select className="sel" value={target.env} onChange={(e) => onEnvChange(e.target.value)}>
              {envs.length === 0 && <option value="">{t('k8s.noEnv')}</option>}
              {envs.map((e) => (
                <option key={e.name} value={e.name}>{e.label || e.name} ({e.name})</option>
              ))}
            </select>
          </label>
          <span
            className={envTagClass(target.env)}
            title={envTagText(envs, target.env)}
          >
            {target.env ? t(envKindKey(target.env)) : '—'}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={() => setEnvModalOpen(true)}>{t('k8s.manageEnv')}</button>
          <span className="k8s-env-kc panel-sub">{kcText}</span>
        </div>

        <div className="k8s-subtabs">
          {SUBTABS.map((st) => (
            <button
              key={st.key}
              className={'k8s-subtab' + (sub === st.key ? ' active' : '')}
              data-sub={st.key}
              onClick={() => setSub(st.key)}
            >
              {t(st.labelKey)}
            </button>
          ))}
        </div>

        {/* 始终挂载所有子标签、用 display 切换显隐，避免切换标签时组件卸载导致
            本地状态 / 连接丢失（shell 的 WebSocket+xterm、files/yaml 的未保存编辑、
            snapshot 的在途抓取、events/top 的自动刷新等都会因卸载而复位）。*/}
        <div className="k8s-subpane active">
          <div className="k8s-subtab-pane" style={{ display: sub === 'snapshot' ? 'flex' : 'none' }}><K8sSnapshot /></div>
          <div className="k8s-subtab-pane" style={{ display: sub === 'yaml' ? 'flex' : 'none' }}><K8sYaml /></div>
          <div className="k8s-subtab-pane" style={{ display: sub === 'network' ? 'flex' : 'none' }}><K8sNetwork /></div>
          <div className="k8s-subtab-pane" style={{ display: sub === 'events' ? 'flex' : 'none' }}><K8sEvents /></div>
          <div className="k8s-subtab-pane" style={{ display: sub === 'top' ? 'flex' : 'none' }}><K8sTop /></div>
          <div className="k8s-subtab-pane" style={{ display: sub === 'shell' ? 'flex' : 'none' }}><K8sShell /></div>
          <div className="k8s-subtab-pane" style={{ display: sub === 'files' ? 'flex' : 'none' }}><K8sFiles /></div>
        </div>

        {envModalOpen && <K8sEnvModal onClose={() => setEnvModalOpen(false)} />}
        {describeSeed && <K8sDescribeModal seed={describeSeed} onClose={() => setDescribeSeed(null)} />}
      </div>
    </K8sContext.Provider>
  );
}
