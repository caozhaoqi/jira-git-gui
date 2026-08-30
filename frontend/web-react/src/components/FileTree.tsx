import { useEffect, useMemo, useRef, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet, apiPost } from '../api/client';
import type { TreeEntry, TreeResp, SearchResp, SearchHit } from '../api/types';
import { useT } from '../i18n';

type SortKey = 'name' | 'type' | 'size' | 'mtime';
type SortDir = 'asc' | 'desc';
type SearchScope = 'filename' | 'content';

function sortEntries(
  entries: TreeEntry[],
  key: SortKey,
  dir: SortDir
): TreeEntry[] {
  const mul = dir === 'desc' ? -1 : 1;
  return [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    let cmp = 0;
    if (key === 'name') cmp = a.name.localeCompare(b.name, undefined, { numeric: true });
    else if (key === 'type') cmp = (a.type || '').localeCompare(b.type || '');
    else if (key === 'size') cmp = (a.size || 0) - (b.size || 0);
    else if (key === 'mtime') cmp = (a.mtime || 0) - (b.mtime || 0);
    return cmp * mul;
  });
}

export function FileTree() {
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const selectedFilePath = useAppStore((s) => s.selectedFilePath);
  const setSelectedFile = useAppStore((s) => s.setSelectedFile);
  const checkedPaths = useAppStore((s) => s.checkedPaths);
  const toggleCheckedPath = useAppStore((s) => s.toggleCheckedPath);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const treeLocalDir = useAppStore((s) => s.treeLocalDir);
  const setTreeLocalDir = useAppStore((s) => s.setTreeLocalDir);
  const { t } = useT();

  const [rootEntries, setRootEntries] = useState<TreeEntry[]>([]);
  const [dirCache, setDirCache] = useState<Record<string, TreeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  // 文件树为空的真实原因（Cookie 失效 / 分支解析失败 / 浏览页不可用…）。
  // 后端 /api/tree 现在会带回这个原因；没有它，界面只会显示「空」，
  // 用户完全不知道该更新 Cookie 还是改用 PAT 克隆。
  const [treeError, setTreeError] = useState('');
  const [cloning, setCloning] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>('name');
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const [searchScope, setSearchScope] = useState<SearchScope>('filename');
  const [searchQ, setSearchQ] = useState('');
  const [searchResults, setSearchResults] = useState<SearchHit[] | null>(null);
  const [searchStatus, setSearchStatus] = useState('');
  const [searching, setSearching] = useState(false);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function loadRoot() {
    setLoading(true);
    setTreeError('');
    try {
      const ld = treeLocalDir.trim();
      const url = ld
        ? `/api/tree?path=&local_dir=${encodeURIComponent(ld)}`
        : '/api/tree?path=';
      const res = await apiGet<TreeResp>(url);
      if (res.error) {
        setRootEntries([]);
        setTreeError(res.error);
        pushLog(`${t('repo.fileTree')}：${res.error}`, 'error');
        addToast(res.error, 'error');
      } else {
        setRootEntries(res.entries || []);
        pushLog(`${t('repo.fileTree')}：${(res.entries || []).length}`);
      }
    } catch (e: any) {
      setRootEntries([]);
      setTreeError(e.message || t('file.loadErr'));
      pushLog(`${t('repo.fileTree')}：${e.message}`, 'error');
      addToast(e.message, 'error');
    } finally {
      setLoading(false);
    }
  }

  /** 远端浏览不可用时的逃生口：克隆到本地后文件树改走本地 git，不再依赖 Jira 页面。 */
  async function cloneRepo() {
    setCloning(true);
    try {
      await apiPost('/api/clone', {});
      pushLog(t('repo.cloneStart'));
      addToast(t('repo.cloneStart'), 'info');
    } catch (e: any) {
      pushLog(t('repo.cloneFail', { msg: e.message }), 'error');
      addToast(e.message, 'error');
    } finally {
      setCloning(false);
    }
  }

  useEffect(() => {
    if (!selectedRepo && !treeLocalDir.trim()) {
      setRootEntries([]);
      setDirCache({});
      setExpanded(new Set());
      return;
    }
    loadRoot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRepo?.repo_id, treeLocalDir]);

  async function toggleDir(entry: TreeEntry) {
    const isOpen = expanded.has(entry.path);
    if (!isOpen) {
      if (!dirCache[entry.path]) {
        try {
          const ld = treeLocalDir.trim();
          const url = ld
            ? `/api/tree?path=${encodeURIComponent(entry.path)}&local_dir=${encodeURIComponent(ld)}`
            : `/api/tree?path=${encodeURIComponent(entry.path)}`;
          const res = await apiGet<TreeResp>(url);
          setDirCache((c) => ({ ...c, [entry.path]: res.entries || [] }));
        } catch (e: any) {
          addToast(e.message, 'error');
          return;
        }
      }
      setExpanded((s) => new Set(s).add(entry.path));
    } else {
      setExpanded((s) => {
        const n = new Set(s);
        n.delete(entry.path);
        return n;
      });
    }
  }

  function onFileClick(entry: TreeEntry) {
    setSelectedFile(entry.path);
  }

  function runSearch() {
    const q = searchQ.trim();
    if (!q) {
      setSearchResults(null);
      setSearchStatus('');
      return;
    }
    if (searchScope === 'filename') {
      const all: TreeEntry[] = [rootEntries, ...Object.values(dirCache)].flat();
      const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
      const hits = all
        .filter((e) => re.test(e.name))
        .map((e) => ({ path: e.path, name: e.name }));
          setSearchResults(hits as SearchHit[]);
          setSearchStatus(t('file.searchStatus', { n: hits.length }));
    } else {
      setSearching(true);
      setSearchStatus(t('common.loading'));
      apiGet<SearchResp>(
        `/api/search?q=${encodeURIComponent(q)}&scope=content&limit=200`
      )
        .then((res) => {
          if (res.error) {
            setSearchResults(null);
            setSearchStatus(res.error);
            return;
          }
          setSearchResults(res.results || []);
          setSearchStatus(t('file.searchMatches', { n: res.total ?? 0 }));
        })
        .catch((e) => {
          setSearchResults(null);
          setSearchStatus(e.message);
        })
        .finally(() => setSearching(false));
    }
  }

  useEffect(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(runSearch, 200);
    return () => {
      if (searchTimer.current) clearTimeout(searchTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQ, searchScope]);

  const isOpenDir = (path: string) => expanded.has(path);

  function renderNode(entry: TreeEntry) {
    const isDir = entry.type === 'dir';
    const isOpen = isOpenDir(entry.path);
    return (
      <div className="tree-node" key={entry.path}>
        <div
          className={`tree-row ${isDir ? '' : 'file'} ${
            selectedFilePath === entry.path ? 'selected' : ''
          }`}
          onClick={() => (isDir ? toggleDir(entry) : onFileClick(entry))}
        >
          {!isDir && (
            <input
              type="checkbox"
              className="tree-checkbox"
              checked={checkedPaths.includes(entry.path)}
              onClick={(e) => e.stopPropagation()}
              onChange={() => toggleCheckedPath(entry.path)}
            />
          )}
          <span className="tree-toggle">{isDir ? (isOpen ? '▼' : '▶') : ''}</span>
          <span className="tree-icon">{isDir ? '📁' : '📄'}</span>
          <span className="tree-name">{entry.name}</span>
          {/* 仅在有数据时才渲染 size / mtime 列；否则那两列会固定占 60+120=180px，
              把 flex:1 的 name 挤成 "trans..." 截断。 */}
          {entry.size != null && (
            <span className="tree-size">{fmtSize(entry.size)}</span>
          )}
          {entry.mtime ? (
            <span className="tree-mtime" title={new Date(entry.mtime * 1000).toLocaleString()}>
              {fmtMtime(entry.mtime)}
            </span>
          ) : null}
        </div>
        {isDir && isOpen && dirCache[entry.path] && (
          <div className="tree-children">
            {sortEntries(dirCache[entry.path], sortKey, sortDir).map(renderNode)}
          </div>
        )}
      </div>
    );
  }

  const sortedRoot = useMemo(
    () => sortEntries(rootEntries, sortKey, sortDir),
    [rootEntries, sortKey, sortDir]
  );

  return (
    <div className="file-tree-pane">
      <div className="panel-header">
        <h2 className="section-title">{t('file.browser')}</h2>
        <span className="panel-sub">{t('file.checkedHint')}</span>
      </div>
      <div className="tree-local-bar">
        <label className="field-inline">
          {t('file.localDir')}
          <input
            className="input tree-local-input"
            placeholder={t('file.localDirPlaceholder')}
            value={treeLocalDir}
            onChange={(e) => setTreeLocalDir(e.target.value)}
          />
        </label>
      </div>
      <div className="tree-toolbar">
        <div className="tree-toolbar-row">
          <div className="tree-search-wrap">
            <input
              className="input tree-search-input"
              placeholder={`🔍 ${t('file.browser')}`}
              value={searchQ}
              onChange={(e) => setSearchQ(e.target.value)}
            />
            <div className="tree-scope-toggle" role="tablist">
              <button
                className={`tree-scope-btn ${searchScope === 'filename' ? 'active' : ''}`}
                onClick={() => setSearchScope('filename')}
              >
                {t('file.searchName')}
              </button>
              <button
                className={`tree-scope-btn ${searchScope === 'content' ? 'active' : ''}`}
                onClick={() => setSearchScope('content')}
              >
                {t('file.searchContent')}
              </button>
            </div>
          </div>
          <div className="tree-sort-wrap">
            <label className="field-inline">
              {t('file.sortLabel')}
              <select
                className="sel tree-sort-key"
                value={sortKey}
                onChange={(e) => setSortKey(e.target.value as SortKey)}
              >
                <option value="name">{t('file.sortName')}</option>
                <option value="mtime">{t('file.sortMtime')}</option>
                <option value="type">{t('file.sortType')}</option>
                <option value="size">{t('file.sortSize')}</option>
              </select>
            </label>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))}
              title={sortDir === 'asc' ? '↑' : '↓'}
            >
              {sortDir === 'asc' ? '↑' : '↓'}
            </button>
          </div>
        </div>
      </div>

      {searchResults && (
        <div className="tree-search-results">
          <div className="tree-search-status">{searchStatus}</div>
          {searching && <div className="tree-search-loading">{t('common.loading')}</div>}
          {!searching &&
            (searchResults.length === 0 ? (
              <div className="tree-search-empty">{t('cf.noData')}</div>
            ) : (
              searchResults.slice(0, 100).map((h, i) => (
                <div
                  key={i}
                  className="tree-search-item"
                  onClick={() => setSelectedFile(h.path)}
                >
                  <span className="tsr-icon">📄</span>
                  <span className="tsr-path">{h.path}</span>
                  {h.line != null && <span className="tsr-line">:{h.line}</span>}
                  {h.snippet && <div className="tsr-snippet">{h.snippet.slice(0, 200)}</div>}
                </div>
              ))
            ))}
        </div>
      )}

      <div className="tree-container">
        {!selectedRepo && (
          <div className="empty-hint">{t('file.noRepo')}</div>
        )}
        {selectedRepo && loading && <div className="tree-loading">{t('common.loading')}</div>}
        {treeError && (
          <div className="tree-error-box">
            <div className="tree-error-title">⚠️ {t('file.treeError')}</div>
            <div className="tree-error-msg">{treeError}</div>
            {!treeLocalDir.trim() && (
              <button
                className="btn btn-sm btn-primary"
                onClick={cloneRepo}
                disabled={cloning || !selectedRepo}
                title="克隆到本地后文件树直接读本地 git，不再依赖 Jira 浏览页"
              >
                {cloning ? t('common.loading') : t('repo.clone')}
              </button>
            )}
          </div>
        )}
        {selectedRepo && !loading && !treeError && sortedRoot.length === 0 && (
          <div className="empty-hint">{t('file.empty')}</div>
        )}
        {sortedRoot.map(renderNode)}
      </div>
    </div>
  );
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} K`;
  return `${(n / 1048576).toFixed(1)} M`;
}

function fmtMtime(ts: number): string {
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
