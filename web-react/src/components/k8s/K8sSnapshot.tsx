import { useCallback, useEffect, useRef, useState } from 'react';
import { apiPost } from '../../api/client';
import { sse } from '../../api/events';
import { useAppStore } from '../../store/useAppStore';
import { useT } from '../../i18n';
import type { K8sSummary, K8sRecord } from '../../api/types';
import { copyText } from '../../utils/clipboard';
import { openLogViewer } from '../../utils/logviewer';
import { useK8s } from './context';

export function K8sSnapshot() {
  const { target, pushLog, openDescribe } = useK8s();
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);
  const { t } = useT();

  const [namespace, setNamespace] = useState('');
  const [selector, setSelector] = useState('');
  const [podFilter, setPodFilter] = useState('');
  const [tail, setTail] = useState(200);
  const [restartThreshold, setRestartThreshold] = useState(5);
  const [allLogs, setAllLogs] = useState(false);
  const [includePrevious, setIncludePrevious] = useState(false);
  const [outDir, setOutDir] = useState('');
  const [kubeconfig, setKubeconfig] = useState('');
  const [logLevel, setLogLevel] = useState('INFO');

  const [running, setRunning] = useState(false);
  const [summary, setSummary] = useState<K8sSummary | null>(null);
  const [records, setRecords] = useState<K8sRecord[]>([]);
  const [reportUrl, setReportUrl] = useState<string | null>(null);
  const [outDirText, setOutDirText] = useState('');
  const [selectedPod, setSelectedPod] = useState('');

  const runningRef = useRef(false);
  const logsRef = useRef<string[]>([]);
  const appendLog = useCallback((msg: string) => {
    logsRef.current.push(msg);
    if (logsRef.current.length > 2000) logsRef.current = logsRef.current.slice(-2000);
  }, []);

  useEffect(() => {
    const offs = [
      sse.on('k8s_log', (d: any) => {
        if (!runningRef.current) return;
        appendLog(d.msg);
      }),
      sse.on('k8s_progress', (d: any) => {
        if (!runningRef.current) return;
        const pct = typeof d.pct === 'number' ? d.pct : 0;
        setProgress({ visible: true, mode: 'determinate', pct, stage: d.name || t('k8s.snapshot.running'), detail: `${d.done}/${d.total}` });
      }),
      sse.on('k8s_done', (d: any) => {
        if (!runningRef.current) return;
        setRunning(false);
        runningRef.current = false;
        setProgress({ visible: false });
        if (d.summary) setSummary(d.summary);
        if (d.records) setRecords(d.records);
        if (d.out_dir) setOutDirText(d.out_dir);
        if (d.report) setReportUrl('/api/k8s/report');
      }),
      sse.on('k8s_error', (d: any) => {
        if (!runningRef.current) return;
        appendLog(t('k8s.snapshot.error') + (d.message || ''));
        addToast(d.message || t('k8s.snapshot.errToast'), 'error');
      }),
      sse.on('k8s_finished', () => {
        if (!runningRef.current) return;
        setRunning(false);
        runningRef.current = false;
        setProgress({ visible: false });
      }),
    ];
    return () => offs.forEach((o) => o());
  }, [appendLog, setProgress, addToast, t]);

  const run = useCallback(async () => {
    if (runningRef.current) return;
    setRunning(true);
    runningRef.current = true;
    logsRef.current = [];
    setSummary(null);
    setRecords([]);
    setReportUrl(null);
    setOutDirText('');
    setProgress({ visible: true, mode: 'determinate', pct: 0, stage: t('k8s.snapshot.preparing'), detail: '' });
    appendLog(t('k8s.snapshot.start'));
    pushLog(t('k8s.snapshot.start'));
    const cfg = {
      namespace: namespace.trim(),
      selector: selector.trim(),
      pod_filter: podFilter.trim(),
      tail: Number(tail) || 200,
      restart_threshold: Number(restartThreshold) || 5,
      all_logs: allLogs,
      include_previous: includePrevious,
      out_dir: outDir.trim(),
      kubeconfig: kubeconfig.trim(),
      env: target.env,
      log_level: logLevel,
    };
    try {
      await apiPost('/api/k8s/snapshot', cfg);
    } catch (ex: any) {
      appendLog(t('k8s.snapshot.reqFail') + ex.message);
      addToast(ex.message, 'error');
      setRunning(false);
      runningRef.current = false;
      setProgress({ visible: false });
    }
  }, [namespace, selector, podFilter, tail, restartThreshold, allLogs, includePrevious, outDir, kubeconfig, target.env, logLevel, pushLog, addToast, setProgress, appendLog, t]);

  const cancel = useCallback(async () => {
    try {
      await apiPost('/api/k8s/cancel', {});
      appendLog(t('k8s.snapshot.cancelSent'));
    } catch (ex: any) {
      appendLog(t('k8s.snapshot.cancelFail') + ex.message);
    }
  }, [appendLog, t]);

  const selectPod = useCallback((name: string) => {
    setSelectedPod(name);
  }, []);

  return (
    <div className="k8s-snapshot">
      <div className="k8s-snapshot-cfg">
        <div className="cfg-grid">
          <label>{t('k8s.snapshot.namespace')}<input className="input input-sm" value={namespace} onChange={(e) => setNamespace(e.target.value)} placeholder={t('k8s.snapshot.namespacePh')} /></label>
          <label>{t('k8s.snapshot.selector')}<input className="input input-sm" value={selector} onChange={(e) => setSelector(e.target.value)} placeholder="label=xxx" /></label>
          <label>{t('k8s.snapshot.podFilter')}<input className="input input-sm" value={podFilter} onChange={(e) => setPodFilter(e.target.value)} /></label>
          <label>{t('k8s.snapshot.tail')}<input className="input input-sm" type="number" value={tail} onChange={(e) => setTail(Number(e.target.value))} /></label>
          <label>{t('k8s.snapshot.restartThreshold')}<input className="input input-sm" type="number" value={restartThreshold} onChange={(e) => setRestartThreshold(Number(e.target.value))} /></label>
          <label>{t('k8s.snapshot.logLevel')}<input className="input input-sm" value={logLevel} onChange={(e) => setLogLevel(e.target.value)} /></label>
          <label>{t('k8s.snapshot.outDir')}<input className="input input-sm" value={outDir} onChange={(e) => setOutDir(e.target.value)} placeholder={t('k8s.snapshot.outDirPh')} /></label>
          <label>{t('k8s.snapshot.kubeconfig')}<input className="input input-sm" value={kubeconfig} onChange={(e) => setKubeconfig(e.target.value)} placeholder={t('k8s.snapshot.kubeconfigPh')} /></label>
        </div>
        <div className="cfg-checks">
          <label className="chk"><input type="checkbox" checked={allLogs} onChange={(e) => setAllLogs(e.target.checked)} /> {t('k8s.snapshot.allLogs')}</label>
          <label className="chk"><input type="checkbox" checked={includePrevious} onChange={(e) => setIncludePrevious(e.target.checked)} /> {t('k8s.snapshot.includePrevious')}</label>
        </div>
        <div className="action-bar">
          <button className="btn btn-primary" onClick={run} disabled={running || !target.env} style={{ display: running ? 'none' : '' }}>{t('k8s.snapshot.run')}</button>
          <button className="btn btn-sm btn-ghost" onClick={cancel} style={{ display: running ? '' : 'none' }}>{t('k8s.snapshot.cancel')}</button>
        </div>
      </div>

      {summary && (
        <div className="k8s-summary" style={{ display: 'flex' }}>
          <div className="k8s-stat"><div className="n">{summary.total ?? 0}</div><div className="l">{t('k8s.snapshot.statTotal')}</div></div>
          <div className="k8s-stat ok"><div className="n">{summary.ok ?? 0}</div><div className="l">{t('k8s.snapshot.statOk')}</div></div>
          <div className="k8s-stat med"><div className="n">{summary.med ?? 0}</div><div className="l">{t('k8s.snapshot.statWarn')}</div></div>
          <div className="k8s-stat high"><div className="n">{summary.high ?? 0}</div><div className="l">{t('k8s.snapshot.statErr')}</div></div>
          <div className="k8s-stat"><div className="n">{summary.logs ?? 0}</div><div className="l">{t('k8s.snapshot.statLogs')}</div></div>
        </div>
      )}

      {reportUrl && (
        <div className="action-bar">
          <a className="btn btn-sm btn-ghost" href={reportUrl} target="_blank" rel="noreferrer">{t('k8s.snapshot.openReport')}</a>
          {outDirText && (
            <button
              className="btn btn-sm btn-ghost"
              onClick={async () => {
                const ok = await copyText(outDirText);
                appendLog((ok ? t('k8s.snapshot.copiedOutDir') : t('k8s.snapshot.outDir')) + outDirText);
              }}
            >{t('k8s.snapshot.copyOutDir')}</button>
          )}
        </div>
      )}

      <div className="k8s-table-wrap">
        {records.length > 0 ? (
          <table className="k8s-table">
            <thead>
              <tr><th>{t('k8s.snapshot.colName')}</th><th>{t('k8s.snapshot.colPhase')}</th><th>{t('k8s.snapshot.colReady')}</th><th>{t('k8s.snapshot.colRestarts')}</th><th>{t('k8s.snapshot.colProblem')}</th><th>{t('k8s.snapshot.colNode')}</th><th>HostIP</th><th>PodIP</th><th>{t('k8s.snapshot.colAge')}</th><th>{t('k8s.snapshot.colSev')}</th></tr>
            </thead>
            <tbody>
              {records.map((r, i) => {
                const problemText = (r.problems || []).map((p) => p[1]).join('; ') || r.reason || '—';
                return (
                  <tr
                    key={r.name + i}
                    className={'k8s-row sev-' + r.sev + (selectedPod === r.name ? ' selected' : '')}
                    onClick={() => selectPod(r.name)}
                  >
                    <td className="k8s-name" title={r.name}>{r.name}</td>
                    <td>{r.phase || '—'}</td>
                    <td>{`${r.ready ?? 0}/${r.total ?? 0}`}</td>
                    <td className={r.restarts && r.restarts > 0 ? 'k8s-restarts' : ''}>{r.restarts ?? 0}</td>
                    <td>{problemText}</td>
                    <td>{r.node || '—'}</td>
                    <td>{r.host_ip || '—'}</td>
                    <td>{r.pod_ip || '—'}</td>
                    <td>{r.age || '—'}</td>
                    <td><span className={`k8s-badge sev-${r.sev}`}>{r.sev}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="empty-hint">{running ? t('k8s.snapshot.running') : t('k8s.snapshot.noData')}</div>
        )}
      </div>

      <div className="k8s-log-wrap">
        <div className="k8s-log-title">
          <span>{selectedPod ? `${t('k8s.snapshot.selectedPod')} · ${selectedPod}` : t('k8s.snapshot.pickPodHint')}</span>
          <div className="spacer" />
          <button
            className="btn btn-sm btn-primary"
            title={t('k8s.snapshot.openFullLog')}
            disabled={!selectedPod}
            onClick={() => {
              if (!selectedPod) { addToast(t('k8s.snapshot.pickPod'), 'warn'); return; }
              openLogViewer({ pod: selectedPod, env: target.env, namespace: target.namespace });
            }}
          >⧉ {t('k8s.snapshot.openFullLog')}</button>
          <button
            className="btn btn-sm btn-ghost"
            title={t('k8s.snapshot.describe')}
            disabled={!selectedPod}
            onClick={() => {
              if (!selectedPod) { addToast(t('k8s.snapshot.pickPod'), 'warn'); return; }
              openDescribe('pod', selectedPod, target.namespace);
            }}
          >🔍 {t('k8s.snapshot.describe')}</button>
        </div>
      </div>
    </div>
  );
}
