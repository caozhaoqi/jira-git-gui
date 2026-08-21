import { useEffect, useMemo, useRef, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet } from '../api/client';
import type { TreeEntry, TreeResp, SearchResp, SearchHit } from '../api/types';

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
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);

  const [rootEntries, setRootEntries] = useState<TreeEntry[]>([]);
  const [dirCache, setDirCache] = useState<Record<string, TreeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
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
    try {
      const res = await apiGet<TreeResp>('/api/tree?path=');
      if (res.error) {
        setRootEntries([]);
        pushLog(`加载文件树失败：${res.error}`, 'error');
        addToast(res.error, 'error');
      } else {
        setRootEntries(res.entries || []);
        pushLog(`文件树已加载，共 ${(res.entries || []).length} 项。`);
      }
    } catch (e: any) {
      setRootEntries([]);
      pushLog(`加载文件树失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!selectedRepo) {
      setRootEntries([]);
      setDirCache({});
      setExpanded(new Set());
      return;
    }
    loadRoot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedRepo?.repo_id]);

  async function toggleDir(entry: TreeEntry) {
    const isOpen = expanded.has(entry.path);
    if (!isOpen) {
      // 懒加载子目录
      if (!dirCache[entry.path]) {
        try {
          const res = await apiGet<TreeResp>(
            `/api/tree?path=${encodeURIComponent(entry.path)}`
          );
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

  // ===== 搜索 =====
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
      setSearchStatus(`已加载目录中匹配 ${hits.length} 项`);
    } else {
      setSearching(true);
      setSearchStatus('搜索中…');
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
          setSearchStatus(
            `共 ${res.total} 处匹配${res.truncated ? '（已截断）' : ''}`
          );
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
          <span className="tree-toggle">{isDir ? (isOpen ? '▼' : '▶') : ''}</span>
          <span className="tree-icon">{isDir ? '📁' : '📄'}</span>
          <span className="tree-name">{entry.name}</span>
          <span className="tree-size">
            {entry.size != null ? fmtSize(entry.size) : ''}
          </span>
          <span className="tree-mtime" title={entry.mtime ? new Date(entry.mtime * 1000).toLocaleString() : ''}>
            {entry.mtime ? fmtMtime(entry.mtime) : ''}
          </span>
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
      <div className="pane-toolbar tree-toolbar">
        <div className="tree-search">
          <input
            className="search-input"
            placeholder={searchScope === 'filename' ? '搜索文件名…' : '搜索文件内容…'}
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
          />
          <button
            className={`tree-scope-btn ${searchScope === 'filename' ? 'active' : ''}`}
            onClick={() => setSearchScope('filename')}
          >
            文件名
          </button>
          <button
            className={`tree-scope-btn ${searchScope === 'content' ? 'active' : ''}`}
            onClick={() => setSearchScope('content')}
          >
            内容
          </button>
        </div>
        <div className="tree-sort">
          <select
            value={sortKey}
            onChange={(e) => setSortKey(e.target.value as SortKey)}
            title="排序字段"
          >
            <option value="name">名称</option>
            <option value="type">类型</option>
            <option value="size">大小</option>
            <option value="mtime">修改时间</option>
          </select>
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))}
            title={sortDir === 'asc' ? '当前升序，点击降序' : '当前降序，点击升序'}
          >
            {sortDir === 'asc' ? '↑' : '↓'}
          </button>
        </div>
      </div>

      {searchResults && (
        <div className="tree-search-results">
          <div className="tree-search-status">{searchStatus}</div>
          {searching && <div className="tree-search-loading">搜索中…</div>}
          {!searching &&
            (searchResults.length === 0 ? (
              <div className="tree-search-empty">没有匹配结果</div>
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
          <div className="empty-hint">请先在左侧选择仓库</div>
        )}
        {selectedRepo && loading && <div className="tree-loading">加载中…</div>}
        {selectedRepo && !loading && sortedRoot.length === 0 && (
          <div className="empty-hint">该仓库无可浏览文件</div>
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
