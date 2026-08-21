import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiPost } from '../api/client';
import { sse } from '../api/events';
import { useAppStore } from '../store/useAppStore';
import type {
  DiffEntry,
  DiffStatus,
  DiffSummary,
  DiffScanReq,
  DiffScanResp,
  DiffFileReq,
  DiffFileResp,
  DiffMergeResp,
  DiffMergeBatchResp,
} from '../api/types';

const DIFF_ICONS: Record<DiffStatus, string> = {
  modified: '✎',
  whitespace_only: '≈',
  local_only: '←',
  remote_only: '→',
  same: '=',
};
const DIFF_LABELS: Record<DiffStatus, string> = {
  modified: '已修改',
  whitespace_only: '仅行尾差异',
  local_only: '仅本地',
  remote_only: '仅远程',
  same: '相同',
};

export function DiffPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);
  const selectedRepo = useAppStore((s) => s.selectedRepo);

  const [localDir, setLocalDir] = useState('');
  const [ignoreLineEndings, setIgnoreLineEndings] = useState(true);
  const [showSame, setShowSame] = useState(false);
  const [mergeRemoteOnly, setMergeRemoteOnly] = useState(false);

  const [entries, setEntries] = useState<DiffEntry[]>([]);
  const [summary, setSummary] = useState<DiffSummary | null>(null);
  const [selectedPath, setSelectedPath] = useState('');
  const [fileTitle, setFileTitle] = useState('');
  const [fileHtml, setFileHtml] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const localDirRef = useRef(localDir);
  localDirRef.current = localDir;
  const scanningRef = useRef(false);

  // ===== SSE 接线（扫描 / 合并进度） =====
  useEffect(() => {
    const offs = [
      sse.on('scan_stage', (d: any) => {
        if (!scanningRef.current) return;
        setProgress({ visible: true, mode: 'indeterminate', stage: d.message || '扫描中…', detail: '' });
      }),
      sse.on('scan_progress', (d: any) => {
        if (!scanningRef.current) return;
        const pct = typeof d.pct === 'number' ? d.pct : 0;
        setProgress({ visible: true, mode: 'determinate', pct, stage: d.message || '扫描中…', detail: `${d.done ?? 0}/${d.total ?? 0}` });
      }),
      sse.on('scan_done', () => {
        if (scanningRef.current) setProgress({ visible: false });
      }),
      sse.on('scan_error', (d: any) => {
        if (!scanningRef.current) return;
        setProgress({ visible: false });
        addToast(d.message || '扫描出错', 'error');
        pushLog(`差异扫描出错：${d.message || ''}`, 'error');
      }),
      sse.on('merge_progress', (d: any) => {
        const pct = typeof d.pct === 'number' ? d.pct : 0;
        setProgress({ visible: true, mode: 'determinate', pct, stage: '批量合并中…', detail: d.error ? `${d.path}: ${d.error}` : `${d.done}/${d.total}` });
      }),
      sse.on('merge_done', () => {
        setProgress({ visible: false });
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [setProgress, addToast, pushLog]);

  const scanDiff = useCallback(async () => {
    const dir = localDir.trim();
    if (!dir) { pushLog('请输入本地目录路径', 'warning'); addToast('请输入本地目录路径', 'warn'); return; }
    if (!selectedRepo) { pushLog('请先选择远程仓库', 'warning'); addToast('请先选择远程仓库', 'warn'); return; }
    setBusy(true);
    scanningRef.current = true;
    setErrors([]);
    setProgress({ visible: true, mode: 'indeterminate', stage: '准备中…', detail: '' });
    setEntries([]);
    setSummary(null);
    setSelectedPath('');
    setFileHtml('');
    setFileTitle('');
    pushLog('开始扫描本地与远程差异…');
    try {
      const body: DiffScanReq = {
        local_dir: dir,
        repo_name: selectedRepo.display_name || '',
        ignore_line_endings: ignoreLineEndings,
      };
      const res = await apiPost<DiffScanResp>('/api/diff/scan', body);
      const s = res.summary || {};
      const wsBadge = s.whitespace_only
        ? ` · 行尾差异 ${s.whitespace_only}`
        : '';
      setSummary(s);
      setEntries(res.entries || []);
      setProgress({ visible: false });
      pushLog(`差异扫描完成：共 ${s.total ?? 0} 个文件，修改 ${s.modified ?? 0}，仅本地 ${s.local_only ?? 0}，仅远程 ${s.remote_only ?? 0}${wsBadge}`);
    } catch (ex: any) {
      setProgress({ visible: false });
      setErrors((e) => [...e, ex.message]);
      pushLog(`差异扫描失败：${ex.message}`, 'error');
      addToast(ex.message, 'error');
    } finally {
      scanningRef.current = false;
      setBusy(false);
    }
  }, [localDir, selectedRepo, ignoreLineEndings, setProgress, pushLog, addToast]);

  const visibleEntries = useMemo(() => {
    return entries.filter((e) => {
      if (e.status === 'same') return showSame;
      if (e.status === 'whitespace_only') return !ignoreLineEndings;
      return true;
    });
  }, [entries, showSame, ignoreLineEndings]);

  const openDiffFile = useCallback(async (path: string) => {
    setSelectedPath(path);
    setFileTitle(`加载中 · ${path}`);
    setFileHtml('<div class="empty-hint">加载中…</div>');
    try {
      const req: DiffFileReq = { local_dir: localDirRef.current, path };
      const res = await apiPost<DiffFileResp>('/api/diff/file', req);
      const entry = entries.find((e) => e.path === path);
      const status = (entry?.status || '') as DiffStatus;
      setFileTitle(`${path}  (${DIFF_LABELS[status] || status})`);
      setFileHtml(renderDiffContent(res, status));
    } catch (ex: any) {
      setFileTitle('错误');
      setFileHtml(esc(ex.message));
    }
  }, [entries]);

  const mergeOne = useCallback(async () => {
    if (!selectedPath) return;
    try {
      const res = await apiPost<DiffMergeResp>('/api/diff/merge', { local_dir: localDirRef.current, path: selectedPath });
      if (res.ok) {
        pushLog(`已合并到本地：${selectedPath}`);
        addToast(`已合并 ${selectedPath}`, 'success');
        setEntries((es) => es.filter((e) => e.path !== selectedPath));
        setFileHtml('<div class="empty-hint">已合并 ✓</div>');
        setFileTitle('已合并');
        setSelectedPath('');
      } else {
        pushLog(`合并失败：${selectedPath}`, 'error');
        addToast('合并失败', 'error');
      }
    } catch (ex: any) {
      pushLog(`合并失败：${ex.message}`, 'error');
      addToast(ex.message, 'error');
    }
  }, [selectedPath, pushLog, addToast]);

  const mergeAll = useCallback(async () => {
    let targets: DiffEntry[];
    if (mergeRemoteOnly) {
      targets = entries.filter((e) => e.status === 'remote_only');
    } else {
      targets = entries.filter((e) => {
        if (e.status === 'whitespace_only' && ignoreLineEndings) return false;
        return e.status === 'modified' || e.status === 'remote_only' || e.status === 'whitespace_only';
      });
    }
    if (!targets.length) {
      pushLog(mergeRemoteOnly ? '没有需要合并的云端差异项' : '没有需要合并的文件', 'warning');
      addToast(mergeRemoteOnly ? '没有需要合并的云端差异项' : '没有需要合并的文件', 'warn');
      return;
    }
    setBusy(true);
    const modeHint = mergeRemoteOnly ? '（仅云端差异项）' : '';
    pushLog(`开始批量合并 ${targets.length} 个文件${modeHint}…`);
    try {
      const query = mergeRemoteOnly ? '?status_filter=remote_only' : '';
      const reqs = targets.map((e) => ({ local_dir: localDirRef.current, path: e.path, status: e.status }));
      const res = await apiPost<DiffMergeBatchResp>(`/api/diff/merge-batch${query}`, reqs);
      const okPaths = new Set((res.results || []).filter((r) => r.ok).map((r) => r.path));
      const okCount = okPaths.size;
      const failCount = (res.results || []).length - okCount;
      pushLog(`批量合并完成${modeHint}：成功 ${okCount}，失败 ${failCount}`);
      addToast(`批量合并完成：成功 ${okCount}，失败 ${failCount}`, failCount ? 'warn' : 'success');
      setEntries((es) => es.filter((e) => !okPaths.has(e.path)));
      setFileTitle(`合并完成${modeHint}：成功 ${okCount}，失败 ${failCount}`);
      setFileHtml('');
    } catch (ex: any) {
      pushLog(`批量合并失败：${ex.message}`, 'error');
      addToast(ex.message, 'error');
    } finally {
      setBusy(false);
      setProgress({ visible: false });
    }
  }, [entries, mergeRemoteOnly, ignoreLineEndings, pushLog, addToast, setProgress]);

  return (
    <div className="diff-panel">
      <div className="diff-cfg-card">
        <div className="diff-cfg-row">
          <label>本地目录</label>
          <input
            className="input"
            placeholder="/path/to/local/repo"
            value={localDir}
            onChange={(e) => setLocalDir(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && scanDiff()}
          />
        </div>
        <div className="diff-cfg-row diff-cfg-inline">
          <label className="chk"><input type="checkbox" checked={ignoreLineEndings} onChange={(e) => setIgnoreLineEndings(e.target.checked)} /> 忽略行尾差异</label>
          <label className="chk"><input type="checkbox" checked={showSame} onChange={(e) => setShowSame(e.target.checked)} /> 显示相同</label>
          <label className="chk"><input type="checkbox" checked={mergeRemoteOnly} onChange={(e) => setMergeRemoteOnly(e.target.checked)} /> 仅合并云端差异</label>
          <button className="btn btn-sm" onClick={scanDiff} disabled={busy || !selectedRepo}>
            {busy ? '扫描中…' : '扫描差异'}
          </button>
        </div>
      </div>

      {!selectedRepo && <div className="empty-hint">请先在「仓库 / 文件」中选择远程仓库，再扫描差异。</div>}

      {summary && (
        <div className="diff-summary">
          {`共 ${summary.total ?? 0} 个文件 | `}
          <span className="badge-modified">修改 {summary.modified ?? 0}</span> ·{' '}
          <span className="badge-local">仅本地 {summary.local_only ?? 0}</span> ·{' '}
          <span className="badge-remote">仅远程 {summary.remote_only ?? 0}</span> · 相同 {summary.same ?? 0}
          {summary.whitespace_only ? <span className="badge-eol"> 行尾差异 {summary.whitespace_only}</span> : null}
        </div>
      )}

      {errors.length > 0 && (
        <div className="diff-error-box" style={{ display: 'block' }}>
          {errors.map((e, i) => <div key={i} className="err-line">{e}</div>)}
        </div>
      )}

      <div className="diff-body">
        <div className="diff-list-pane">
          {entries.length === 0 ? (
            <div className="empty-hint">输入本地目录并扫描，查看本地与远程差异。</div>
          ) : visibleEntries.length === 0 ? (
            <div className="empty-hint">
              {entries.every((e) => e.status === 'same')
                ? '无差异文件（所有文件相同；可勾选「显示相同」查看）'
                : entries.every((e) => e.status === 'same' || e.status === 'whitespace_only')
                  ? '无有效差异（剩余差异均为 CRLF/LF 行尾符；可取消「忽略行尾差异」查看）'
                  : '无差异（本地与远程完全一致）'}
            </div>
          ) : (
            visibleEntries.map((e) => (
              <div
                key={e.path}
                className={'diff-item' + (e.status === 'whitespace_only' ? ' diff-item-eol' : '') + (selectedPath === e.path ? ' selected' : '')}
                onClick={() => openDiffFile(e.path)}
              >
                <span className="diff-icon">{DIFF_ICONS[e.status] || '?'}</span>
                <span className="diff-path" title={e.path}>{e.path}{e.status === 'whitespace_only' ? ' ' : ''}{e.status === 'whitespace_only' ? <span className="diff-eol-badge">CRLF/LF</span> : null}</span>
              </div>
            ))
          )}
        </div>

        <div className="diff-content-pane">
          <div className="diff-file-head">
            <span className="diff-file-title">{fileTitle}</span>
            {selectedPath && (
              <button className="btn btn-sm" onClick={mergeOne} disabled={busy}>合并到本地</button>
            )}
            {entries.length > 0 && (
              <button className="btn btn-sm btn-ghost" onClick={mergeAll} disabled={busy}>
                全部合并到本地
              </button>
            )}
          </div>
          {fileHtml ? <div className="diff-content" dangerouslySetInnerHTML={{ __html: fileHtml }} /> : <div className="empty-hint">从左侧选择文件查看差异</div>}
        </div>
      </div>
    </div>
  );
}

// ===== Diff 内容渲染（端口自 04-diff.js） =====
function esc(s: unknown): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderDiffContent(res: DiffFileResp, status: DiffStatus): string {
  const diffText = res.diff || '';
  const local = res.local_content || '';
  const remote = res.remote_content || '';

  if (!diffText) {
    if (status === 'modified' || status === 'whitespace_only' || res.normalized_same) {
      const hint = `<div class="diff-info-hint">内容实际相同（可能仅行尾符 / 空白 / 格式差异）<br>本地大小 ${local.length} 字符，远程大小 ${remote.length} 字符</div>`;
      return hint + renderSideBySide(local, remote);
    }
    if (status === 'local_only') {
      return '<div class="empty-hint">仅本地存在，远程无此文件</div>' + renderPlain(local, 'local');
    }
    if (status === 'remote_only') {
      return '<div class="empty-hint">仅远程存在，本地无此文件（新增）</div>' + renderPlain(remote, 'remote');
    }
    return '<div class="empty-hint">内容完全相同</div>';
  }
  return renderUnifiedDiff(diffText);
}

function renderUnifiedDiff(diffText: string): string {
  const lines = diffText.split('\n');
  const rows: string[] = [];
  let oldNo = 0, newNo = 0;
  for (const line of lines) {
    if (!line) continue;
    let type = 'ctx';
    let oldCell = '', newCell = '', content = line;
    if (line.startsWith('@@')) {
      type = 'hunk';
      const m = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
      if (m) { oldNo = parseInt(m[1]) - 1; newNo = parseInt(m[2]) - 1; }
    } else if (line.startsWith('+++') || line.startsWith('---')) {
      type = 'header';
    } else if (line.startsWith('+')) {
      type = 'add'; newNo++; content = line.substring(1); newCell = String(newNo);
    } else if (line.startsWith('-')) {
      type = 'del'; oldNo++; content = line.substring(1); oldCell = String(oldNo);
    } else if (line.startsWith(' ')) {
      type = 'ctx'; oldNo++; newNo++; content = line.substring(1); oldCell = String(oldNo); newCell = String(newNo);
    }
    rows.push(
      `<tr class="diff-row diff-${type}">` +
      `<td class="diff-ln">${oldCell}</td>` +
      `<td class="diff-ln">${newCell}</td>` +
      `<td class="diff-sign">${type === 'add' ? '+' : type === 'del' ? '-' : ''}</td>` +
      `<td class="diff-code">${esc(content)}</td>` +
      `</tr>`
    );
  }
  return `<table class="diff-table">${rows.join('')}</table>`;
}

function renderSideBySide(local: string, remote: string): string {
  const localLines = local.split('\n');
  const remoteLines = remote.split('\n');
  const maxLines = Math.max(localLines.length, remoteLines.length);
  const rows: string[] = [];
  for (let i = 0; i < maxLines; i++) {
    const l = localLines[i] ?? '';
    const r = remoteLines[i] ?? '';
    const same = l === r;
    rows.push(
      `<tr class="diff-row ${same ? 'diff-ctx' : 'diff-changed'}">` +
      `<td class="diff-ln">${i + 1}</td>` +
      `<td class="diff-code">${esc(l)}</td>` +
      `<td class="diff-ln">${i + 1}</td>` +
      `<td class="diff-code">${esc(r)}</td>` +
      `</tr>`
    );
  }
  return `<div class="diff-sidebyside"><table class="diff-table diff-sidebyside-table">${rows.join('')}</table></div>`;
}

function renderPlain(content: string, side: 'local' | 'remote'): string {
  return `<pre class="diff-plain diff-plain-${side}">${esc(content)}</pre>`;
}
