import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiPost, apiGet } from '../api/client';
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
  Repo,
  ReposResp,
  TreeEntry,
  Commit,
  MergeManifestResp,
  DiffCommitsResp,
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

  // ===== 对比仓库 / 目录 / 扫描参数 =====
  const [repos, setRepos] = useState<Repo[]>([]);
  const [compareRepo, setCompareRepo] = useState<string>(selectedRepo?.repo_id || '');
  // .env MERGE_REPO_*：远程仓库名 -> 本地目录（选中仓库时自动填本地目录）
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [localDir, setLocalDir] = useState('');
  const [compareDir, setCompareDir] = useState('');
  const [fastScan, setFastScan] = useState(true);
  const [ignoreLineEndings, setIgnoreLineEndings] = useState(true);
  const [showSame, setShowSame] = useState(false);
  const [mergeRemoteOnly, setMergeRemoteOnly] = useState(false);

  const [entries, setEntries] = useState<DiffEntry[]>([]);
  const [summary, setSummary] = useState<DiffSummary | null>(null);
  const [mergedCount, setMergedCount] = useState(0);
  const [selectedPath, setSelectedPath] = useState('');
  const [fileTitle, setFileTitle] = useState('');
  const [fileHtml, setFileHtml] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  // 最近更新记录（git log 风格）与已合并记录
  const [commits, setCommits] = useState<Commit[]>([]);
  const [commitsLoading, setCommitsLoading] = useState(false);
  const [subDirs, setSubDirs] = useState<TreeEntry[]>([]);
  const [showDirChooser, setShowDirChooser] = useState(false);

  const localDirRef = useRef(localDir);
  localDirRef.current = localDir;
  const compareDirRef = useRef(compareDir);
  compareDirRef.current = compareDir;
  const compareRepoRef = useRef(compareRepo);
  compareRepoRef.current = compareRepo;
  const scanningRef = useRef(false);
  // 扫描是并发递归的，速率比串行下载更抖，用更保守的平滑系数
  const scanEta = useRef(new EtaTracker({ alpha: 0.2, warmupMs: 2000 }));
  const scanEtaStarted = useRef(false);
  /**
   * 远端文件总量估计。
   *
   * 旧实现拿「目录进度比例」反推总量（totalEst = 已扫文件数 / frac），把
   * **文件数**和**目录进度**两个单位混着除——文件分布一不均匀（一个大目录
   * 装了全仓 90% 的文件）总量估值就会翻几倍，表现为「剩余时间越走越长」。
   *
   * 这里改用后端在 scan_stage 里给的 ``local_count``：本地与远端扫的是同一个
   * 仓库的同一子目录（compare_dir 同时收窄两侧），文件数高度接近，且**与
   * 进度里的 done（已扫文件数）同单位**。拿不到这个值就不显示 ETA。
   */
  const remoteFileTotalEst = useRef(0);
  const mergeEta = useRef(new EtaTracker());
  const mergeEtaStarted = useRef(false);

  // ===== 初始化：仓库列表 + .env 映射 =====
  const loadRepos = useCallback(async () => {
    try {
      const res = await apiGet<ReposResp>('/api/repos');
      const list = res.repos || [];
      setRepos(list);
      if (!compareRepoRef.current && selectedRepo?.repo_id) {
        setCompareRepo(selectedRepo.repo_id);
      }
    } catch (e: any) {
      pushLog(t('diff.repoLoadFail', { msg: e.message }), 'error');
    }
  }, [selectedRepo, pushLog, t]);

  const loadMappings = useCallback(async () => {
    try {
      const res = await apiGet<{ mappings?: { repo_name: string; local_dir: string }[] }>(
        '/api/diff/repo-mappings'
      );
      const m: Record<string, string> = {};
      (res.mappings || []).forEach((x) => { m[x.repo_name] = x.local_dir; });
      setMappings(m);
    } catch {
      /* 忽略：无 .env 映射时纯手动填目录 */
    }
  }, []);

  useEffect(() => {
    loadRepos();
    loadMappings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 选中对比仓库：通知后端 set_repo，并按 .env 映射自动填本地目录
  const selectCompareRepo = useCallback(async (repoId: string) => {
    setCompareRepo(repoId);
    const r = repos.find((x) => x.repo_id === repoId);
    if (r) {
      try {
        await apiPost('/api/repo/select', {
          repo_id: repoId,
          repo_name: r.display_name || r.name,
          branch: r.default_branch || '',
        });
      } catch (e: any) {
        addToast(e.message || t('diff.selectRepoFail'), 'error');
      }
      // 自动填本地目录：匹配 display_name 或 name
      const guess =
        mappings[r.display_name || ''] || mappings[r.name || ''] || '';
      if (guess) {
        setLocalDir(guess);
        pushLog(`${t('diff.envLocalDir')}：${guess}`, 'info');
      }
    }
  }, [repos, mappings, pushLog, addToast, t]);

  // ===== 子目录选择器（范围限定）=====
  const loadSubDirs = useCallback(async () => {
    const ld = localDirRef.current.trim();
    if (!ld) {
      addToast(t('diff.enterLocalDir'), 'warn');
      return;
    }
    try {
      const res = await apiGet<{ entries?: TreeEntry[] }>(
        `/api/tree?path=&local_dir=${encodeURIComponent(ld)}`
      );
      setSubDirs((res.entries || []).filter((e) => e.type === 'dir'));
      setShowDirChooser(true);
    } catch (e: any) {
      addToast(e.message, 'error');
    }
  }, [addToast, t]);

  const pickSubDir = useCallback((path: string) => {
    setCompareDir(path);
    setShowDirChooser(false);
  }, []);

  // ===== 最近更新记录（git log）=====
  const loadCommits = useCallback(async () => {
    if (!compareRepoRef.current) {
      addToast(t('diff.selectRemoteFirst'), 'warn');
      return;
    }
    setCommitsLoading(true);
    try {
      const cd = compareDirRef.current.trim();
      const url = `/api/diff/commits?limit=30${cd ? `&path=${encodeURIComponent(cd)}` : ''}`;
      const res = await apiGet<DiffCommitsResp>(url);
      if (res.error) {
        addToast(res.error, 'error');
        setCommits([]);
      } else {
        setCommits(res.commits || []);
      }
    } catch (e: any) {
      addToast(e.message, 'error');
    } finally {
      setCommitsLoading(false);
    }
  }, [addToast, t]);

  // ===== 已合并记录（merge_manifest）=====
  const loadManifest = useCallback(async () => {
    const ld = localDirRef.current.trim();
    if (!ld) return;
    const cd = compareDirRef.current.trim();
    try {
      const url = `/api/diff/merge-manifest?local_dir=${encodeURIComponent(ld)}${
        cd ? `&compare_dir=${encodeURIComponent(cd)}` : ''
      }`;
      const res = await apiGet<MergeManifestResp>(url);
      setMergedCount(res.count || 0);
    } catch {
      /* 忽略 */
    }
  }, []);

  // ===== SSE 接线（扫描 / 合并进度） =====
  useEffect(() => {
    const offs = [
      sse.on('scan_stage', (d: any) => {
        if (!scanningRef.current) return;
        // 远端扫描开始前记下本地文件数，作为远端文件总量的实测估计
        if (d.stage === 'remote' && typeof d.local_count === 'number') {
          remoteFileTotalEst.current = d.local_count;
        }
        setProgress({ visible: true, mode: 'indeterminate', stage: d.message || t('diff.scanning'), detail: '' });
      }),
      sse.on('scan_progress', (d: any) => {
        if (!scanningRef.current) return;
        const pct = typeof d.pct === 'number' ? d.pct : 0;
        const done = typeof d.done === 'number' ? d.done : 0;
        if (!scanEtaStarted.current) {
          scanEta.current.reset(done);
          scanEtaStarted.current = true;
        }
        // 注意：pct 只用于进度条，不再拿来反推总量（那是旧版 ETA 失真的根因）。
        // 总量用本地实测文件数；若已扫文件数反超估计值，说明估计已失真，
        // 宁可不显示也不给个离谱数字。
        const totalEst = remoteFileTotalEst.current;
        const etaSec =
          totalEst > done ? scanEta.current.etaFromTotal(done, totalEst) : null;
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
    if (!compareRepo) { pushLog(t('diff.selectRemoteFirst'), 'warning'); addToast(t('diff.selectRemoteFirst'), 'warn'); return; }
    setBusy(true);
    scanningRef.current = true;
    scanEtaStarted.current = false;
    remoteFileTotalEst.current = 0;
    setErrors([]);
    setProgress({ visible: true, mode: 'indeterminate', stage: t('diff.preparing'), detail: '' });
    setEntries([]);
    setSummary(null);
    setSelectedPath('');
    setFileHtml('');
    setFileTitle('');
    setCommits([]);
    pushLog(t('diff.scanStart'));
    try {
      const r = repos.find((x) => x.repo_id === compareRepo);
      const body: DiffScanReq = {
        local_dir: dir,
        repo_id: compareRepo,
        repo_name: r?.display_name || r?.name || '',
        branch: r?.default_branch || '',
        compare_dir: compareDir.trim(),
        ignore_line_endings: ignoreLineEndings,
        fast_scan: fastScan,
      };
      const res = await apiPost<DiffScanResp>('/api/diff/scan', body);
      const s = res.summary || {};
      const wsBadge = s.whitespace_only
        ? ` · ${t('diff.ignoreEol')} ${s.whitespace_only}`
        : '';
      setSummary(s);
      setEntries(res.entries || []);
      setMergedCount(res.merged_count || 0);
      setProgress({ visible: false });
      pushLog(`${t('diff.scanComplete')}：${s.total ?? 0} · ${t('diff.merge')} ${s.modified ?? 0} · ${t('diff.local')} ${s.local_only ?? 0} · ${t('diff.remote')} ${s.remote_only ?? 0}${wsBadge}`);
      // 顺带拉取最近更新记录与已合并记录
      loadCommits();
      loadManifest();
    } catch (ex: any) {
      setProgress({ visible: false });
      setErrors((e) => [...e, ex.message]);
      pushLog(t('diff.scanFail', { msg: ex.message }), 'error');
      addToast(ex.message, 'error');
    } finally {
      scanningRef.current = false;
      setBusy(false);
    }
  }, [localDir, compareRepo, compareDir, fastScan, ignoreLineEndings, repos, loadCommits, loadManifest, setProgress, pushLog, addToast, t]);

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
      const req: DiffFileReq = {
        local_dir: localDirRef.current,
        path,
        compare_dir: compareDirRef.current.trim(),
      };
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
      const res = await apiPost<DiffMergeResp>('/api/diff/merge', {
        local_dir: localDirRef.current,
        path: selectedPath,
        compare_dir: compareDirRef.current.trim(),
      });
      if (res.ok) {
        if (res.skipped) {
          pushLog(t('diff.mergeSkipped', { path: selectedPath }), 'info');
          addToast(t('diff.mergeSkipped', { path: selectedPath }), 'success');
        } else {
          pushLog(t('diff.mergeOk', { path: selectedPath }));
          addToast(t('diff.mergeOk', { path: selectedPath }), 'success');
        }
        setEntries((es) => es.filter((e) => e.path !== selectedPath));
        setFileHtml(`<div class="empty-hint">${t('diff.diffDone')}</div>`);
        setFileTitle(t('diff.merged'));
        setSelectedPath('');
        loadManifest();
      } else {
        pushLog(t('diff.mergeFailed', { path: selectedPath }), 'error');
        addToast(res.error || t('diff.mergeFailedShort'), 'error');
      }
    } catch (ex: any) {
      pushLog(t('diff.mergeFailed', { path: ex.message }), 'error');
      addToast(ex.message, 'error');
    }
  }, [selectedPath, pushLog, addToast, t, loadManifest]);

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
      const reqs = targets.map((e) => ({
        local_dir: localDirRef.current,
        path: e.path,
        compare_dir: compareDirRef.current.trim(),
        status: e.status,
      }));
      const res = await apiPost<DiffMergeBatchResp>(`/api/diff/merge-batch${query}`, reqs);
      const okPaths = new Set((res.results || []).filter((r) => r.ok).map((r) => r.path));
      const okCount = okPaths.size;
      const failCount = (res.results || []).length - okCount;
      pushLog(t('diff.batchMergeDone', { ok: okCount, fail: failCount }) + modeHint);
      addToast(t('diff.batchMergeDone', { ok: okCount, fail: failCount }), failCount ? 'warn' : 'success');
      setEntries((es) => es.filter((e) => !okPaths.has(e.path)));
      setFileTitle(t('diff.batchMergeDone', { ok: okCount, fail: failCount }) + modeHint);
      setFileHtml('');
      loadManifest();
    } catch (ex: any) {
      pushLog(t('diff.mergeFailed', { path: ex.message }), 'error');
      addToast(ex.message, 'error');
    } finally {
      setBusy(false);
      setProgress({ visible: false });
    }
  }, [entries, mergeRemoteOnly, ignoreLineEndings, pushLog, addToast, setProgress, t, loadManifest]);

  return (
    <div className="diff-panel tab-inner wide">
      <div className="diff-cfg-card">
        <div className="diff-cfg-row">
          <label className="field-inline">
            {t('diff.compareRepo')}
            <select
              className="sel"
              value={compareRepo}
              onChange={(e) => selectCompareRepo(e.target.value)}
              style={{ minWidth: 200 }}
            >
              <option value="">{t('diff.pickRepo')}</option>
              {repos.map((r) => {
                // 同名仓库场景下，仅靠名称无法区分，必须同时显示仓库 ID
                const label = r.display_name || r.name;
                const text = label ? `${label}  ·  ID ${r.repo_id}` : r.repo_id;
                return (
                  <option key={r.repo_id} value={r.repo_id}>
                    {text}
                  </option>
                );
              })}
            </select>
          </label>
          <label className="field-inline">
            {t('diff.localDir')}
            <input
              className="input"
              placeholder="/path/to/local/repo"
              value={localDir}
              onChange={(e) => setLocalDir(e.target.value)}
              style={{ minWidth: 240, flex: 1 }}
            />
          </label>
          <button className="btn btn-primary" onClick={scanDiff} disabled={busy || !compareRepo}>
            {busy ? t('diff.scanning') : t('diff.scan')}
          </button>
        </div>

        <div className="diff-cfg-row diff-cfg-inline">
          <label className="field-inline">
            {t('diff.compareDir')}
            <input
              className="input"
              placeholder={t('diff.compareDirPlaceholder')}
              value={compareDir}
              onChange={(e) => setCompareDir(e.target.value)}
              style={{ minWidth: 160 }}
            />
            <button className="btn btn-sm btn-ghost" type="button" onClick={loadSubDirs}>
              {t('diff.browseDir')}
            </button>
          </label>
          {showDirChooser && (
            <div className="diff-dir-chooser">
              <div
                className="diff-dir-item"
                onClick={() => pickSubDir('')}
              >
                {t('diff.wholeRepo')}
              </div>
              {subDirs.map((d) => (
                <div
                  key={d.path}
                  className="diff-dir-item"
                  onClick={() => pickSubDir(d.path)}
                >
                  📁 {d.path}
                </div>
              ))}
              {subDirs.length === 0 && (
                <div className="diff-dir-empty">{t('diff.noSubDirs')}</div>
              )}
            </div>
          )}
        </div>

        <div className="diff-cfg-row diff-cfg-inline">
          <label className="chk">
            <input type="checkbox" checked={fastScan} onChange={(e) => setFastScan(e.target.checked)} />
            {t('diff.fastScan')}
          </label>
          <label className="chk"><input type="checkbox" checked={ignoreLineEndings} onChange={(e) => setIgnoreLineEndings(e.target.checked)} /> {t('diff.ignoreEol')}</label>
          <label className="chk"><input type="checkbox" checked={showSame} onChange={(e) => setShowSame(e.target.checked)} /> {t('diff.showSame')}</label>
          <label className="chk"><input type="checkbox" checked={mergeRemoteOnly} onChange={(e) => setMergeRemoteOnly(e.target.checked)} /> {t('diff.mergeRemoteOnly')}</label>
          <button className="btn btn-sm btn-ghost" type="button" onClick={loadCommits} disabled={commitsLoading || !compareRepo}>
            {commitsLoading ? t('common.loading') : t('diff.recentUpdates')}
          </button>
        </div>
      </div>

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

      {!compareRepo && <div className="empty-hint">{t('diff.pickRepo')}</div>}

      {summary && (
        <div className="diff-summary">
          {`${summary.total ?? 0} · `}
          <span className="badge-modified">{t('diff.merge')} {summary.modified ?? 0}</span> ·{' '}
          <span className="badge-local">{t('diff.local')} {summary.local_only ?? 0}</span> ·{' '}
          <span className="badge-remote">{t('diff.remote')} {summary.remote_only ?? 0}</span> · {t('diff.noDiff')} {summary.same ?? 0}
          {summary.whitespace_only ? <span className="badge-eol"> {t('diff.ignoreEol')} {summary.whitespace_only}</span> : null}
          {mergedCount > 0 ? <span className="badge-merged"> · {t('diff.mergedBadge', { n: mergedCount })}</span> : null}
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
                {e.merged ? <span className="diff-merged-badge" title={t('diff.mergedTip')}>✓</span> : null}
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

      {/* 最近更新记录（git log 风格） */}
      <div className="diff-commits">
        <div className="panel-header">
          <h3 className="section-title">{t('diff.recentUpdates')}{compareDir ? ` · ${compareDir}` : ''}</h3>
          <button className="btn btn-sm btn-ghost" onClick={loadCommits} disabled={commitsLoading || !compareRepo}>
            {commitsLoading ? t('common.loading') : t('diff.refresh')}
          </button>
        </div>
        {commits.length === 0 ? (
          <div className="empty-hint">{t('diff.noCommits')}</div>
        ) : (
          <div className="diff-commits-list">
            {commits.map((c) => (
              <div key={c.commit_id} className="commit-item">
                <div className="commit-head">
                  <span className="commit-msg">{c.message}</span>
                  <span className="commit-meta">{c.author} · {c.date}</span>
                </div>
                {c.files && c.files.length > 0 && (
                  <div className="commit-files">
                    {c.files.slice(0, 12).map((f, i) => (
                      <span key={i} className={`commit-file ct-${f.change_type || ''}`}>
                        {(f.change_type || '?')} {f.path}
                      </span>
                    ))}
                    {c.files.length > 12 && <span className="commit-file-more">+{c.files.length - 12}</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
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
