import { useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet, apiPost } from '../api/client';
import type { Repo, ReposResp, StatusResp } from '../api/types';
import { normalizeStatus } from '../api/types';
import { useT } from '../i18n';

export function RepoList() {
  const repos = useAppStore((s) => s.repos);
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const setRepos = useAppStore((s) => s.setRepos);
  const selectRepo = useAppStore((s) => s.selectRepo);
  const setSelectedFile = useAppStore((s) => s.setSelectedFile);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const updateStatus = useAppStore((s) => s.setStatus);
  const { t } = useT();
  const [keyword, setKeyword] = useState('');
  const [busy, setBusy] = useState(false);
  const [manualId, setManualId] = useState('');
  const [manualName, setManualName] = useState('');
  const [manualBranch, setManualBranch] = useState('');

  async function discover(force: boolean) {
    setBusy(true);
    pushLog(t('repo.selectRepo'));
    try {
      const res = await apiGet<ReposResp>(
        force ? '/api/repos?refresh=1' : '/api/repos'
      );
      if (res.error) {
        pushLog(`${t('repo.title')}：${res.error}`, 'warning');
        if (/cookie|登录|login|未配置/i.test(res.error)) {
          pushLog(t('repo.noPreview'), 'error');
        }
      }
      setRepos(res.repos || []);
      pushLog(`${t('repo.title')}：${(res.repos || []).length} 个${force ? '（强制刷新）' : ''}`);
    } catch (e: any) {
      pushLog(`${t('repo.title')}：${e.message}`, 'error');
      addToast(e.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function openRepo(r: Repo) {
    selectRepo(r);
    setSelectedFile(null);
    const branch = r.default_branch || '';
    try {
      await apiPost('/api/repo/select', {
        repo_id: r.repo_id,
        repo_name: r.display_name,
        branch,
      });
      apiGet<StatusResp>('/api/status')
        .then((s) => updateStatus(normalizeStatus(s)))
        .catch(() => {});
      pushLog(
        `id=${r.repo_id} name=${r.display_name} branch=${branch || '(默认)'}`
      );
    } catch (e: any) {
      addToast(e.message, 'error');
    }
  }

  async function loadManual() {
    if (!manualId.trim()) {
      addToast(t('repo.noFileSelected'), 'warn');
      return;
    }
    const r: Repo = {
      repo_id: manualId.trim(),
      display_name: manualName.trim() || manualId.trim(),
      default_branch: manualBranch.trim(),
    };
    await openRepo(r);
  }

  const kw = keyword.trim().toLowerCase();
  const matched = kw
    ? repos.filter(
        (r) =>
          (r.display_name || '').toLowerCase().includes(kw) ||
          String(r.repo_id).toLowerCase().includes(kw) ||
          (r.default_branch || '').toLowerCase().includes(kw)
      )
    : repos;

  return (
    <div className="repo-list-pane">
      <div className="panel-header">
        <h2 className="section-title">{t('repo.title')}</h2>
      </div>
      <div className="repo-toolbar">
        <input
          className="input repo-search-input"
          placeholder={`🔍 ${t('repo.selectRepo')}…`}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <button className="btn btn-primary" onClick={() => discover(false)} disabled={busy}>
          {busy ? t('common.loading') : `🔄 ${t('repo.discover')}`}
        </button>
      </div>
      <div className="repo-list">
        {!matched.length && (
          <div className="empty-hint">
            {repos.length ? `(${kw})` : t('repo.selectRepo')}
          </div>
        )}
        {matched.map((r) => (
          <div
            key={r.repo_id}
            className={`repo-item ${selectedRepo?.repo_id === r.repo_id ? 'selected' : ''}`}
            onClick={() => openRepo(r)}
          >
            <div className="repo-name">{r.display_name || r.repo_id}</div>
            <div className="repo-meta">
              id={r.repo_id}
              {r.default_branch ? ` [branch=${r.default_branch}]` : ''}
            </div>
          </div>
        ))}
      </div>
      <details className="manual-group">
        <summary>{t('repo.checkout')}</summary>
        <div className="form-rows">
          <label>
            {t('connect.repoId')}
            <input className="input" value={manualId} onChange={(e) => setManualId(e.target.value)} />
          </label>
          <label>
            {t('connect.repoName')}
            <input
              className="input"
              placeholder={t('repo.clone')}
              value={manualName}
              onChange={(e) => setManualName(e.target.value)}
            />
          </label>
          <label>
            {t('connect.branch')}
            <input
              className="input"
              placeholder="(默认)"
              value={manualBranch}
              onChange={(e) => setManualBranch(e.target.value)}
            />
          </label>
          <button className="btn btn-primary btn-block" onClick={loadManual}>
            {t('repo.fileTree')}
          </button>
        </div>
      </details>
    </div>
  );
}
