import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet, apiPost } from '../api/client';
import type { ConnectBody, ConnectResp, StatusResp } from '../api/types';

export function ConnectModal({ onClose }: { onClose: () => void }) {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setStatus = useAppStore((s) => s.setStatus);

  const [jiraUrl, setJiraUrl] = useState('');
  const [username, setUsername] = useState('');
  const [mode, setMode] = useState<'pat' | 'cookie'>('pat');
  const [pat, setPat] = useState('');
  const [cookie, setCookie] = useState('');
  const [repoId, setRepoId] = useState('');
  const [repoName, setRepoName] = useState('');
  const [branch, setBranch] = useState('');
  const [statusText, setStatusText] = useState('');
  const [statusColor, setStatusColor] = useState('');
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
          setStatusText('已从本地读取上次保存的 Cookie（如需更新请重新粘贴）');
        }
      })
      .catch(() => {});
  }, []);

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
    setStatusText('测试中…（PAT 模式会触发真实克隆，可能耗时）');
    setStatusColor('');
    try {
      const res = await apiPost<ConnectResp>('/api/connect', body());
      const parts: string[] = [];
      parts.push(res.cookieOk ? 'Cookie ✓' : 'Cookie ✗');
      if (res.patTest) parts.push(`PAT ${res.patTest.ok ? '✓' : '✗'}: ${res.patTest.msg}`);
      if (res.repoDefaults?.displayName) {
        setRepoName(res.repoDefaults.displayName);
        parts.push(`仓库名已探测: ${res.repoDefaults.displayName}`);
      }
      if (res.note) parts.push(res.note);
      if (mode === 'cookie' && cookie.trim()) {
        if (res.cookieSaved) parts.push('Cookie 已保存到本地，下次启动自动读取');
        else if (res.cookieWarning) {
          parts.push(res.cookieWarning);
          setStatusColor('var(--danger)');
        }
      }
      setStatusText(parts.join(' | '));
    } catch (e: any) {
      setStatusText(`错误：${e.message}`);
      setStatusColor('var(--danger)');
    } finally {
      setTesting(false);
    }
  }

  async function apply() {
    setSaving(true);
    try {
      await apiPost<ConnectResp>('/api/connect', body());
      addToast('连接配置已更新', 'success');
      apiGet<StatusResp>('/api/status').then(setStatus).catch(() => {});
      pushLog('连接配置已更新。');
      onClose();
    } catch (e: any) {
      addToast(e.message || '保存失败', 'error');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>连接设置</h3>
          <button className="btn btn-icon" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <label className="field">
            <span>Jira URL</span>
            <input value={jiraUrl} onChange={(e) => setJiraUrl(e.target.value)} />
          </label>
          <label className="field">
            <span>用户名</span>
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
              PAT 模式
            </label>
            <label className="radio">
              <input
                type="radio"
                name="mode"
                checked={mode === 'cookie'}
                onChange={() => setMode('cookie')}
              />
              Cookie 模式
            </label>
          </div>
          {mode === 'pat' ? (
            <label className="field">
              <span>PAT Token</span>
              <input
                type="password"
                value={pat}
                onChange={(e) => setPat(e.target.value)}
              />
            </label>
          ) : (
            <label className="field">
              <span>Cookie</span>
              <textarea value={cookie} onChange={(e) => setCookie(e.target.value)} />
            </label>
          )}
          <label className="field">
            <span>仓库 ID</span>
            <input value={repoId} onChange={(e) => setRepoId(e.target.value)} />
          </label>
          <label className="field">
            <span>仓库名</span>
            <input value={repoName} onChange={(e) => setRepoName(e.target.value)} />
          </label>
          <label className="field">
            <span>分支</span>
            <input value={branch} onChange={(e) => setBranch(e.target.value)} />
          </label>
          <div className="modal-status" style={{ color: statusColor }}>
            {statusText}
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost" onClick={test} disabled={testing}>
            {testing ? '测试中…' : '测试连接'}
          </button>
          <button className="btn btn-primary" onClick={apply} disabled={saving}>
            {saving ? '保存中…' : '确定'}
          </button>
        </div>
      </div>
    </div>
  );
}
