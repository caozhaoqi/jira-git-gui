import { useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet, apiPost } from '../api/client';
import type { Repo, ReposResp, StatusResp } from '../api/types';
import { normalizeStatus } from '../api/types';

export function RepoList() {
  const repos = useAppStore((s) => s.repos);
  const setRepos = useAppStore((s) => s.setRepos);
  const selectRepo = useAppStore((s) => s.selectRepo);
  const setSelectedFile = useAppStore((s) => s.setSelectedFile);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const updateStatus = useAppStore((s) => s.setStatus);
  const [keyword, setKeyword] = useState('');
  const [busy, setBusy] = useState(false);

  async function discover(force: boolean) {
    setBusy(true);
    pushLog('【发现仓库】开始…');
    try {
      const res = await apiGet<ReposResp>(
        force ? '/api/repos?refresh=1' : '/api/repos'
      );
      if (res.error) {
        pushLog(`发现仓库错误：${res.error}`, 'warning');
        if (/cookie|登录|login|未配置/i.test(res.error)) {
          pushLog('Cookie 可能已过期，请重新打开「连接设置」获取新 Cookie。', 'error');
        }
      }
      setRepos(res.repos || []);
      pushLog(`【发现仓库】返回 ${(res.repos || []).length} 个${force ? '（强制刷新）' : ''}`);
    } catch (e: any) {
      pushLog(`发现仓库异常：${e.message}`, 'error');
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
        `已选择仓库 id=${r.repo_id} name=${r.display_name} branch=${branch || '(默认)'}`
      );
    } catch (e: any) {
      addToast(e.message, 'error');
    }
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
      <div className="pane-toolbar">
        <input
          className="search-input"
          placeholder="搜索仓库…"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <button className="btn btn-sm" onClick={() => discover(false)} disabled={busy}>
          {busy ? '发现中…' : '发现仓库'}
        </button>
        <button className="btn btn-sm btn-ghost" onClick={() => discover(true)} disabled={busy} title="绕过 10 分钟缓存，强制重新发现">
          🔄
        </button>
      </div>
      <div className="repo-list">
        {!matched.length && (
          <div className="empty-hint">
            {repos.length ? `无匹配项（关键字：${kw}）` : '未发现仓库，或该账号无权限'}
          </div>
        )}
        {matched.map((r) => (
          <div
            key={r.repo_id}
            className="repo-item"
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
    </div>
  );
}
