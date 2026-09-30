import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet, apiPost } from '../api/client';
import type { ConnectBody, ConnectResp, StatusResp } from '../api/types';
import { useT } from '../i18n';
import { useModalA11y } from '../utils/useModalA11y';

export function ConnectModal({ onClose }: { onClose: () => void }) {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setStatus = useAppStore((s) => s.setStatus);
  const { t } = useT();
  const dialogRef = useModalA11y<HTMLDivElement>(onClose);

  const [jiraUrl, setJiraUrl] = useState('');
  const [username, setUsername] = useState('');
  const [mode, setMode] = useState<'pat' | 'cookie'>('pat');
  const [pat, setPat] = useState('');
  const [cookie, setCookie] = useState('');
  const [repoId, setRepoId] = useState('');
  const [repoName, setRepoName] = useState('');
  const [branch, setBranch] = useState('');
  const [statusText, setStatusText] = useState('');
  const [statusType, setStatusType] = useState<'' | 'ok' | 'err'>('');
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    apiGet<StatusResp>('/api/status')
      .then((s) => {
        setJiraUrl(s.jira_url || '');
        setUsername(s.username || '');
        if (s.mode === 'cookie' || s.mode === 'pat') setMode(s.mode);
        setRepoId(s.repo_id || '');
        setBranch(s.branch || '');
        setRepoName(s.repo_name || '');
        if (s.cookie_set && s.cookie_source === 'session') {
          setStatusText(t('connect.readLocalCookie'));
        }
      })
      .catch(() => {});
  }, [t]);

  function body(): ConnectBody {
    return {
      jira_url: jiraUrl.trim(),
      username: username.trim(),
      mode,
      pat: pat.trim(),
      cookie: cookie.trim(),
      repo_id: repoId.trim(),
      repo_name: repoName.trim(),
      branch: branch.trim(),
    };
  }

  async function test() {
    setTesting(true);
    setStatusText(t('connect.testing'));
    setStatusType('');
    try {
      // dry_run：只探测连通性，不改写服务端配置、不落盘（此前「测试」与「应用」
      // 打同一请求，点测试就已经切换仓库并保存凭据）
      const res = await apiPost<ConnectResp>('/api/connect', { ...body(), dry_run: true });
      const parts: string[] = [];
      parts.push(res.cookieOk ? 'Cookie ✓' : 'Cookie ✗');
      if (res.patTest) parts.push(`PAT ${res.patTest.ok ? '✓' : '✗'}: ${res.patTest.msg}`);
      if (res.repoDefaults?.displayName) {
        setRepoName(res.repoDefaults.displayName);
        parts.push(`${t('connect.repoName')}: ${res.repoDefaults.displayName}`);
      }
      if (res.note) parts.push(res.note);
      let ok = res.cookieOk || Boolean(res.patTest?.ok) || Boolean(res.repoDefaults?.displayName);
      if (mode === 'cookie' && cookie.trim() && res.cookieWarning) {
        parts.push(res.cookieWarning);
        ok = false;
      }
      // 测试不保存：明确提示，避免用户以为已经生效
      parts.push(t('connect.testNotSaved'));
      setStatusType(ok ? 'ok' : 'err');
      setStatusText(parts.join(' | '));
    } catch (e: any) {
      setStatusText(`${t('common.error')}：${e.message}`);
      setStatusType('err');
    } finally {
      setTesting(false);
    }
  }

  async function apply() {
    setSaving(true);
    try {
      await apiPost<ConnectResp>('/api/connect', body());
      addToast(t('connect.configUpdated'), 'success');
      apiGet<StatusResp>('/api/status').then(setStatus).catch(() => {});
      pushLog(t('connect.updated'));
      onClose();
    } catch (e: any) {
      addToast(e.message || t('connect.saveFailed'), 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="connect-modal-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3 id="connect-modal-title">{t('connect.title')}</h3>
          <button className="btn btn-icon" onClick={onClose} aria-label={t('common.close')}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <label className="field">
            <span>{t('connect.jiraUrl')}</span>
            <input value={jiraUrl} onChange={(e) => setJiraUrl(e.target.value)} />
          </label>
          <label className="field">
            <span>{t('connect.username')}</span>
            <input value={username} onChange={(e) => setUsername(e.target.value)} />
          </label>
          <div className="field-row">
            <label className="radio">
              <input
                type="radio"
                name="mode"
                checked={mode === 'pat'}
                onChange={() => setMode('pat')}
              />
              {t('connect.patMode')}
            </label>
            <label className="radio">
              <input
                type="radio"
                name="mode"
                checked={mode === 'cookie'}
                onChange={() => setMode('cookie')}
              />
              {t('connect.cookieMode')}
            </label>
          </div>
          {mode === 'pat' ? (
            <label className="field">
              <span>{t('connect.patToken')}</span>
              <input
                type="password"
                value={pat}
                onChange={(e) => setPat(e.target.value)}
              />
            </label>
          ) : (
            <label className="field">
              <span>{t('connect.cookie')}</span>
              <textarea value={cookie} onChange={(e) => setCookie(e.target.value)} />
            </label>
          )}
          <label className="field">
            <span>{t('connect.repoId')}</span>
            <input value={repoId} onChange={(e) => setRepoId(e.target.value)} />
          </label>
          <label className="field">
            <span>{t('connect.repoName')}</span>
            <input value={repoName} onChange={(e) => setRepoName(e.target.value)} />
          </label>
          <label className="field">
            <span>{t('connect.branch')}</span>
            <input value={branch} onChange={(e) => setBranch(e.target.value)} />
          </label>
          <div className={'modal-status' + (statusType ? ' ' + statusType : '')}>
            {statusText}
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost" onClick={test} disabled={testing}>
            {testing ? t('common.testing') : t('connect.test')}
          </button>
          <button className="btn btn-primary" onClick={apply} disabled={saving}>
            {saving ? t('common.saving') : t('connect.apply')}
          </button>
        </div>
      </div>
    </div>
  );
}
