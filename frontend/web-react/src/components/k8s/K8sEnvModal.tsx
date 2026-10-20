import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import type { K8sEnvsResp, K8sEnv } from '../../api/types';
import { useT } from '../../i18n';
import { useModalA11y } from '../../utils/useModalA11y';
import { envTagText } from '../../utils/k8sEnv';
import { useK8s } from './context';

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
  // 保存/切换/删除后同步刷新 Panel 的环境列表与当前环境（envbar 下拉、kcText、target.env），
  // 否则新增的环境不出现在顶部下拉里、切换后 target.env 与后端 current 不一致。
  const { reloadEnvs } = useK8s();
  const dialogRef = useModalA11y<HTMLDivElement>(onClose);
  const [list, setList] = useState<K8sEnv[]>([]);
  const [form, setForm] = useState<EnvForm>(EMPTY);
  const [msg, setMsg] = useState('');
  // 点选（回填表单）的环境名：给列表项加 selected 高亮，否则看不出点了哪个
  const [selName, setSelName] = useState('');
  // 删除二次确认：首次点击只武装按钮（变红「确认删除？」），5 秒内再点才真删，
  // 防止误触丢配置；超时/其他操作自动解除。
  const [delArmed, setDelArmed] = useState(false);
  const idInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!delArmed) return;
    const timer = setTimeout(() => setDelArmed(false), 5000);
    return () => clearTimeout(timer);
  }, [delArmed]);

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
    setSelName(e.name);
    setMsg('');
    setDelArmed(false);
  };

  /** 进入「新建环境」模式：清空表单、取消列表选中、焦点落到标识输入框。 */
  const startNew = () => {
    setForm(EMPTY);
    setSelName('');
    setMsg('');
    setDelArmed(false);
    idInputRef.current?.focus();
  };

  const loadList = useCallback(async (initial = false) => {
    try {
      const d = await apiGet<K8sEnvsResp>('/api/k8s/env');
      const envs = d.environments || [];
      setList(envs);
      // 首次加载锚定「Current」项（与绿色徽标一致）并回填表单：此前打开弹窗
      // 列表高亮着当前环境、表单却是空的，用户不知道该填还是该点。
      setSelName((prev) => prev || (envs.find((x) => x.is_current) || ({} as K8sEnv)).name || '');
      if (initial) {
        const cur = envs.find((x) => x.is_current);
        if (cur) fill(cur);
      }
    } catch (ex: any) {
      setMsg(t('k8s.env.listFail') + ex.message);
    }
  }, [t]);

  useEffect(() => {
    loadList(true);
  }, [loadList]);

  // 标识是否已存在：决定保存按钮是「创建」还是「保存」，以及切换/删除是否可用。
  // 标识为空（刚点过「新增」）同样归入新建态：模式标签显示「新建环境」、主按钮显示「创建」。
  const nameExists = !!form.name.trim() && list.some((e) => e.name === form.name.trim());
  const isNewEnv = !nameExists;

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
      setMsg(isNewEnv ? t('k8s.env.created') : t('k8s.env.saved'));
      setSelName(body.name);
      setDelArmed(false);
      await loadList();
      await reloadEnvs();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
    }
  };

  const switchEnv = async () => {
    if (!form.name.trim()) { setMsg(t('k8s.env.nameRequired')); return; }
    try {
      await apiPost('/api/k8s/env/switch', { name: form.name.trim() });
      setMsg(t('k8s.env.switched'));
      setDelArmed(false);
      await loadList();
      await reloadEnvs();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
    }
  };

  const del = async () => {
    if (!form.name.trim()) return;
    // 二次确认：首次点击只武装，再点才真删
    if (!delArmed) { setDelArmed(true); return; }
    try {
      await apiPost('/api/k8s/env/delete', { name: form.name.trim() });
      setMsg(t('k8s.env.deleted'));
      setForm(EMPTY);
      setSelName('');
      setDelArmed(false);
      await loadList();
      await reloadEnvs();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="k8s-env-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3 id="k8s-env-title">{t('k8s.env.title')}</h3>
          <button className="btn btn-sm btn-ghost" onClick={onClose} aria-label={t('common.close')}>✕</button>
        </div>
        <div className="modal-body">
          <div className="k8s-env-list">
            {list.length === 0 && <div className="empty-hint">{t('k8s.env.empty')}</div>}
            {list.map((e) => (
              <div
                key={e.name}
                className={'k8s-env-item' + (selName === e.name ? ' selected' : '')}
                role="button"
                tabIndex={0}
                aria-pressed={selName === e.name}
                onClick={() => fill(e)}
                onKeyDown={(ev) => {
                  if (ev.key === 'Enter' || ev.key === ' ') {
                    ev.preventDefault();
                    fill(e);
                  }
                }}
              >
                <span className="nm">{e.label || e.name}</span>
                <span className="nm">({e.name})</span>
                <span className="kc">{e.ssh_host ? `SSH ${e.ssh_user || 'root'}@${e.ssh_host}${e.ssh_port && e.ssh_port !== '22' ? ':' + e.ssh_port : ''}` : (e.kubeconfig || t('k8s.env.noKubeconfigShort'))}</span>
                {e.is_current && <span className="cur">{t('k8s.env.current')}</span>}
              </div>
            ))}
          </div>
          <div className="k8s-env-form">
            <div className="k8s-env-form-mode">
              <span className={isNewEnv ? 'k8s-env-mode-tag new' : 'k8s-env-mode-tag edit'}>
                {isNewEnv ? t('k8s.env.modeNew') : t('k8s.env.modeEdit')}
              </span>
              <span className="k8s-env-form-mode-text">
                {nameExists
                  ? envTagText(list, form.name.trim())
                  : t('k8s.env.newHint')}
              </span>
            </div>
            <div className="form-row"><label>{t('k8s.env.id')}</label><input ref={idInputRef} className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t('k8s.env.idPh')} /></div>
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
          {/* 按钮层级：主提交（创建/保存）唯一 primary 放中间；新增/切换/删除为 ghost；
              删除放最右且需二次确认（武装态变红），防误触丢配置。 */}
          <button className="btn btn-sm btn-ghost" onClick={startNew}>{t('k8s.env.newBtn')}</button>
          <button
            className="btn btn-sm btn-primary"
            onClick={save}
            disabled={!form.name.trim()}
            title={!form.name.trim() ? t('k8s.env.nameRequired') : undefined}
          >
            {isNewEnv ? t('k8s.env.create') : t('k8s.env.save')}
          </button>
          <button className="btn btn-sm btn-ghost" onClick={switchEnv} disabled={!nameExists} title={nameExists ? undefined : t('k8s.env.switchNeedExisting')}>
            {t('k8s.env.switch')}
          </button>
          <button
            className={`btn btn-sm${delArmed ? ' btn-danger' : ' btn-ghost'}`}
            onClick={del}
            disabled={!form.name.trim()}
            title={delArmed ? t('k8s.env.confirmDeleteTip') : undefined}
          >
            {delArmed ? t('k8s.env.confirmDelete') : t('k8s.env.delete')}
          </button>
        </div>
      </div>
    </div>
  );
}
