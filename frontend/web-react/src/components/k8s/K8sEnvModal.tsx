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
  const { reloadEnvs, addToast } = useK8s();
  const dialogRef = useModalA11y<HTMLDivElement>(onClose);
  const [list, setList] = useState<K8sEnv[]>([]);
  const [form, setForm] = useState<EnvForm>(EMPTY);
  const [msg, setMsg] = useState('');
  // 显式模式：edit=改已有（ID 锁定防「改 ID 静默变新增」）；new=创建（ID 可填、撞车拦截）。
  // 取代旧版「ID 是否在列表中」的隐式判定——那个设计让同一个保存按钮
  // 有时更新有时创建，用户改一下 ID 就静默多出一个环境。
  const [mode, setMode] = useState<'edit' | 'new'>('edit');
  // 列表选中项（回填表单的那一项）
  const [selName, setSelName] = useState('');
  // 删除二次确认：首次点击只武装按钮（变红「确认删除？」），5 秒内再点才真删
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
    setMode('edit');
    setDelArmed(false);
  };

  /** 进入「新建」模式：清空表单、取消列表选中、焦点落到标识输入框。 */
  const startNew = () => {
    setForm(EMPTY);
    setSelName('');
    setMode('new');
    setMsg('');
    setDelArmed(false);
    idInputRef.current?.focus();
  };

  const loadList = useCallback(async (initial = false): Promise<K8sEnv[]> => {
    try {
      const d = await apiGet<K8sEnvsResp>('/api/k8s/env');
      const envs = d.environments || [];
      setList(envs);
      // 首次加载锚定「Current」项并回填表单：打开弹窗即有明确编辑对象；
      // 一个环境都没有时直接进新建态（列表空，编辑无从谈起）。
      setSelName((prev) => prev || (envs.find((x) => x.is_current) || ({} as K8sEnv)).name || '');
      if (initial) {
        const cur = envs.find((x) => x.is_current);
        if (cur) fill(cur);
        else startNew();
      }
      return envs;
    } catch (ex: any) {
      setMsg(t('k8s.env.listFail') + ex.message);
      return [];
    }
  }, [t]);

  useEffect(() => {
    loadList(true);
  }, [loadList]);

  // 新建模式下标识撞车 → 禁用创建按钮：新建永远不会静默覆盖已有环境
  const idTaken = mode === 'new' && !!form.name.trim() && list.some((e) => e.name === form.name.trim());
  const canEdit = mode === 'edit' && !!form.name.trim();

  const save = async () => {
    if (!form.name.trim()) { setMsg(t('k8s.env.nameRequired')); return; }
    if (idTaken) {
      setMsg(t('k8s.env.idExists'));
      addToast(t('k8s.env.idExists'), 'warn');
      return;
    }
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
      if (mode === 'new') {
        // 创建成功 → 落到新环境的编辑态，「保存修改」与「创建环境」语义始终唯一
        setSelName(body.name);
        setMode('edit');
        setMsg(t('k8s.env.created'));
        addToast(t('k8s.env.toastCreated', { name: body.name }), 'success');
      } else {
        setMsg(t('k8s.env.saved'));
        addToast(t('k8s.env.toastSaved', { name: body.name }), 'success');
      }
      setDelArmed(false);
      await loadList();
      await reloadEnvs();
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
      addToast(t('k8s.env.opFail', { msg: ex.message }), 'error');
    }
  };

  const switchEnv = async () => {
    if (!form.name.trim()) { setMsg(t('k8s.env.nameRequired')); return; }
    const name = form.name.trim();
    try {
      await apiPost('/api/k8s/env/switch', { name });
      setDelArmed(false);
      await loadList();
      await reloadEnvs();
      setSelName(name);
      setMsg(t('k8s.env.switched'));
      addToast(t('k8s.env.toastSwitched', { name }), 'success');
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
      addToast(t('k8s.env.opFail', { msg: ex.message }), 'error');
    }
  };

  const del = async () => {
    if (!canEdit) return;
    // 二次确认：首次点击只武装，再点才真删
    if (!delArmed) { setDelArmed(true); return; }
    const name = form.name.trim();
    try {
      await apiPost('/api/k8s/env/delete', { name });
      setDelArmed(false);
      const envs = await loadList();
      await reloadEnvs();
      // 删除后落到明确状态：优先回填 current，其次第一项；列表空则进新建态
      setSelName('');
      const next = envs.find((x) => x.is_current) || envs[0];
      if (next) fill(next);
      else startNew();
      setMsg(t('k8s.env.deleted'));
      addToast(t('k8s.env.toastDeleted', { name }), 'success');
    } catch (ex: any) {
      setMsg(t('k8s.env.fail') + ex.message);
      addToast(t('k8s.env.opFail', { msg: ex.message }), 'error');
    }
  };

  /** 点「编辑」分段：回到选中项（从新建态切回时重锚到 current/第一项）。 */
  const switchToEdit = () => {
    const e = list.find((x) => x.name === selName) || list.find((x) => x.is_current) || list[0];
    if (e) fill(e);
    else startNew();
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal modal-wide"
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
          <div className="k8s-env-cols">
            {/* 左栏：环境列表（点选即回填右侧编辑） */}
            <div className="k8s-env-side">
              <div className="k8s-env-side-title">{t('k8s.env.list')} · {list.length}</div>
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
            </div>

            {/* 右栏：显式模式分段 + 表单。编辑态 ID 锁定，从交互上杜绝
                「改一下 ID，保存就从更新静默变成新增」。 */}
            <div className="k8s-env-main">
              <div className="k8s-env-seg" role="tablist" aria-label={t('k8s.env.modeLabel')}>
                <button
                  role="tab"
                  aria-selected={mode === 'edit'}
                  className={'seg' + (mode === 'edit' ? ' active' : '')}
                  onClick={switchToEdit}
                  disabled={list.length === 0}
                >
                  {t('k8s.env.modeEdit')}
                </button>
                <button
                  role="tab"
                  aria-selected={mode === 'new'}
                  className={'seg' + (mode === 'new' ? ' active' : '')}
                  onClick={startNew}
                >
                  ＋ {t('k8s.env.modeNew')}
                </button>
              </div>

              <div className="k8s-env-mode-note">
                {mode === 'edit'
                  ? (selName ? t('k8s.env.editingName', { name: envTagText(list, selName) }) : t('k8s.env.pickFirst'))
                  : t('k8s.env.creatingHint')}
              </div>

              <div className="k8s-env-form">
                <div className="form-row">
                  <label>{t('k8s.env.id')}</label>
                  <input
                    ref={idInputRef}
                    className="input"
                    value={form.name}
                    disabled={mode === 'edit'}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder={mode === 'new' ? t('k8s.env.idPh') : undefined}
                  />
                  {mode === 'edit' && <span className="k8s-env-lock" title={t('k8s.env.idLocked')}>🔒</span>}
                </div>
                {mode === 'edit'
                  ? <div className="k8s-env-id-hint">{t('k8s.env.idLocked')}</div>
                  : idTaken && <div className="k8s-env-id-hint err">{t('k8s.env.idExists')}</div>}
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
          </div>
        </div>
        <div className="modal-footer">
          <span className="k8s-env-msg">{msg}</span>
          <div className="spacer" />
          {/* 按钮层级随模式切换，主提交唯一 primary，行为 100% 可预期：
              编辑 → 保存修改（ID 不变，永远只是更新）；新建 → 创建环境（撞车禁用，永远只是创建）。 */}
          {mode === 'edit' ? (
            <>
              <button className="btn btn-sm btn-primary" onClick={save} disabled={!form.name.trim()}>
                {t('k8s.env.saveEdit')}
              </button>
              <button className="btn btn-sm btn-ghost" onClick={switchEnv} disabled={!form.name.trim()}>
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
            </>
          ) : (
            <>
              <button
                className="btn btn-sm btn-primary"
                onClick={save}
                disabled={!form.name.trim() || idTaken}
                title={idTaken ? t('k8s.env.idExists') : undefined}
              >
                {t('k8s.env.createEnv')}
              </button>
              <button className="btn btn-sm btn-ghost" onClick={startNew}>
                {t('k8s.env.resetForm')}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
