import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiPost } from '../../api/client';
import type {
  K8sFileEntry, K8sFileListResp, K8sFileReadResp, K8sFileWriteResp, K8sFileSearchResp, K8sFileSearchHit, K8sPodsResp,
} from '../../api/types';
import { useK8s } from './context';
import { k8sPathJoin, k8sPathParent, fmtSize } from '../../utils/format';
import { langFromName, highlightCode } from '../../utils/highlight';
import { useT } from '../../i18n';

type SortKey = 'name' | 'type' | 'size' | 'mtime';
interface SelectedFile { name: string; isDir: boolean; }

export function K8sFiles() {
  const { target, setTarget, addToast } = useK8s();
  const { t } = useT();

  const [path, setPath] = useState('/');
  const [entries, setEntries] = useState<K8sFileEntry[]>([]);
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'name', dir: 'asc' });
  const [selected, setSelected] = useState<SelectedFile | null>(null);
  const [editPath, setEditPath] = useState('');
  const [editContent, setEditContent] = useState('');
  const [editMsg, setEditMsg] = useState('');
  const [editTruncated, setEditTruncated] = useState(false);
  const [editFullscreen, setEditFullscreen] = useState(false);
  // 查看模式（高亮只读）与编辑模式（textarea 可保存）切换
  const [editMode, setEditMode] = useState(false);
  const [editLang, setEditLang] = useState('');
  const [searchQ, setSearchQ] = useState('');
  const [searchStatus, setSearchStatus] = useState('');
  const [searchResults, setSearchResults] = useState<K8sFileSearchHit[]>([]);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [podList, setPodList] = useState<{ name: string; phase?: string }[]>([]);

  const loadPods = useCallback(async () => {
    if (!target.env) return;
    const q = encodeURIComponent(target.namespace.trim());
    try {
      const d = await api<K8sPodsResp>(`/api/k8s/pods?env=${encodeURIComponent(target.env)}&namespace=${q}`);
      if (d.ok) setPodList(d.pods || []);
    } catch {
      setPodList([]);
    }
  }, [target.env, target.namespace]);

  useEffect(() => { if (target.env) loadPods(); }, [target.env, loadPods]);

  const listFiles = useCallback(async (p?: string) => {
    const pp = p !== undefined ? p : path;
    if (!target.pod) {
      setEntries([]);
      return;
    }
    if (p !== undefined) setPath(pp);
    try {
      const d = await apiPost<K8sFileListResp>('/api/k8s/file/list', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: pp,
      });
      if (!d.ok) { setEntries([]); addToast(t('k8s.files.loadFail') + (d.error || ''), 'error'); return; }
      setEntries(d.entries || []);
    } catch (ex: any) {
      setEntries([]);
      addToast(t('k8s.files.loadFail') + ex.message, 'error');
    }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, t]);

  useEffect(() => {
    if (target.pod) listFiles(path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.env, target.pod]);

  const sorted = useMemoSort(entries, sort);

  // 高亮后的 HTML（仅在查看模式使用）
  const highlightedHtml = useMemo(
    () => (editPath ? highlightCode(editContent, editPath) : ''),
    [editPath, editContent]
  );

  const openFile = useCallback(async (name: string, isDir: boolean) => {
    const full = k8sPathJoin(path, name);
    if (isDir) { listFiles(full); return; }
    try {
      const d = await apiPost<K8sFileReadResp>('/api/k8s/file/read', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: full, max_bytes: 200000,
      });
      if (!d.ok) { addToast(t('k8s.files.readFail') + (d.error || ''), 'error'); return; }
      if (d.is_binary) { addToast(t('k8s.files.binaryFile'), 'info'); return; }
      setEditPath(full);
      setEditContent(d.content || '');
      setEditLang(langFromName(full).label);
      setEditMode(false); // 默认以高亮查看模式打开
      setEditTruncated(!!d.truncated);
      setEditMsg('');
    } catch (ex: any) {
      addToast(t('k8s.files.readFail') + ex.message, 'error');
    }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, listFiles, t]);

  const saveFile = useCallback(async () => {
    if (!editPath) return;
    setEditMsg(t('k8s.files.saveMsgSaving'));
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/write', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: editPath, content: editContent,
      });
      if (!d.ok) { setEditMsg(t('k8s.files.saveFail') + (d.error || '')); return; }
      setEditMsg(t('k8s.files.saveDone'));
      listFiles(path);
    } catch (ex: any) {
      setEditMsg(t('k8s.files.saveFail') + ex.message);
    }
  }, [editPath, editContent, target.env, target.pod, target.container, target.namespace, listFiles, path, t]);

  const fileDownload = useCallback(() => {
    const blob = new Blob([editContent], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (editPath.split('/').pop()) || 'file.txt';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }, [editContent, editPath]);

  const mkdir = useCallback(async () => {
    const name = window.prompt(t('k8s.files.newFolderPrompt'));
    if (!name) return;
    const full = k8sPathJoin(path, name.trim());
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/mkdir', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: full,
      });
      if (!d.ok) { addToast(t('k8s.files.mkdirFail') + (d.error || ''), 'error'); return; }
      listFiles(path);
    } catch (ex: any) { addToast(t('k8s.files.mkdirFail') + ex.message, 'error'); }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, listFiles, t]);

  const upload = useCallback(async (file: File) => {
    const data = await fileToBase64(file);
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/upload', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: path, data,
      });
      if (!d.ok) { addToast(t('k8s.files.uploadFail') + (d.error || ''), 'error'); return; }
      listFiles(path);
    } catch (ex: any) { addToast(t('k8s.files.uploadFail') + ex.message, 'error'); }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, listFiles, t]);

  const del = useCallback(async () => {
    if (!selected) { addToast(t('k8s.files.selectToDelete'), 'warn'); return; }
    if (!window.confirm(t('k8s.files.confirmDelete', { type: selected.isDir ? t('k8s.files.dir') : t('k8s.files.file'), name: selected.name }))) return;
    const full = k8sPathJoin(path, selected.name);
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/delete', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: full, is_dir: selected.isDir,
      });
      if (!d.ok) { addToast(t('k8s.files.deleteFail') + (d.error || ''), 'error'); return; }
      setSelected(null);
      listFiles(path);
    } catch (ex: any) { addToast(t('k8s.files.deleteFail') + ex.message, 'error'); }
  }, [selected, path, target.env, target.pod, target.container, target.namespace, addToast, listFiles, t]);

  const doSearch = useCallback(async () => {
    const q = searchQ.trim();
    if (!q) { setSearchStatus(t('k8s.files.searchStatusNoKw')); return; }
    if (!target.pod) { setSearchStatus(t('k8s.files.searchStatusNoPod')); return; }
    setSearchStatus(t('k8s.files.searchStatusSearching'));
    setSearchResults([]);
    try {
      const d = await apiPost<K8sFileSearchResp>('/api/k8s/file/search', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, q, path,
      });
      if (!d.ok) { setSearchStatus(t('k8s.files.searchFail') + (d.error || '')); return; }
      setSearchResults(d.results || []);
      const n = d.total ?? 0;
      setSearchStatus(
        t('k8s.files.searchStatusMatches', { n }) + (d.truncated ? t('k8s.files.searchStatusTruncated') : '')
      );
    } catch (ex: any) { setSearchStatus(t('k8s.files.searchFail') + ex.message); }
  }, [searchQ, target.env, target.pod, target.container, target.namespace, path, t]);

  const toggleSort = (key: SortKey) => {
    setSort((s) => ({
      key,
      dir: s.key === key ? (s.dir === 'asc' ? 'desc' : 'asc') : 'asc',
    }));
  };

  return (
    <div className="k8s-files">
      <div className="panel-header">
        <h2 className="section-title">{t('k8s.files.browser')}</h2>
        <span className="panel-sub">{t('k8s.files.browserHint')}</span>
      </div>

      <div className="k8s-files-toolbar card-soft">
        {/* 接続 + 検索行 */}
        <div className="k8s-files-connrow">
          <select
            className="sel k8s-files-podsel"
            value={target.pod}
            onChange={(e) => setTarget({ pod: e.target.value })}
          >
            <option value="">{t('k8s.shell.selectPod')}</option>
            {podList.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name} · {p.phase || ''}
              </option>
            ))}
          </select>
          <input
            className="input input-sm k8s-files-ns"
            value={target.namespace}
            onChange={(e) => setTarget({ namespace: e.target.value })}
            placeholder={t('k8s.files.path')}
          />
          <div className="spacer" />
          <input
            className="input input-sm k8s-files-search-input"
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
            placeholder={t('k8s.files.searchPlaceholder')}
            onKeyDown={(e) => e.key === 'Enter' && doSearch()}
          />
          <button className="btn btn-sm btn-primary" onClick={doSearch}>
            {t('k8s.files.search')}
          </button>
          {searchStatus && <span className="k8s-files-search-status">{searchStatus}</span>}
        </div>

        {/* パス + 操作行 */}
        <div className="k8s-files-navrow">
          <div className="k8s-files-breadcrumb">
            <span className="k8s-files-bc-label">{t('k8s.files.path')}</span>
            <div className="k8s-files-path">
              <span className="k8s-files-crumb" onClick={() => listFiles('/')}>
                📁 /
              </span>
              {path.split('/').filter(Boolean).map((p, i, arr) => {
                const seg = '/' + arr.slice(0, i + 1).join('/');
                return (
                  <span key={i}>
                    <span className="k8s-files-sep">/</span>
                    <span className="k8s-files-crumb" onClick={() => listFiles(seg)}>
                      {p}
                    </span>
                  </span>
                );
              })}
            </div>
          </div>
          <div className="k8s-files-actions">
            <button className="btn btn-sm" onClick={() => listFiles(k8sPathParent(path))} title={t('k8s.files.up')}>
              ↑ {t('k8s.files.up')}
            </button>
            <button className="btn btn-sm" onClick={() => listFiles(path)}>
              {t('k8s.files.refresh')}
            </button>
            <button className="btn btn-sm" onClick={mkdir}>
              {t('k8s.files.mkdirShort')}
            </button>
            <button className="btn btn-sm" onClick={() => fileInputRef.current?.click()}>
              {t('k8s.files.uploadShort')}
            </button>
            <button className="btn btn-sm btn-ghost" onClick={del}>
              {t('k8s.files.deleteShort')}
            </button>
          </div>
        </div>
      </div>

      {searchResults.length > 0 && (
        <div className="k8s-files-search-results">
          {searchResults.map((r, i) => (
            <div
              key={i}
              className="tree-search-item"
              onClick={async () => {
                const dir = r.path.includes('/')
                  ? r.path.slice(0, r.path.lastIndexOf('/'))
                  : path;
                await listFiles(dir);
                const name = r.path.split('/').pop() || r.path;
                openFile(name, false);
              }}
            >
              <span className="tsr-icon">📄</span>
              <span className="tsr-path">{r.path}</span>
              <span className="tsr-line">:{r.line}</span>
              <div className="tsr-snippet">{(r.snippet || '').slice(0, 200)}</div>
            </div>
          ))}
        </div>
      )}

      <div className="k8s-files-list-wrap card-soft">
        <div className="k8s-table-scroll">
          <table className="k8s-files-table">
            <thead>
              <tr>
                <th className={'k8s-sortable' + (sort.key === 'name' ? ' active' : '')} onClick={() => toggleSort('name')}>
                  {t('k8s.files.colName')}{' '}
                  <span className="k8s-sort-ind">
                    {sort.key === 'name' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}
                  </span>
                </th>
                <th className={'k8s-sortable' + (sort.key === 'type' ? ' active' : '')} onClick={() => toggleSort('type')}>
                  {t('k8s.files.colType')}{' '}
                  <span className="k8s-sort-ind">
                    {sort.key === 'type' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}
                  </span>
                </th>
                <th className={'k8s-sortable' + (sort.key === 'size' ? ' active' : '')} onClick={() => toggleSort('size')}>
                  {t('k8s.files.colSize')}{' '}
                  <span className="k8s-sort-ind">
                    {sort.key === 'size' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}
                  </span>
                </th>
                <th className={'k8s-sortable' + (sort.key === 'mtime' ? ' active' : '')} onClick={() => toggleSort('mtime')}>
                  {t('k8s.files.colMtime')}{' '}
                  <span className="k8s-sort-ind">
                    {sort.key === 'mtime' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
              {!target.pod ? (
                <tr>
                  <td colSpan={4} className="empty-hint">
                    {t('k8s.files.noPod')}
                  </td>
                </tr>
              ) : sorted.length === 0 ? (
                <tr>
                  <td colSpan={4} className="empty-hint">
                    {t('k8s.files.emptyDir')}
                  </td>
                </tr>
              ) : (
                sorted.map((e, i) => {
                  const isDir = e.type === 'dir';
                  return (
                    <tr
                      key={e.name + i}
                      className={selected && selected.name === e.name ? 'selected' : ''}
                      onClick={() => setSelected({ name: e.name, isDir })}
                      onDoubleClick={() => openFile(e.name, isDir)}
                    >
                      <td className="k8s-files-name">
                        <span className="k8s-files-icon">{isDir ? '📁' : '📄'}</span>
                        {e.name}
                      </td>
                      <td className="k8s-files-type">{isDir ? t('k8s.files.dir') : t('k8s.files.file')}</td>
                      <td className="k8s-files-size">{isDir ? '—' : fmtSize(e.size)}</td>
                      <td className="k8s-files-time">
                        {(e.modtime || '').replace('T', ' ').replace('Z', '').slice(0, 19)}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        style={{ display: 'none' }}
        onChange={(e) => {
          const f = e.target.files && e.target.files[0];
          if (f) upload(f);
          e.target.value = '';
        }}
      />

      {editPath && (
        <div className="modal-mask" onClick={() => { setEditPath(''); setEditFullscreen(false); }}>
          <div
            className={'modal modal-lg' + (editFullscreen ? ' modal-fullscreen' : '')}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h3>{t('k8s.files.editTitle')}{editPath.split('/').pop()}{editTruncated ? t('k8s.files.truncated') : ''}{editLang ? <span className="k8s-file-lang">{editLang}</span> : null}</h3>
              <div className="hcm-detail-head-actions">
                <button
                  className={'btn btn-sm' + (editMode ? '' : ' btn-primary')}
                  title={t('k8s.files.viewMode')}
                  onClick={() => setEditMode(false)}
                >
                  {t('k8s.files.view')}
                </button>
                <button
                  className={'btn btn-sm' + (editMode ? ' btn-primary' : '')}
                  title={t('k8s.files.editMode')}
                  onClick={() => setEditMode(true)}
                >
                  {t('k8s.files.edit')}
                </button>
                <button
                  className="btn btn-sm"
                  title={editFullscreen ? t('k8s.files.exitFullscreen') : t('k8s.files.fullscreen')}
                  onClick={() => setEditFullscreen((v) => !v)}
                >
                  {editFullscreen ? '🗗 ' + t('k8s.files.exitFullscreen') : '⛶ ' + t('k8s.files.fullscreen')}
                </button>
                <button className="btn btn-sm btn-ghost" onClick={() => { setEditPath(''); setEditMode(false); setEditFullscreen(false); }}>✕</button>
              </div>
            </div>
            <div className="modal-body">
              {editMode ? (
                <textarea className="k8s-file-edit-area" value={editContent} onChange={(e) => setEditContent(e.target.value)} />
              ) : (
                <pre className="k8s-file-view hljs" dangerouslySetInnerHTML={{ __html: highlightedHtml }} />
              )}
            </div>
            <div className="modal-footer">
              <span className="k8s-env-msg">{editMsg}</span>
              <div className="spacer" />
              <button className="btn btn-sm" onClick={saveFile}>{t('k8s.files.save')}</button>
              <button className="btn btn-sm btn-ghost" onClick={fileDownload}>{t('k8s.files.downloadFile')}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function useMemoSort(entries: K8sFileEntry[], sort: { key: SortKey; dir: 'asc' | 'desc' }): K8sFileEntry[] {
  const dirMul = sort.dir === 'desc' ? -1 : 1;
  return entries.slice().sort((a, b) => {
    const ad = a.type === 'dir' ? 0 : 1;
    const bd = b.type === 'dir' ? 0 : 1;
    if (ad !== bd) return ad - bd;
    let cmp = 0;
    if (sort.key === 'name') cmp = String(a.name).localeCompare(String(b.name), undefined, { numeric: true });
    else if (sort.key === 'type') cmp = String(a.type).localeCompare(String(b.type));
    else if (sort.key === 'size') cmp = (a.size || 0) - (b.size || 0);
    else if (sort.key === 'mtime') cmp = String(a.modtime || '').localeCompare(String(b.modtime || ''));
    return cmp * dirMul;
  });
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || '').split(',')[1] || '');
    r.onerror = () => reject(r.error || new Error('读取文件失败'));
    r.readAsDataURL(file);
  });
}
