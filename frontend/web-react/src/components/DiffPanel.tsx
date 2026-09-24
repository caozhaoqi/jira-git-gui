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
  DiffConflict,
  MergeResolveReq,
  MergeResolveResp,
  FileResp,
  FileAtCommitResp,
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
  // F1：把差异扫描结果叠加到文件树（setDiffOverlay）。F13：合并勾选项用 checkedPaths。
  const setDiffOverlay = useAppStore((s) => s.setDiffOverlay);
  const checkedPaths = useAppStore((s) => s.checkedPaths);
  const setProgress = useAppStore((s) => s.setProgress);
  const progress = useAppStore((s) => s.progress);
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const activeTab = useAppStore((s) => s.activeTab);
  const storeRepos = useAppStore((s) => s.repos);
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
  // F2：历史版本预览（commit 里某文件的历史内容 + 与本地对比）
  const [history, setHistory] = useState<{
    commitId: string;
    commitMsg: string;
    path: string;
    changeType: string;
    content: string;
    localContent: string;
    err?: string;
  } | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  // F4：合并冲突（3-way）材料 + 决策态
  const [conflicts, setConflicts] = useState<DiffConflict[]>([]);
  const [conflictResolutions, setConflictResolutions] = useState<Record<string, string>>({});
  const [conflictMerged, setConflictMerged] = useState<Record<string, string>>({});
  const [resolving, setResolving] = useState(false);
  const [busy, setBusy] = useState(false);

  // 区块折叠态（配置 / 汇总 / 最近更新）
  const [cfgOpen, setCfgOpen] = useState(true);
  const [summaryOpen, setSummaryOpen] = useState(true);
  const [commitsOpen, setCommitsOpen] = useState(true);

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

  // 自愈 1：DiffPanel 一经访问就常驻挂载（App.tsx visited 策略），若首次挂载时
  // Cookie 已过期，/api/repos 返回 0 个且本地列表永远不会自动补。「仓库 / 文件」
  // 页刷新过仓库（store.repos 有数据）而本地为空时，直接同步进来。
  useEffect(() => {
    if (!repos.length && storeRepos.length) {
      setRepos(storeRepos);
    }
  }, [storeRepos, repos.length]);

  // 自愈 2：每次切回 diff 页都刷新仓库列表（后端有 600s 缓存，代价低），
  // 同时 loadRepos 内部会把 selectedRepo 回填到 compareRepo（若为空）——
  // 修「在仓库页选了仓库、切到对比页却不生效」的脱节。
  useEffect(() => {
    if (activeTab === 'diff') {
      loadRepos();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

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
      setDiffOverlay({}); // F1：扫描开始先清掉上一轮的树差异色点
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
      // F1：把差异状态叠加到文件树（path -> status），same 不进叠加层以免刷屏。
      const overlay: Record<string, DiffStatus> = {};
      for (const e of res.entries || []) {
        if (e.status !== 'same') overlay[e.path] = e.status;
      }
      setDiffOverlay(overlay);
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

  // F2：查看某次提交里某文件的历史版本，并与本地当前内容左右对比。
  const openHistoryFile = useCallback(async (
    commitId: string, commitMsg: string, path: string, changeType: string,
  ) => {
    setHistoryLoading(true);
    setHistory({ commitId, commitMsg, path, changeType, content: '', localContent: '' });
    try {
      const hist = await apiGet<FileAtCommitResp>(
        `/api/file-at-commit?${new URLSearchParams({ commit_id: commitId, path }).toString()}`,
      );
      if (hist.error) {
        setHistory({ commitId, commitMsg, path, changeType, content: '', localContent: '', err: hist.error });
        return;
      }
      // 本地当前内容（仅当设置了本地目录；缺失不影响历史预览，用于左右对比）。
      let localContent = '';
      const localDir = localDirRef.current;
      if (localDir) {
        try {
          const loc = await apiGet<FileResp>(
            `/api/file?${new URLSearchParams({ path, local_dir: localDir }).toString()}`,
          );
          if (!loc.error && typeof loc.content === 'string') localContent = loc.content;
        } catch {
          /* 本地文件缺失属正常（如该文件后来被删除） */
        }
      }
      setHistory({
        commitId, commitMsg, path, changeType,
        content: hist.content || '', localContent, err: undefined,
      });
    } catch (ex: any) {
      setHistory({ commitId, commitMsg, path, changeType, content: '', localContent: '', err: ex.message });
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const backToDiff = useCallback(() => {
    setHistory(null);
  }, []);

  // diffOverlay 的 setter 只接受值（非 updater），故按需整体重写。
  const dropOverlay = useCallback((paths: string[]) => {
    const st = useAppStore.getState();
    const next: Record<string, DiffStatus> = { ...st.diffOverlay };
    for (const p of paths) delete next[p];
    st.setDiffOverlay(next);
  }, []);

  // F4：简易 3-way 合并（行级）。base 缺失时直接以 theirs 兜底；
  // 仅一侧相对 base 改动 → 取那侧；两侧都改 → 冲突标记块，供用户在文本框内手工解决。
  const threeWayMerge = useCallback((base: string | null | undefined, ours: string, theirs: string): string => {
    if (base == null) return theirs;
    if (ours === theirs) return ours;
    if (ours === base) return theirs;       // 仅远端改
    if (theirs === base) return ours;       // 仅本地改
    const b = base.split('\n'); const o = ours.split('\n'); const t = theirs.split('\n');
    const max = Math.max(b.length, o.length, t.length);
    const out: string[] = [];
    for (let i = 0; i < max; i++) {
      const bl = b[i] ?? '', ol = o[i] ?? '', tl = t[i] ?? '';
      if (ol === tl) { out.push(ol); }
      else if (ol === bl) { out.push(tl); }
      else if (tl === bl) { out.push(ol); }
      else { out.push('<<<<<<< LOCAL', ol, '=======', tl, '>>>>>>> REMOTE'); }
    }
    return out.join('\n');
  }, []);

  // F4：把后端返回的冲突材料收集进 conflicts 状态并打开冲突面板。
  const openConflicts = useCallback((list: DiffConflict[]) => {
    setConflicts(list);
    const initRes: Record<string, string> = {};
    const initMerged: Record<string, string> = {};
    for (const c of list) {
      // 二进制冲突无法经 JSON 传字节，仅支持「保留本地」
      initRes[c.path] = c.is_binary ? 'ours' : 'merged';
      initMerged[c.path] = threeWayMerge(c.base, c.ours, c.theirs ?? '');
    }
    setConflictResolutions(initRes);
    setConflictMerged(initMerged);
  }, [threeWayMerge]);

  // F4：提交冲突决策（ours/theirs/merged）到后端，成功后刷新扫描。
  const resolveConflicts = useCallback(async () => {
    if (!conflicts.length) return;
    setResolving(true);
    try {
      const reqs: MergeResolveReq[] = conflicts.map((c) => ({
        local_dir: localDirRef.current,
        path: c.path,
        compare_dir: compareDirRef.current.trim(),
        resolution: conflictResolutions[c.path] || 'merged',
        merged_content: conflictMerged[c.path] ?? '',
        theirs_content: c.theirs ?? '',
      }));
      const res = await apiPost<MergeResolveResp>('/api/diff/merge-resolve', reqs);
      const okCount = (res.results || []).filter((r) => r.ok).length;
      const failCount = (res.results || []).length - okCount;
      if (failCount) {
        const firstErr = (res.results || []).find((r) => !r.ok)?.error || '';
        pushLog(t('diff.conflictResolveFail', { msg: firstErr }), 'error');
        addToast(t('diff.conflictResolveFail', { msg: firstErr }), 'error');
      } else {
        pushLog(t('diff.conflictResolved', { n: okCount }));
        addToast(t('diff.conflictResolved', { n: okCount }), 'success');
      }
      const resolved = conflicts.map((c) => c.path);
      const resolvedSet = new Set(resolved);
      setEntries((es) => es.filter((e) => !resolvedSet.has(e.path)));
      dropOverlay(resolved);
      setConflicts([]);
      setConflictResolutions({});
      setConflictMerged({});
      loadManifest();
    } catch (ex: any) {
      pushLog(t('diff.conflictResolveFail', { msg: ex.message }), 'error');
      addToast(ex.message, 'error');
    } finally {
      setResolving(false);
    }
  }, [conflicts, conflictResolutions, conflictMerged, pushLog, addToast, t, loadManifest, dropOverlay]);

  const mergeOne = useCallback(async () => {
    if (!selectedPath) return;
    try {
      const res = await apiPost<DiffMergeResp>('/api/diff/merge', {
        local_dir: localDirRef.current,
        path: selectedPath,
        compare_dir: compareDirRef.current.trim(),
      });
      // F4：单文件合并冲突 → 打开 3-way 决策面板
      if (res.conflict) {
        openConflicts([{
          path: res.path || selectedPath, kind: res.kind, base: res.base ?? null,
          ours: res.ours || '', theirs: res.theirs, is_binary: res.is_binary,
          remote_hash: res.remote_hash, local_hash: res.local_hash,
        }]);
        return;
      }
      if (res.ok) {
        if (res.skipped) {
          pushLog(t('diff.mergeSkipped', { path: selectedPath }), 'info');
          addToast(t('diff.mergeSkipped', { path: selectedPath }), 'success');
        } else {
          pushLog(t('diff.mergeOk', { path: selectedPath }));
          addToast(t('diff.mergeOk', { path: selectedPath }), 'success');
        }
        setEntries((es) => es.filter((e) => e.path !== selectedPath));
        dropOverlay([selectedPath]);
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
  }, [selectedPath, pushLog, addToast, t, loadManifest, openConflicts, dropOverlay]);

  // 批量合并核心（F13 mergeSelected 与 mergeAll 共用）。
  const runMerge = useCallback(async (targets: DiffEntry[], statusFilter: string = '') => {
    if (!targets.length) {
      const msg = statusFilter === 'remote_only' ? t('diff.noMergeCloudTarget') : t('diff.noMergeTarget');
      pushLog(msg, 'warning');
      addToast(msg, 'warn');
      return;
    }
    setBusy(true);
    mergeEtaStarted.current = false;
    const modeHint = statusFilter ? `（${t('diff.mergeRemoteOnly')}）` : '';
    pushLog(t('diff.batchMergeStart', { n: targets.length }) + modeHint);
    try {
      const query = statusFilter ? `?status_filter=${statusFilter}` : '';
      const reqs = targets.map((e) => ({
        local_dir: localDirRef.current,
        path: e.path,
        compare_dir: compareDirRef.current.trim(),
        status: e.status,
      }));
      const res = await apiPost<DiffMergeBatchResp>(`/api/diff/merge-batch${query}`, reqs);
      const all = res.results || [];
      const okPaths = new Set(all.filter((r) => r.ok).map((r) => r.path));
      const okCount = okPaths.size;
      // F4：冲突项不算失败，单独收集走 3-way 决策
      const conflictList = all
        .filter((r) => r.conflict)
        .map((r) => ({
          path: r.path, kind: r.kind, base: r.base ?? null, ours: r.ours || '',
          theirs: r.theirs, is_binary: r.is_binary,
          remote_hash: r.remote_hash, local_hash: r.local_hash,
        }));
      const failCount = all.filter((r) => !r.ok && !r.conflict).length;
      pushLog(t('diff.batchMergeDone', { ok: okCount, fail: failCount }) + modeHint);
      addToast(t('diff.batchMergeDone', { ok: okCount, fail: failCount }), (failCount || conflictList.length) ? 'warn' : 'success');
      setEntries((es) => es.filter((e) => !okPaths.has(e.path)));  // 冲突项保留，待决策
      // F13：合并完成后清掉被合并文件的勾选，避免重复操作。
      if (statusFilter === '') {
        useAppStore.getState().clearCheckedPaths();
      }
      setFileTitle(t('diff.batchMergeDone', { ok: okCount, fail: failCount }) + modeHint);
      setFileHtml('');
      loadManifest();
      if (conflictList.length) {
        addToast(t('diff.conflictsFound', { n: conflictList.length }), 'warn');
        openConflicts(conflictList);
      }
    } catch (ex: any) {
      pushLog(t('diff.mergeFailed', { path: ex.message }), 'error');
      addToast(ex.message, 'error');
    } finally {
      setBusy(false);
      setProgress({ visible: false });
    }
  }, [pushLog, addToast, setProgress, t, loadManifest, openConflicts]);

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
    await runMerge(targets, mergeRemoteOnly ? 'remote_only' : '');
  }, [entries, mergeRemoteOnly, ignoreLineEndings, runMerge]);

  // F13：仅合并文件树里勾选的文件（checkedPaths 与当前差异条目交集）。
  const mergeSelected = useCallback(async () => {
    const targets = checkedPaths
      .map((p) => entries.find((e) => e.path === p))
      .filter((e): e is DiffEntry => !!e);
    await runMerge(targets, '');
  }, [checkedPaths, entries, runMerge]);

  // F6：把当前扫描结果导出为 Markdown 报告（纯前端，无需后端）。
  const exportReport = useCallback(() => {
    if (!summary && entries.length === 0) {
      addToast(t('diff.noDiffFiles'), 'warn');
      return;
    }
    const s = summary || {};
    const lines: string[] = [];
    lines.push(`# ${t('diff.title')} — ${new Date().toLocaleString()}`);
    lines.push('');
    lines.push(`- ${t('diff.localDir')}: \`${localDir}\``);
    lines.push(`- compare_dir: \`${compareDir || '/'}\``);
    lines.push(`- total: ${s.total ?? 0} · modified: ${s.modified ?? 0} · local_only: ${s.local_only ?? 0} · remote_only: ${s.remote_only ?? 0} · same: ${s.same ?? 0} · whitespace_only: ${s.whitespace_only ?? 0}`);
    lines.push(`- merged_count: ${mergedCount}`);
    lines.push('');
    lines.push('## Entries');
    for (const e of entries) {
      lines.push(`- [${e.status}] ${e.path}${e.merged ? ' (merged)' : ''}`);
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `diff-report-${Date.now()}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    pushLog(`${t('diff.exportReport')} ✓`);
  }, [summary, entries, mergedCount, localDir, compareDir, t, pushLog, addToast]);

  return (
    <div className="diff-panel tab-inner wide">
      <div className={`card-soft clash-card diff-cfg-region${cfgOpen ? '' : ' collapsed'}`}>
        <div className="panel-header">
          <button type="button" className="clash-collapser" onClick={() => setCfgOpen((o) => !o)}>
            <span className="cfd-caret">{cfgOpen ? '▾' : '▸'}</span>
            <span className="section-title">{t('diff.sectionConfig')}</span>
          </button>
        </div>
        {cfgOpen && (
        <div className="card-body">
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
            <button
              className="btn btn-sm btn-ghost"
              type="button"
              onClick={() => (showDirChooser ? setShowDirChooser(false) : loadSubDirs())}
              aria-expanded={showDirChooser}
            >
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
        </div>
        )}
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
        <div className={`card-soft clash-card diff-summary-region${summaryOpen ? '' : ' collapsed'}`}>
          <div className="panel-header">
            <button type="button" className="clash-collapser" onClick={() => setSummaryOpen((o) => !o)}>
              <span className="cfd-caret">{summaryOpen ? '▾' : '▸'}</span>
              <span className="section-title">{t('diff.sectionSummary')}</span>
            </button>
          </div>
          {summaryOpen && (
          <div className="card-body">
            <div className="diff-summary">
          {`${summary.total ?? 0} · `}
          <span className="badge-modified">{t('diff.merge')} {summary.modified ?? 0}</span> ·{' '}
          <span className="badge-local">{t('diff.local')} {summary.local_only ?? 0}</span> ·{' '}
          <span className="badge-remote">{t('diff.remote')} {summary.remote_only ?? 0}</span> · {t('diff.noDiff')} {summary.same ?? 0}
          {summary.whitespace_only ? <span className="badge-eol"> {t('diff.ignoreEol')} {summary.whitespace_only}</span> : null}
          {mergedCount > 0 ? <span className="badge-merged"> · {t('diff.mergedBadge', { n: mergedCount })}</span> : null}
          </div>
        </div>
        )}
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
            <span className="diff-file-title">
              {history
                ? `${t('diff.historyVersion')} · ${history.path}`
                : fileTitle}
            </span>
            {/* F2：历史版本预览模式下用「返回差异」替代合并系列按钮 */}
            {history ? (
              <button className="btn btn-sm btn-ghost" onClick={backToDiff}>{t('diff.backToDiff')}</button>
            ) : (
              <>
                {selectedPath && (
                  <button className="btn btn-sm btn-primary" onClick={mergeOne} disabled={busy}>{t('diff.mergeOne')}</button>
                )}
                {entries.length > 0 && (
                  <button className="btn btn-sm btn-primary" onClick={mergeAll} disabled={busy}>
                    {t('diff.mergeAll')}
                  </button>
                )}
                {/* F13：仅合并文件树里勾选的文件 */}
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={mergeSelected}
                  disabled={busy || checkedPaths.length === 0}
                  title={checkedPaths.length ? '' : t('diff.noMergeTarget')}
                >
                  {t('diff.mergeSelected')}{checkedPaths.length ? ` (${checkedPaths.length})` : ''}
                </button>
                {/* F6：导出差异/合并报告为 Markdown */}
                <button className="btn btn-sm btn-ghost" onClick={exportReport} disabled={busy}>
                  {t('diff.exportReport')}
                </button>
              </>
            )}
          </div>
          {historyLoading ? (
            <div className="empty-hint">{t('diff.loadingDiff')}</div>
          ) : history ? (
            history.err ? (
              <div className="empty-hint">{esc(history.err)}</div>
            ) : (
              <div className="diff-content" dangerouslySetInnerHTML={{ __html: renderHistoryCompare(history.localContent, history.content, t) }} />
            )
          ) : fileHtml ? (
            <div className="diff-content" dangerouslySetInnerHTML={{ __html: fileHtml }} />
          ) : (
            <div className="empty-hint">{t('diff.selectFileDiff')}</div>
          )}
        </div>
      </div>

      {/* 最近更新记录（git log 风格） */}
      <div className={`card-soft clash-card diff-commits-region${commitsOpen ? '' : ' collapsed'}`}>
        <div className="panel-header">
          <button type="button" className="clash-collapser" onClick={() => setCommitsOpen((o) => !o)}>
            <span className="cfd-caret">{commitsOpen ? '▾' : '▸'}</span>
            <span className="section-title">{t('diff.recentUpdates')}{compareDir ? ` · ${compareDir}` : ''}</span>
          </button>
          <button className="btn btn-sm btn-ghost" onClick={loadCommits} disabled={commitsLoading || !compareRepo}>
            {commitsLoading ? t('common.loading') : t('diff.refresh')}
          </button>
        </div>
        {commitsOpen && (
        <div className="card-body">
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
                      <span
                        key={i}
                        className={`commit-file ct-${f.change_type || ''}`}
                        role="button"
                        tabIndex={0}
                        title={t('diff.viewHistoryVersion')}
                        onClick={() => openHistoryFile(c.commit_id, c.message, f.path, f.change_type || '')}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            openHistoryFile(c.commit_id, c.message, f.path, f.change_type || '');
                          }
                        }}
                      >
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
        )}
      </div>

      {/* F4：合并冲突 3-way 决策面板 */}
      {conflicts.length > 0 && (
        <div className="modal-mask" onClick={() => setConflicts([])}>
          <div className="modal conflict-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{t('diff.conflictTitle')}（{conflicts.length}）</h3>
              <button className="btn btn-sm btn-ghost" onClick={() => setConflicts([])}>{t('common.close')}</button>
            </div>
            <div className="modal-body">
            <div className="conflict-hint">{t('diff.conflictHint')}</div>
            <div className="conflict-list">
              {conflicts.map((c) => {
                const res = conflictResolutions[c.path] || (c.is_binary ? 'theirs' : 'merged');
                return (
                  <div key={c.path} className="conflict-item">
                    <div className="conflict-path">{c.path}</div>
                    <div className="conflict-choices">
                      <label className="rd"><input type="radio" name={`res-${c.path}`} checked={res === 'ours'} onChange={() => setConflictResolutions((p) => ({ ...p, [c.path]: 'ours' }))} /> {t('diff.keepLocal')}</label>
                      {!c.is_binary && (
                        <>
                          <label className="rd"><input type="radio" name={`res-${c.path}`} checked={res === 'theirs'} onChange={() => setConflictResolutions((p) => ({ ...p, [c.path]: 'theirs' }))} /> {t('diff.useRemote')}</label>
                          <label className="rd"><input type="radio" name={`res-${c.path}`} checked={res === 'merged'} onChange={() => setConflictResolutions((p) => ({ ...p, [c.path]: 'merged' }))} /> {t('diff.manualMerge')}</label>
                        </>
                      )}
                    </div>
                    {!c.is_binary && res === 'merged' && (
                      <div className="conflict-3way">
                        <div className="conflict-panes">
                          <div className="cpane"><div className="cpane-h">{t('diff.conflictBase')}</div><pre className="cpane-pre">{esc(c.base ?? '')}</pre></div>
                          <div className="cpane"><div className="cpane-h">{t('diff.conflictOurs')}</div><pre className="cpane-pre">{esc(c.ours)}</pre></div>
                          <div className="cpane"><div className="cpane-h">{t('diff.conflictTheirs')}</div><pre className="cpane-pre">{esc(c.theirs ?? '')}</pre></div>
                        </div>
                        <div className="cpane-h">{t('diff.manualMerge')}</div>
                        <textarea
                          className="conflict-merged"
                          value={conflictMerged[c.path] ?? ''}
                          onChange={(e) => setConflictMerged((p) => ({ ...p, [c.path]: e.target.value }))}
                          spellCheck={false}
                        />
                      </div>
                    )}
                    {c.is_binary && (
                      <div className="conflict-hint">{t('diff.conflictBinary')}</div>
                    )}
                  </div>
                );
              })}
            </div>
            </div>
            <div className="modal-footer">
              <button className="btn btn-primary" onClick={resolveConflicts} disabled={resolving}>
                {resolving ? t('common.loading') : t('diff.resolveConflicts')}
              </button>
            </div>
          </div>
        </div>
      )}
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
  return renderUnifiedDiff(diffText, t);
}

function renderUnifiedDiff(diffText: string, t: (k: string, v?: Record<string, string | number>) => string): string {
  const lines = diffText.split('\n');
  const fileHeaders: string[] = [];
  type Hunk = { header: string; rows: string[]; adds: number; dels: number };
  const hunks: Hunk[] = [];
  let cur: Hunk | null = null;
  let oldNo = 0, newNo = 0;
  for (const line of lines) {
    if (!line) continue;
    // 文件头（--- / +++）始终可见，不计入 hunk
    if (line.startsWith('---') || line.startsWith('+++')) {
      fileHeaders.push(esc(line));
      continue;
    }
    if (line.startsWith('@@')) {
      const m = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
      if (m) { oldNo = parseInt(m[1]) - 1; newNo = parseInt(m[2]) - 1; }
      cur = { header: line, rows: [], adds: 0, dels: 0 };
      hunks.push(cur);
      continue;
    }
    if (!cur) {
      // 无 @@ 头（整文件新增 / 删除）：包成单个 hunk
      cur = { header: t('diff.wholeFileDiff'), rows: [], adds: 0, dels: 0 };
      hunks.push(cur);
    }
    let type = 'ctx';
    let oldCell = '', newCell = '', content = line;
    if (line.startsWith('+')) {
      type = 'add'; newNo++; content = line.substring(1); newCell = String(newNo); cur.adds++;
    } else if (line.startsWith('-')) {
      type = 'del'; oldNo++; content = line.substring(1); oldCell = String(oldNo); cur.dels++;
    } else if (line.startsWith(' ')) {
      type = 'ctx'; oldNo++; newNo++; content = line.substring(1); oldCell = String(oldNo); newCell = String(newNo);
    }
    cur.rows.push(
      `<tr class="diff-row diff-${type}">` +
      `<td class="diff-ln">${oldCell}</td>` +
      `<td class="diff-ln">${newCell}</td>` +
      `<td class="diff-sign">${type === 'add' ? '+' : type === 'del' ? '-' : ''}</td>` +
      `<td class="diff-code">${esc(content)}</td>` +
      `</tr>`
    );
  }
  if (hunks.length === 0) {
    return `<div class="empty-hint">${t('diff.sameContent')}</div>`;
  }
  const headerHtml = fileHeaders.length
    ? `<div class="diff-file-headers">${fileHeaders.map((h) => `<div class="diff-file-header">${h}</div>`).join('')}</div>`
    : '';
  // 每个 hunk 用原生 <details> 包裹，默认展开、点击折叠，无需 JS。
  const hunkHtml = hunks
    .map((h) => {
      const stats =
        h.adds || h.dels
          ? `<span class="diff-hunk-stats"><span class="diff-stat-add">+${h.adds}</span> <span class="diff-stat-del">-${h.dels}</span></span>`
          : '';
      return (
        `<details class="diff-hunk" open>` +
        `<summary class="diff-hunk-head"><span class="diff-hunk-title">${esc(h.header)}</span>${stats}</summary>` +
        `<div class="diff-hunk-body"><table class="diff-table">${h.rows.join('')}</table></div>` +
        `</details>`
      );
    })
    .join('');
  return headerHtml + hunkHtml;
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

// F2：历史版本（commit 中的内容）与本地当前内容左右对比。
function renderHistoryCompare(local: string, remote: string, t: (k: string, v?: Record<string, string | number>) => string): string {
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
  const head =
    `<thead><tr class="diff-col-head">` +
    `<th></th><th>${esc(t('diff.localNow'))}</th>` +
    `<th></th><th>${esc(t('diff.historyNow'))}</th>` +
    `</tr></thead>`;
  return `<div class="diff-sidebyside"><table class="diff-table diff-sidebyside-table">${head}<tbody>${rows.join('')}</tbody></table></div>`;
}
