import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiPost } from '../api/client';
import { sse } from '../api/events';
import { useAppStore } from '../store/useAppStore';
import { EtaTracker, formatEta } from '../utils/eta';
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
import { useT } from '../i18n';

const DIFF_ICONS: Record<DiffStatus, string> = {
  modified: '✎',
  whitespace_only: '≈',
  local_only: '←',
  remote_only: '→',
  same: '=',
};
const DIFF_LABELS: Record<DiffStatus, string> = {
  modified: 'modified',
  whitespace_only: 'whitespace',
  local_only: 'local only',
  remote_only: 'remote only',
  same: 'same',
};

export function DiffPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);
  const progress = useAppStore((s) => s.progress);
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const { t } = useT();

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
  // ETA 估算：扫描阶段与合并阶段各自独立计时（后端给的文件数 / 完成比例不同源）
  const scanEta = useRef(new EtaTracker());
  const scanEtaStarted = useRef(false);
  const mergeEta = useRef(new EtaTracker());
  const mergeEtaStarted = useRef(false);

  // ===== SSE 接线（扫描 / 合并进度） =====
  useEffect(() => {
    const offs = [
      sse.on('scan_stage', (d: any) => {
        if (!scanningRef.current) return;
        setProgress({ visible: true, mode: 'indeterminate', stage: d.message || t('diff.scanning'), detail: '' });
      }),
      sse.on('scan_progress', (d: any) => {
        if (!scanningRef.current) return;
        const pct = typeof d.pct === 'number' ? d.pct : 0;
        const done = typeof d.done === 'number' ? d.done : 0;
        // 后端 total 是「已发现目录数」而非文件数，done 才是真实文件数；
        // 远程扫描阶段 pct 落在 10~80，用 (pct-10)/70 当作该阶段完成比例反推总量。
        if (!scanEtaStarted.current) {
          scanEta.current.reset(done);
          scanEtaStarted.current = true;
        }
        const frac = (pct - 10) / 70;
        const etaSec = scanEta.current.etaFromFraction(done, frac);
        const eta = etaSec != null ? formatEta(etaSec) : '';
        setProgress({
          visible: true,
          mode: 'determinate',
          pct,
          stage: t('diff.scanRemote'),
          detail: t('diff.filesScanned', { n: done.toLocaleString() }),
          eta,
        });
      }),
      sse.on('scan_done', () => {
        scanEtaStarted.current = false;
        if (scanningRef.current) setProgress({ visible: false });
      }),
      sse.on('scan_error', (d: any) => {
        scanEtaStarted.current = false;
        if (!scanningRef.current) return;
        setProgress({ visible: false });
        addToast(d.message || t('diff.scanFail', { msg: '' }), 'error');
        pushLog(t('diff.scanFail', { msg: d.message || '' }), 'error');
      }),
      sse.on('merge_progress', (d: any) => {
        const pct = typeof d.pct === 'number' ? d.pct : 0;
        const done = typeof d.done === 'number' ? d.done : 0;
        const total = typeof d.total === 'number' ? d.total : 0;
        if (!mergeEtaStarted.current) {
          mergeEta.current.reset(done);
          mergeEtaStarted.current = true;
        }
        const etaSec = mergeEta.current.etaFromTotal(done, total);
        const eta = etaSec != null ? formatEta(etaSec) : '';
        setProgress({
          visible: true,
          mode: 'determinate',
          pct,
          stage: t('diff.merging'),
          detail: d.error
            ? `${d.path}: ${d.error}`
            : t('diff.mergingFiles', { done, total }),
          eta,
        });
      }),
      sse.on('merge_done', () => {
        mergeEtaStarted.current = false;
        setProgress({ visible: false });
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [setProgress, addToast, pushLog, t]);

  const scanDiff = useCallback(async () => {
    const dir = localDir.trim();
    if (!dir) { pushLog(t('diff.enterLocalDir'), 'warning'); addToast(t('diff.enterLocalDir'), 'warn'); return; }
    if (!selectedRepo) { pushLog(t('diff.selectRemoteFirst'), 'warning'); addToast(t('diff.selectRemoteFirst'), 'warn'); return; }
    setBusy(true);
    scanningRef.current = true;
    scanEtaStarted.current = false;
    setErrors([]);
    setProgress({ visible: true, mode: 'indeterminate', stage: t('diff.preparing'), detail: '' });
    setEntries([]);
    setSummary(null);
    setSelectedPath('');
    setFileHtml('');
    setFileTitle('');
    pushLog(t('diff.scanStart'));
    try {
      const body: DiffScanReq = {
        local_dir: dir,
        repo_name: selectedRepo.display_name || '',
        ignore_line_endings: ignoreLineEndings,
      };
      const res = await apiPost<DiffScanResp>('/api/diff/scan', body);
      const s = res.summary || {};
      const wsBadge = s.whitespace_only
        ? ` · ${t('diff.ignoreEol')} ${s.whitespace_only}`
        : '';
      setSummary(s);
      setEntries(res.entries || []);
      setProgress({ visible: false });
      pushLog(`${t('diff.scanComplete')}：${s.total ?? 0} · ${t('diff.merge')} ${s.modified ?? 0} · ${t('diff.local')} ${s.local_only ?? 0} · ${t('diff.remote')} ${s.remote_only ?? 0}${wsBadge}`);
    } catch (ex: any) {
      setProgress({ visible: false });
      setErrors((e) => [...e, ex.message]);
      pushLog(t('diff.scanFail', { msg: ex.message }), 'error');
      addToast(ex.message, 'error');
    } finally {
      scanningRef.current = false;
      setBusy(false);
    }
  }, [localDir, selectedRepo, ignoreLineEndings, setProgress, pushLog, addToast, t]);

  const visibleEntries = useMemo(() => {
    return entries.filter((e) => {
      if (e.status === 'same') return showSame;
      if (e.status === 'whitespace_only') return !ignoreLineEndings;
      return true;
    });
  }, [entries, showSame, ignoreLineEndings]);

  const openDiffFile = useCallback(async (path: string) => {
    setSelectedPath(path);
    setFileTitle(t('diff.fileTitleLoading') + path);
    setFileHtml(`<div class="empty-hint">${t('diff.loadingDiff')}</div>`);
    try {
      const req: DiffFileReq = { local_dir: localDirRef.current, path };
      const res = await apiPost<DiffFileResp>('/api/diff/file', req);
      const entry = entries.find((e) => e.path === path);
      const status = (entry?.status || '') as DiffStatus;
      setFileTitle(`${path}  (${DIFF_LABELS[status] || status})`);
      setFileHtml(renderDiffContent(res, status, t));
    } catch (ex: any) {
      setFileTitle(t('diff.error'));
      setFileHtml(esc(ex.message));
    }
  }, [entries, t]);

  const mergeOne = useCallback(async () => {
    if (!selectedPath) return;
    try {
      const res = await apiPost<DiffMergeResp>('/api/diff/merge', { local_dir: localDirRef.current, path: selectedPath });
      if (res.ok) {
        pushLog(t('diff.mergeOk', { path: selectedPath }));
        addToast(t('diff.mergeOk', { path: selectedPath }), 'success');
        setEntries((es) => es.filter((e) => e.path !== selectedPath));
        setFileHtml(`<div class="empty-hint">${t('diff.diffDone')}</div>`);
        setFileTitle(t('diff.merged'));
        setSelectedPath('');
      } else {
        pushLog(t('diff.mergeFailed', { path: selectedPath }), 'error');
        addToast(t('diff.mergeFailedShort'), 'error');
      }
    } catch (ex: any) {
      pushLog(t('diff.mergeFailed', { path: ex.message }), 'error');
      addToast(ex.message, 'error');
    }
  }, [selectedPath, pushLog, addToast, t]);

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
      const msg = mergeRemoteOnly ? t('diff.noMergeCloudTarget') : t('diff.noMergeTarget');
      pushLog(msg, 'warning');
      addToast(msg, 'warn');
      return;
    }
    setBusy(true);
    mergeEtaStarted.current = false;
    const modeHint = mergeRemoteOnly ? `（${t('diff.mergeRemoteOnly')}）` : '';
    pushLog(t('diff.batchMergeStart', { n: targets.length }) + modeHint);
    try {
      const query = mergeRemoteOnly ? '?status_filter=remote_only' : '';
      const reqs = targets.map((e) => ({ local_dir: localDirRef.current, path: e.path, status: e.status }));
      const res = await apiPost<DiffMergeBatchResp>(`/api/diff/merge-batch${query}`, reqs);
      const okPaths = new Set((res.results || []).filter((r) => r.ok).map((r) => r.path));
      const okCount = okPaths.size;
      const failCount = (res.results || []).length - okCount;
      pushLog(t('diff.batchMergeDone', { ok: okCount, fail: failCount }) + modeHint);
      addToast(t('diff.batchMergeDone', { ok: okCount, fail: failCount }), failCount ? 'warn' : 'success');
      setEntries((es) => es.filter((e) => !okPaths.has(e.path)));
      setFileTitle(t('diff.batchMergeDone', { ok: okCount, fail: failCount }) + modeHint);
      setFileHtml('');
    } catch (ex: any) {
      pushLog(t('diff.mergeFailed', { path: ex.message }), 'error');
      addToast(ex.message, 'error');
    } finally {
      setBusy(false);
      setProgress({ visible: false });
    }
  }, [entries, mergeRemoteOnly, ignoreLineEndings, pushLog, addToast, setProgress, t]);

  return (
    <div className="diff-panel tab-inner wide">
      <div className="diff-cfg-card">
        <div className="diff-cfg-row">
          <label className="field-inline">
            {t('diff.localDir')}
            <input
              className="input"
              placeholder="/path/to/local/repo"
              value={localDir}
              onChange={(e) => setLocalDir(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && scanDiff()}
              style={{ minWidth: 280, flex: 1 }}
            />
          </label>
          <button className="btn btn-primary" onClick={scanDiff} disabled={busy || !selectedRepo}>
            {busy ? t('diff.scanning') : t('diff.scan')}
          </button>
        </div>
        <div className="diff-cfg-row diff-cfg-inline">
          <label className="chk"><input type="checkbox" checked={ignoreLineEndings} onChange={(e) => setIgnoreLineEndings(e.target.checked)} /> {t('diff.ignoreEol')}</label>
          <label className="chk"><input type="checkbox" checked={showSame} onChange={(e) => setShowSame(e.target.checked)} /> {t('diff.showSame')}</label>
          <label className="chk"><input type="checkbox" checked={mergeRemoteOnly} onChange={(e) => setMergeRemoteOnly(e.target.checked)} /> {t('diff.mergeRemoteOnly')}</label>
        </div>
      </div>

      {/* 面板内进度条：直接订阅全局 progress store，复用 scan_progress / merge_progress SSE 事件。
          旧原生版的 #diff-progress 在 React 重构时漏搬，这里补回，让对比 / 合并进度在面板内可见。 */}
      {progress.visible && (
        <div className={`diff-progress ${progress.mode === 'indeterminate' ? 'indeterminate' : ''} ${progress.mode === 'error' ? 'error' : ''}`}>
          <div className="diff-progress-bar">
            <div
              className="diff-progress-fill"
              style={progress.mode === 'determinate' ? { width: `${Math.max(0, Math.min(100, progress.pct))}%` } : undefined}
            />
          </div>
          <div className="diff-progress-meta">
            <span className="diff-progress-stage">{progress.stage}</span>
            {progress.mode === 'determinate' && (
              <span className="diff-progress-pct">{Math.max(0, Math.min(100, progress.pct))}%</span>
            )}
          </div>
          {progress.detail && <div className="diff-progress-detail">{progress.detail}</div>}
          {progress.eta && <div className="diff-progress-eta">⏱ 预计剩余 {progress.eta}</div>}
        </div>
      )}

      {!selectedRepo && <div className="empty-hint">{t('diff.pickRepo')}</div>}

      {summary && (
        <div className="diff-summary">
          {`${summary.total ?? 0} · `}
          <span className="badge-modified">{t('diff.merge')} {summary.modified ?? 0}</span> ·{' '}
          <span className="badge-local">{t('diff.local')} {summary.local_only ?? 0}</span> ·{' '}
          <span className="badge-remote">{t('diff.remote')} {summary.remote_only ?? 0}</span> · {t('diff.noDiff')} {summary.same ?? 0}
          {summary.whitespace_only ? <span className="badge-eol"> {t('diff.ignoreEol')} {summary.whitespace_only}</span> : null}
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
            <div className="empty-hint">{t('diff.noDiffFiles')}</div>
          ) : visibleEntries.length === 0 ? (
            <div className="empty-hint">
              {entries.every((e) => e.status === 'same')
                ? t('diff.allSame')
                : entries.every((e) => e.status === 'same' || e.status === 'whitespace_only')
                  ? t('diff.noEffectiveDiff')
                  : t('diff.identical')}
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
              <button className="btn btn-sm btn-primary" onClick={mergeOne} disabled={busy}>{t('diff.mergeOne')}</button>
            )}
            {entries.length > 0 && (
              <button className="btn btn-sm btn-primary" onClick={mergeAll} disabled={busy}>
                {t('diff.mergeAll')}
              </button>
            )}
          </div>
          {fileHtml ? <div className="diff-content" dangerouslySetInnerHTML={{ __html: fileHtml }} /> : <div className="empty-hint">{t('diff.selectFileDiff')}</div>}
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

function renderDiffContent(res: DiffFileResp, status: DiffStatus, t: (k: string, v?: Record<string, string | number>) => string): string {
  const diffText = res.diff || '';
  const local = res.local_content || '';
  const remote = res.remote_content || '';

  if (!diffText) {
    if (status === 'modified' || status === 'whitespace_only' || res.normalized_same) {
      const hint = `<div class="diff-info-hint">${t('diff.contentSameHint')}<br>${t('diff.localSize')} ${local.length} ${t('diff.chars')}，${t('diff.remoteSize')} ${remote.length} ${t('diff.chars')}</div>`;
      return hint + renderSideBySide(local, remote);
    }
    if (status === 'local_only') {
      return `<div class="empty-hint">${t('diff.localOnly')}</div>` + renderPlain(local, 'local');
    }
    if (status === 'remote_only') {
      return `<div class="empty-hint">${t('diff.remoteOnly')}</div>` + renderPlain(remote, 'remote');
    }
    return `<div class="empty-hint">${t('diff.sameContent')}</div>`;
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
