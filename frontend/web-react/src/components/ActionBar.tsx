import { useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiPost } from '../api/client';
import { useT } from '../i18n';
import { requestConfirm } from '../utils/confirmStore';

export function ActionBar() {
  const selectedRepo = useAppStore((s) => s.selectedRepo);
  const selectedFilePath = useAppStore((s) => s.selectedFilePath);
  const checkedPaths = useAppStore((s) => s.checkedPaths);
  const clearCheckedPaths = useAppStore((s) => s.clearCheckedPaths);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);
  const clearLogs = useAppStore((s) => s.clearLogs);
  const qps = useAppStore((s) => s.qps);
  const setQps = useAppStore((s) => s.setQps);
  const concurrency = useAppStore((s) => s.concurrency);
  const setConcurrency = useAppStore((s) => s.setConcurrency);
  const progress = useAppStore((s) => s.progress);
  const { t } = useT();
  const [rate, setRate] = useState(String(qps));

  const workers = concurrency;

  async function applyRate() {
    const v = Math.max(1, Math.min(50, parseInt(rate, 10) || 6));
    setRate(String(v));
    try {
      await apiPost('/api/rate-limit', { qps: v });
      setQps(v);
      pushLog(t('repo.rateSet', { v }));
    } catch (e: any) {
      addToast(e.message || t('repo.rateSet', { v }), 'error');
    }
  }

  async function cloneRepo() {
    try {
      await apiPost('/api/clone', {});
      setProgress({ visible: true, mode: 'indeterminate', stage: t('repo.clone'), detail: '' });
      pushLog(t('repo.cloneStart'));
    } catch (e: any) {
      pushLog(t('repo.cloneFail', { msg: e.message }), 'error');
      addToast(e.message, 'error');
    }
  }

  async function downloadAll() {
    try {
      await apiPost('/api/download/repo', { max_workers: workers });
      setProgress({ visible: true, mode: 'indeterminate', stage: t('repo.downloadAll'), detail: '' });
      pushLog(t('repo.downloadAllStart'));
    } catch (e: any) {
      pushLog(t('repo.downloadAllFail', { msg: e.message }), 'error');
      addToast(e.message, 'error');
    }
  }

  async function downloadSelected() {
    const paths = checkedPaths.length ? checkedPaths : selectedFilePath ? [selectedFilePath] : [];
    if (!paths.length) {
      addToast(t('repo.noFileSelected'), 'warn');
      return;
    }
    try {
      await apiPost('/api/download', { paths, max_workers: workers });
      setProgress({ visible: true, mode: 'indeterminate', stage: t('repo.downloadSelected'), detail: '' });
      pushLog(t('repo.downloadSelectedStart', { n: paths.length }));
      // 已下载的勾选要清掉：否则这些「看不见的勾选」会一直留给差异页的
      // 「合并勾选项」使用，用户无法察觉、也无法在差异列表里取消。
      clearCheckedPaths();
    } catch (e: any) {
      pushLog(t('repo.downloadFail', { msg: e.message }), 'error');
      addToast(e.message, 'error');
    }
  }

  async function cancelDownload() {
    await apiPost('/api/download/cancel', {});
    pushLog(t('repo.cancelRequested'));
  }

  async function clearResume() {
    // 断点续传清单一旦清空只能重下，属不可撤销操作：先确认再执行。
    if (!(await requestConfirm({ message: t('repo.clearResumeConfirm'), danger: true }))) return;
    try {
      const res = await apiPost<{ msg?: string; error?: string }>('/api/resume', {});
      const msg = res.msg || res.error || t('repo.resumeDone');
      pushLog(msg);
      // 结果只在「系统 → 日志」页可见，这里补一条即时反馈
      addToast(res.error ? msg : t('repo.resumeCleared'), res.error ? 'error' : 'success');
    } catch (e: any) {
      pushLog(t('repo.resumeFail', { msg: e.message }), 'error');
      addToast(t('repo.resumeFail', { msg: e.message }), 'error');
    }
  }

  async function handleClearLogs() {
    if (!(await requestConfirm({ message: t('repo.clearLogsConfirm'), danger: true }))) return;
    clearLogs();
    addToast(t('repo.logsCleared'), 'success');
  }

  return (
    <div className="actionbar">
      <div className="actionbar-group">
        <button className="btn btn-primary" onClick={cloneRepo} disabled={!selectedRepo}>
          {t('repo.clone')}
        </button>
        <button className="btn" onClick={downloadSelected} disabled={!selectedRepo}>
          {t('repo.downloadSelected')}
        </button>
        <button className="btn" onClick={downloadAll} disabled={!selectedRepo}>
          {t('repo.downloadAll')}
        </button>
      </div>
      <div className="actionbar-divider" />
      <div className="actionbar-group">
        <button className="btn btn-ghost" onClick={clearResume}>
          {t('repo.clearResume')}
        </button>
        <button className="btn btn-ghost" onClick={handleClearLogs}>
          {t('repo.clearLogs')}
        </button>
      </div>
      <div className="actionbar-divider" />
      <div className="actionbar-group actionbar-fields">
        <label className="field-inline">
          {t('repo.concurrency')}
          <input
            type="number"
            min={1}
            max={16}
            className="spin"
            value={concurrency}
            onChange={(e) => setConcurrency(parseInt(e.target.value, 10) || 4)}
          />
        </label>
        <label className="field-inline" title={t('repo.rateTitle')}>
          {t('repo.rate')}
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
        {progress.visible && (
          <button className="btn btn-sm btn-ghost" onClick={cancelDownload}>
            {t('repo.cancelDownload')}
          </button>
        )}
      </div>
    </div>
  );
}
