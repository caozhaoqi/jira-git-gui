import { useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiPost } from '../api/client';
import { ProgressBar } from './ProgressBar';
import { useT } from '../i18n';

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
    try {
      const res = await apiPost<{ msg?: string; error?: string }>('/api/resume', {});
      pushLog(res.msg || res.error || t('repo.resumeDone'));
    } catch (e: any) {
      pushLog(t('repo.resumeFail', { msg: e.message }), 'error');
    }
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
        <button className="btn btn-ghost" onClick={clearLogs}>
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
        <ProgressBar />
        {progress.visible && (
          <button className="btn btn-sm btn-ghost" onClick={cancelDownload}>
            {t('repo.cancelDownload')}
          </button>
        )}
      </div>
    </div>
  );
}
