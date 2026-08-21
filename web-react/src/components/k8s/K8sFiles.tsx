import { useCallback, useEffect, useRef, useState } from 'react';
import { api, apiPost } from '../../api/client';
import type {
  K8sFileEntry, K8sFileListResp, K8sFileReadResp, K8sFileWriteResp, K8sFileSearchResp, K8sFileSearchHit, K8sPodsResp,
} from '../../api/types';
import { useK8s } from './context';
import { k8sPathJoin, k8sPathParent, fmtSize } from '../../utils/format';

type SortKey = 'name' | 'type' | 'size' | 'mtime';
interface SelectedFile { name: string; isDir: boolean; }

export function K8sFiles() {
  const { target, setTarget, addToast } = useK8s();

  const [path, setPath] = useState('/');
  const [entries, setEntries] = useState<K8sFileEntry[]>([]);
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>({ key: 'name', dir: 'asc' });
  const [selected, setSelected] = useState<SelectedFile | null>(null);
  const [editPath, setEditPath] = useState('');
  const [editContent, setEditContent] = useState('');
  const [editMsg, setEditMsg] = useState('');
  const [editTruncated, setEditTruncated] = useState(false);
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
      if (!d.ok) { setEntries([]); addToast('加载失败：' + (d.error || ''), 'error'); return; }
      setEntries(d.entries || []);
    } catch (ex: any) {
      setEntries([]);
      addToast('加载失败：' + ex.message, 'error');
    }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast]);

  useEffect(() => {
    if (target.pod) listFiles(path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.env, target.pod]);

  const sorted = useMemoSort(entries, sort);

  const openFile = useCallback(async (name: string, isDir: boolean) => {
    const full = k8sPathJoin(path, name);
    if (isDir) { listFiles(full); return; }
    try {
      const d = await apiPost<K8sFileReadResp>('/api/k8s/file/read', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: full, max_bytes: 200000,
      });
      if (!d.ok) { addToast('读取失败：' + (d.error || ''), 'error'); return; }
      if (d.is_binary) { addToast('这是二进制文件，不支持在线编辑。', 'info'); return; }
      setEditPath(full);
      setEditContent(d.content || '');
      setEditTruncated(!!d.truncated);
      setEditMsg('');
    } catch (ex: any) {
      addToast('读取失败：' + ex.message, 'error');
    }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, listFiles]);

  const saveFile = useCallback(async () => {
    if (!editPath) return;
    setEditMsg('保存中…');
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/write', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: editPath, content: editContent,
      });
      if (!d.ok) { setEditMsg('失败：' + (d.error || '')); return; }
      setEditMsg('✅ 已保存');
      listFiles(path);
    } catch (ex: any) {
      setEditMsg('失败：' + ex.message);
    }
  }, [editPath, editContent, target.env, target.pod, target.container, target.namespace, listFiles, path]);

  const fileDownload = useCallback(() => {
    const blob = new Blob([editContent], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (editPath.split('/').pop()) || 'file.txt';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
  }, [editContent, editPath]);

  const mkdir = useCallback(async () => {
    const name = window.prompt('新建文件夹名称：');
    if (!name) return;
    const full = k8sPathJoin(path, name.trim());
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/mkdir', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: full,
      });
      if (!d.ok) { addToast('新建失败：' + (d.error || ''), 'error'); return; }
      listFiles(path);
    } catch (ex: any) { addToast('新建失败：' + ex.message, 'error'); }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, listFiles]);

  const upload = useCallback(async (file: File) => {
    const data = await fileToBase64(file);
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/upload', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: path, data,
      });
      if (!d.ok) { addToast('上传失败：' + (d.error || ''), 'error'); return; }
      listFiles(path);
    } catch (ex: any) { addToast('上传失败：' + ex.message, 'error'); }
  }, [path, target.env, target.pod, target.container, target.namespace, addToast, listFiles]);

  const del = useCallback(async () => {
    if (!selected) { addToast('请先单击选中要删除的文件 / 目录', 'warn'); return; }
    if (!window.confirm(`确认删除 ${selected.isDir ? '目录' : '文件'}「${selected.name}」？此操作不可恢复。`)) return;
    const full = k8sPathJoin(path, selected.name);
    try {
      const d = await apiPost<K8sFileWriteResp>('/api/k8s/file/delete', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, path: full, is_dir: selected.isDir,
      });
      if (!d.ok) { addToast('删除失败：' + (d.error || ''), 'error'); return; }
      setSelected(null);
      listFiles(path);
    } catch (ex: any) { addToast('删除失败：' + ex.message, 'error'); }
  }, [selected, path, target.env, target.pod, target.container, target.namespace, addToast, listFiles]);

  const doSearch = useCallback(async () => {
    const q = searchQ.trim();
    if (!q) { setSearchStatus('请输入搜索关键词'); return; }
    if (!target.pod) { setSearchStatus('请先选择 Pod'); return; }
    setSearchStatus('搜索中…');
    setSearchResults([]);
    try {
      const d = await apiPost<K8sFileSearchResp>('/api/k8s/file/search', {
        env: target.env, pod: target.pod, container: target.container, namespace: target.namespace, q, path,
      });
      if (!d.ok) { setSearchStatus('搜索失败：' + (d.error || '')); return; }
      setSearchResults(d.results || []);
      setSearchStatus(`共 ${d.total ?? 0} 处匹配`);
    } catch (ex: any) { setSearchStatus('搜索失败：' + ex.message); }
  }, [searchQ, target.env, target.pod, target.container, target.namespace, path]);

  const toggleSort = (key: SortKey) => {
    setSort((s) => ({
      key,
      dir: s.key === key ? (s.dir === 'asc' ? 'desc' : 'asc') : 'asc',
    }));
  };

  return (
    <div className="k8s-files">
      <div className="k8s-shell-connbar" style={{ display: 'flex' }}>
        <select className="sel" value={target.pod} onChange={(e) => setTarget({ pod: e.target.value })}>
          <option value="">— 选择 Pod —</option>
          {podList.map((p) => <option key={p.name} value={p.name}>{p.name} · {p.phase || ''}</option>)}
        </select>
        <input className="input input-sm" value={target.namespace} onChange={(e) => setTarget({ namespace: e.target.value })} placeholder="命名空间" />
        <button className="btn btn-sm btn-ghost" onClick={mkdir}>新建目录</button>
        <button className="btn btn-sm btn-ghost" onClick={() => fileInputRef.current?.click()}>上传</button>
        <button className="btn btn-sm btn-ghost" onClick={del}>删除</button>
        <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={(e) => { const f = e.target.files && e.target.files[0]; if (f) upload(f); e.target.value = ''; }} />
      </div>

      <div className="k8s-files-breadcrumb">
        <span className="k8s-files-crumb" onClick={() => listFiles('/')}>📁 /</span>
        {path.split('/').filter(Boolean).map((p, i, arr) => {
          const seg = '/' + arr.slice(0, i + 1).join('/');
          return (<span key={i}><span className="k8s-files-sep">/</span><span className="k8s-files-crumb" onClick={() => listFiles(seg)}>{p}</span></span>);
        })}
        <button className="btn btn-sm btn-ghost" onClick={() => listFiles(k8sPathParent(path))}>上级</button>
        <button className="btn btn-sm btn-ghost" onClick={() => listFiles(path)}>刷新</button>
      </div>

      <div className="k8s-files-search">
        <input className="input input-sm" value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="容器内文件内容搜索（grep）" onKeyDown={(e) => e.key === 'Enter' && doSearch()} />
        <button className="btn btn-sm" onClick={doSearch}>搜索</button>
        {searchStatus && <span className="k8s-files-search-status">{searchStatus}</span>}
        {searchResults.length > 0 && (
          <div className="k8s-files-search-results">
            {searchResults.map((r, i) => (
              <div key={i} className="tree-search-item" onClick={async () => {
                const dir = r.path.includes('/') ? r.path.slice(0, r.path.lastIndexOf('/')) : path;
                await listFiles(dir);
                const name = (r.path.split('/').pop()) || r.path;
                openFile(name, false);
              }}>
                <span className="tsr-icon">📄</span>
                <span className="tsr-path">{r.path}</span>
                <span className="tsr-line">:{r.line}</span>
                <div className="tsr-snippet">{(r.snippet || '').slice(0, 200)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="k8s-table-wrap">
        <table className="k8s-files-table">
          <thead>
            <tr>
              <th className={'k8s-sortable' + (sort.key === 'name' ? ' active' : '')} data-sort="name" onClick={() => toggleSort('name')}>名称 {sort.key === 'name' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}</th>
              <th className={'k8s-sortable' + (sort.key === 'type' ? ' active' : '')} data-sort="type" onClick={() => toggleSort('type')}>类型 {sort.key === 'type' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}</th>
              <th className={'k8s-sortable' + (sort.key === 'size' ? ' active' : '')} data-sort="size" onClick={() => toggleSort('size')}>大小 {sort.key === 'size' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}</th>
              <th className={'k8s-sortable' + (sort.key === 'mtime' ? ' active' : '')} data-sort="mtime" onClick={() => toggleSort('mtime')}>修改时间 {sort.key === 'mtime' ? (sort.dir === 'desc' ? '▼' : '▲') : ''}</th>
            </tr>
          </thead>
          <tbody>
            {!target.pod ? <tr><td colSpan={4} className="empty-hint">请先在上方选择 Pod / 容器</td></tr> :
              sorted.length === 0 ? <tr><td colSpan={4} className="empty-hint">空目录</td></tr> :
              sorted.map((e, i) => {
                const isDir = e.type === 'dir';
                return (
                  <tr
                    key={e.name + i}
                    className={'k8s-files-row' + (selected && selected.name === e.name ? ' selected' : '')}
                    onClick={() => setSelected({ name: e.name, isDir })}
                    onDoubleClick={() => openFile(e.name, isDir)}
                  >
                    <td className="k8s-files-name"><span className="k8s-files-icon">{isDir ? '📁' : '📄'}</span>{e.name}</td>
                    <td className="k8s-files-type">{isDir ? '目录' : '文件'}</td>
                    <td className="k8s-files-size">{isDir ? '—' : fmtSize(e.size)}</td>
                    <td className="k8s-files-time">{(e.modtime || '').replace('T', ' ').replace('Z', '').slice(0, 19)}</td>
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>

      {editPath && (
        <div className="modal" style={{ display: 'flex' }}>
          <div className="modal-box modal-lg">
            <div className="modal-head">
              <h3>编辑 · {editPath.split('/').pop()}{editTruncated ? '（已截断）' : ''}</h3>
              <button className="btn btn-sm btn-ghost" onClick={() => setEditPath('')}>✕</button>
            </div>
            <textarea className="k8s-file-edit-area" value={editContent} onChange={(e) => setEditContent(e.target.value)} />
            <div className="modal-foot">
              <span className="k8s-env-msg">{editMsg}</span>
              <div className="spacer" />
              <button className="btn btn-sm" onClick={saveFile}>保存</button>
              <button className="btn btn-sm btn-ghost" onClick={fileDownload}>下载</button>
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
