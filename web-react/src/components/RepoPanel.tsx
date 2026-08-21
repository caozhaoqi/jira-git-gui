import { useCallback, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiPost } from '../api/client';
import { RepoList } from './RepoList';
import { FileTree } from './FileTree';
import { Preview } from './Preview';

const DEFAULT_WORKERS = 4;

export function RepoPanel() {
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const selectedFilePath = useAppStore((s) => s.selectedFilePath);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);

  const [leftWidth, setLeftWidth] = useState(260);
  const [rightWidth, setRightWidth] = useState(480);
  const dragging = useRef<null | 'left' | 'right'>(null);

  const onMouseDown = (side: 'left' | 'right') => (e: ReactMouseEvent) => {
    e.preventDefault();
    dragging.current = side;
    const startX = e.clientX;
    const startLeft = leftWidth;
    const startRight = rightWidth;
    const onMove = (ev: MouseEvent) => {
      if (dragging.current === 'left') {
        setLeftWidth(Math.max(180, Math.min(520, startLeft + ev.clientX - startX)));
      } else if (dragging.current === 'right') {
        setRightWidth(Math.max(300, Math.min(900, startRight - (ev.clientX - startX))));
      }
    };
    const onUp = () => {
      dragging.current = null;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  const startProgress = useCallback(
    (stage: string) => setProgress({ visible: true, mode: 'indeterminate', stage, detail: '' }),
    [setProgress]
  );

  async function cloneRepo() {
    try {
      await apiPost('/api/clone', {});
      startProgress('克隆仓库');
      pushLog('开始克隆仓库…');
    } catch (e: any) {
      pushLog(`克隆请求失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    }
  }

  async function downloadAll() {
    try {
      await apiPost('/api/download/repo', { max_workers: DEFAULT_WORKERS });
      startProgress('下载整个仓库');
      pushLog('开始递归下载整个仓库…');
    } catch (e: any) {
      pushLog(`整库下载请求失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    }
  }

  async function downloadCurrent() {
    if (!selectedFilePath) {
      addToast('未选中文件', 'warn');
      return;
    }
    try {
      await apiPost('/api/download', { paths: [selectedFilePath], max_workers: DEFAULT_WORKERS });
      startProgress('下载选中文件');
      pushLog(`开始下载 1 个文件…`);
    } catch (e: any) {
      pushLog(`下载请求失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    }
  }

  async function cancelDownload() {
    await apiPost('/api/download/cancel', {});
    pushLog('已请求取消下载。');
  }

  async function clearResume() {
    try {
      const res = await apiPost<{ msg?: string; error?: string }>('/api/resume', {});
      pushLog(res.msg || res.error || '操作完成');
    } catch (e: any) {
      pushLog(`清空断点失败：${e.message}`, 'error');
    }
  }

  return (
    <div className="repo-panel">
      <div className="action-bar">
        <button className="btn btn-sm" onClick={cloneRepo} disabled={!selectedRepo}>
          📥 克隆仓库
        </button>
        <button className="btn btn-sm" onClick={downloadAll} disabled={!selectedRepo}>
          ⬇ 下载整库
        </button>
        <button className="btn btn-sm" onClick={downloadCurrent} disabled={!selectedFilePath}>
          ⬇ 下载当前文件
        </button>
        <button className="btn btn-sm btn-ghost" onClick={cancelDownload}>
          ✕ 取消
        </button>
        <button className="btn btn-sm btn-ghost" onClick={clearResume}>
          ♻ 清空断点
        </button>
      </div>
      <div className="three-column">
        <div className="col col-left" style={{ width: leftWidth }}>
          <RepoList />
        </div>
        <div
          className="resizer"
          onMouseDown={onMouseDown('left')}
          title="拖动调整列宽"
        />
        <div className="col col-mid">
          <FileTree />
        </div>
        <div
          className="resizer"
          onMouseDown={onMouseDown('right')}
          title="拖动调整列宽"
        />
        <div className="col col-right" style={{ width: rightWidth }}>
          <Preview />
        </div>
      </div>
    </div>
  );
}
