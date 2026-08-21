import { useCallback, useState } from 'react';
import { api, apiGet } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import type { Commit, CommitFile, CommitsResp, FileAtCommitResp } from '../api/types';
import { esc, renderDiff, formatRelativeTime, authorColor, authorInitial } from '../utils/format';

const SIGN_MAP: Record<string, string> = {
  ADDED: '+', MODIFIED: 'M', DELETED: 'D', RENAMED: 'R', COPIED: 'C',
  A: '+', M: 'M', D: 'D', R: 'R', C: 'C',
};

export function CommitsPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const selectedRepo = useAppStore((s) => s.selectedRepo);

  const [mode, setMode] = useState<'remote' | 'local'>('remote');
  const [issueKey, setIssueKey] = useState('');
  const [commits, setCommits] = useState<Commit[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
  const [fileDiff, setFileDiff] = useState<{ path: string; html: string } | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);

  const queryCommits = useCallback(async () => {
    const localMode = mode === 'local';
    const key = issueKey.trim();
    if (localMode && !selectedRepo) {
      addToast('本地模式请先在「仓库 / 文件」中选中一个仓库', 'warn');
      return;
    }
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (key) params.set('issue_key', key);
      if (localMode) params.set('local_mode', 'true');
      const res = await apiGet<CommitsResp>(`/api/commits?${params.toString()}`);
      if (res.error) {
        setCommits([]);
        addToast(res.error, 'error');
        pushLog(`提交查询失败：${res.error}`, 'error');
      } else {
        setCommits(res.commits || []);
        setSelectedIdx(null);
        setFileDiff(null);
        const n = (res.commits || []).length;
        pushLog(`提交记录：共 ${n} 条`);
        if (n === 0) addToast('没有查询到提交记录', 'info');
      }
    } catch (e: any) {
      pushLog(`提交查询失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    } finally {
      setLoading(false);
    }
  }, [mode, issueKey, selectedRepo, addToast, pushLog]);

  const sel = selectedIdx != null ? commits[selectedIdx] : null;

  const openFileAtCommit = useCallback(async (commitId: string, path: string) => {
    setDiffLoading(true);
    setFileDiff({ path, html: `<div class="diff-loading">加载 ${esc(path)} @ ${esc(commitId.slice(0, 8))} 的 diff…</div>` });
    try {
      const [oldRes, newRes] = await Promise.all([
        api<FileAtCommitResp>(`/api/file-at-commit?commit_id=${encodeURIComponent(commitId)}&path=${encodeURIComponent(path)}`),
        api<FileAtCommitResp>(`/api/file?path=${encodeURIComponent(path)}`),
      ]);
      if (oldRes.error && newRes.error) {
        setFileDiff({ path, html: `<div class="diff-error">加载失败：旧版本 ${esc(oldRes.error)}；新版本 ${esc(newRes.error)}</div>` });
      } else if (oldRes.error) {
        setFileDiff({ path, html: `<div class="diff-title">${esc(path)}（新增文件 · 旧版本不存在）</div>` + renderDiff('', newRes.content || '') });
      } else if (newRes.error) {
        setFileDiff({ path, html: `<div class="diff-title">${esc(path)}（文件已删除）</div>` + renderDiff(oldRes.content || '', '') });
      } else {
        setFileDiff({ path, html: `<div class="diff-title">${esc(path)} @ ${esc(commitId.slice(0, 8))} → 当前</div>` + renderDiff(oldRes.content || '', newRes.content || '') });
      }
    } catch (ex: any) {
      setFileDiff({ path, html: `<div class="diff-error">加载失败：${esc(ex.message)}</div>` });
    } finally {
      setDiffLoading(false);
    }
  }, []);

  return (
    <div className="commits-panel">
      <div className="action-bar">
        <label className="inline-label">模式</label>
        <select
          className="sel"
          value={mode}
          onChange={(e) => setMode(e.target.value as 'remote' | 'local')}
        >
          <option value="remote">远程（Jira Issue）</option>
          <option value="local">本地（当前仓库）</option>
        </select>
        <input
          className="input"
          placeholder={mode === 'local' ? '（当前仓库）' : 'Issue 关键字，如 TST-234'}
          value={issueKey}
          onChange={(e) => setIssueKey(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && queryCommits()}
          style={{ flex: 1, minWidth: 160 }}
        />
        <button className="btn btn-sm" onClick={queryCommits} disabled={loading}>
          {loading ? '查询中…' : '查询'}
        </button>
      </div>

      <div className="commits-body">
        <div className="commits-list-pane">
          {commits.length === 0 ? (
            <div className="empty-hint">没有查询到提交记录</div>
          ) : (
            commits.map((c, i) => {
              const msg = (c.message || '').split('\n')[0] || '';
              const shortMsg = msg.length > 60 ? msg.slice(0, 59) + '…' : msg;
              let adds = 0, dels = 0;
              (c.files || []).forEach((f) => {
                adds += f.lines_added || 0;
                dels += f.lines_removed || 0;
              });
              const statsHtml = adds || dels ? (
                <span className="commit-stats"><b className="add">+{adds}</b><b className="del">-{dels}</b></span>
              ) : null;
              const relTime = formatRelativeTime(c.date);
              const shortHash = (c.commit_id || '').slice(0, 7);
              return (
                <div
                  key={c.commit_id + '-' + i}
                  className={'commit-item' + (selectedIdx === i ? ' selected' : '')}
                  onClick={() => setSelectedIdx(i)}
                >
                  <div className="commit-item-row">
                    <span className="commit-badge" style={{ background: authorColor(c.author) }}>
                      {authorInitial(c.author)}
                    </span>
                    <div className="commit-item-body">
                      <div className="commit-msg" title={esc(c.message || '')}>{esc(shortMsg)}</div>
                      <div className="commit-meta">
                        <span className="commit-author">{esc(c.author || '?')}</span>
                        <span className="commit-hash">{esc(shortHash)}</span>
                        <span className="commit-when" title={esc(c.date || '')}>{esc(relTime)}</span>
                        {c.repository_name ? <span className="commit-repo">{esc(c.repository_name)}</span> : null}
                        <span className="commit-files-count">📄 {(c.files || []).length}</span>
                      </div>
                    </div>
                    {statsHtml}
                  </div>
                </div>
              );
            })
          )}
        </div>

        <div className="commits-detail-pane">
          {!sel && <div className="empty-hint">单击左侧提交查看详情与变更文件</div>}
          {sel && (
            <>
              <pre className="commit-detail-text">{[
                `commit  ${sel.commit_id}`,
                `Author: ${sel.author}`,
                `Date:   ${sel.date}`,
                `Branch: ${sel.branch}${sel.repository_name ? `  (repo: ${sel.repository_name})` : ''}`,
                '',
                sel.message || '',
                '',
                `变更文件（${(sel.files || []).length}）：单击文件查看行级 diff`,
              ].join('\n')}</pre>
              <div className="commit-files">
                {(() => {
                  let totalAdd = 0, totalDel = 0;
                  (sel.files || []).forEach((f) => { totalAdd += f.lines_added || 0; totalDel += f.lines_removed || 0; });
                  return (
                    <div className="commit-file-summary">
                      <span>共 {(sel.files || []).length} 个文件变更</span>
                      <span className="commit-stats"><b className="add">+{totalAdd}</b><b className="del">-{totalDel}</b></span>
                    </div>
                  );
                })()}
                {(sel.files || []).map((f: CommitFile, fi: number) => {
                  const sign = SIGN_MAP[(f.change_type || '').toUpperCase()] || '?';
                  const stat = (f.lines_added || f.lines_removed) ? (
                    <span className="commit-stats"><b className="add">+{f.lines_added || 0}</b><b className="del">-{f.lines_removed || 0}</b></span>
                  ) : null;
                  return (
                    <div
                      key={f.path + '-' + fi}
                      className="commit-file-item"
                      onClick={() => openFileAtCommit(sel.commit_id, f.path)}
                    >
                      <span className={`change-badge change-${sign}`}>{sign}</span>
                      <span className="commit-file-path">{esc(f.path)}</span>
                      {stat}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {sel && (
            <div className="commit-diff" style={{ display: diffLoading || fileDiff ? 'block' : 'none' }}>
              {fileDiff ? <div dangerouslySetInnerHTML={{ __html: fileDiff.html }} /> : null}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
