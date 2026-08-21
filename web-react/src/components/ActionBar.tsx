import { useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiPost } from '../api/client';
import { ProgressBar } from './ProgressBar';

export function ActionBar() {
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const selectedFilePath = useAppStore((s) => s.selectedFilePath);
  const checkedPaths = useAppStore((s) => s.checkedPaths);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);
  const clearLogs = useAppStore((s) => s.clearLogs);
  const qps = useAppStore((s) => s.qps);
  const setQps = useAppStore((s) => s.setQps);
  const concurrency = useAppStore((s) => s.concurrency);
  const setConcurrency = useAppStore((s) => s.setConcurrency);
  const progress = useAppStore((s) => s.progress);
  const [rate, setRate] = useState(String(qps));

  const workers = concurrency;

  async function applyRate() {
    const v = Math.max(1, Math.min(50, parseInt(rate, 10) || 6));
    setRate(String(v));
    try {
      await apiPost('/api/rate-limit', { qps: v });
      setQps(v);
      pushLog(`请求速率上限已设为 ${v} 请求/秒`);
    } catch (e: any) {
      addToast(e.message || '设置速率失败', 'error');
    }
  }

  async function cloneRepo() {
    try {
      await apiPost('/api/clone', {});
      setProgress({ visible: true, mode: 'indeterminate', stage: '克隆仓库', detail: '' });
      pushLog('开始克隆仓库…');
    } catch (e: any) {
      pushLog(`克隆请求失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    }
  }

  async function downloadAll() {
    try {
      await apiPost('/api/download/repo', { max_workers: workers });
      setProgress({ visible: true, mode: 'indeterminate', stage: '下载整个仓库', detail: '' });
      pushLog('开始递归下载整个仓库…');
    } catch (e: any) {
      pushLog(`整库下载请求失败：${e.message}`, 'error');
      addToast(e.message, 'error');
    }
  }

  async function downloadSelected() {
    const paths = checkedPaths.length ? checkedPaths : selectedFilePath ? [selectedFilePath] : [];
    if (!paths.length) {
      addToast('未勾选或选中文件', 'warn');
      return;
    }
    try {
      await apiPost('/api/download', { paths, max_workers: workers });
      setProgress({ visible: true, mode: 'indeterminate', stage: '下载选中', detail: '' });
      pushLog(`开始下载 ${paths.length} 个文件…`);
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
    <div className="actionbar">
      <div className="actionbar-group">
        <button className="btn btn-primary" onClick={cloneRepo} disabled={!selectedRepo}>
          克隆仓库 (PAT)
        </button>
        <button className="btn" onClick={downloadSelected} disabled={!selectedRepo}>
          下载选中 (Cookie)
        </button>
        <button className="btn" onClick={downloadAll} disabled={!selectedRepo}>
          下载整个仓库
        </button>
      </div>
      <div className="actionbar-divider" />
      <div className="actionbar-group">
        <button className="btn btn-ghost" onClick={clearResume}>
          清空断点
        </button>
        <button className="btn btn-ghost" onClick={clearLogs}>
          清空日志
        </button>
      </div>
      <div className="actionbar-divider" />
      <div className="actionbar-group actionbar-fields">
        <label className="field-inline">
          并发
          <input
            type="number"
            min={1}
            max={16}
            className="spin"
            value={concurrency}
            onChange={(e) => setConcurrency(parseInt(e.target.value, 10) || 4)}
          />
        </label>
        <label className="field-inline" title="对 Jira 服务器的稳态请求速率上限">
          速率
          <input
            type="number"
            min={1}
            max={50}
            className="spin"
            value={rate}
            onChange={(e) => setRate(e.target.value)}
            onBlur={applyRate}
          />
        </label>
      </div>
      <div className="actionbar-spacer" />
      <div className="actionbar-group">
        <ProgressBar />
        {progress.visible && (
          <button className="btn btn-sm btn-ghost" onClick={cancelDownload}>
            取消
          </button>
        )}
      </div>
    </div>
  );
}
