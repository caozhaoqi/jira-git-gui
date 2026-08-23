import { useCallback, useEffect, useRef, useState } from 'react';
import { apiPost } from '../../api/client';
import { sse } from '../../api/events';
import { useAppStore } from '../../store/useAppStore';
import type { K8sSummary, K8sRecord } from '../../api/types';
import { copyText } from '../../utils/clipboard';
import { openLogViewer } from '../../utils/logviewer';
import { useK8s } from './context';

export function K8sSnapshot() {
  const { target, pushLog, openDescribe } = useK8s();
  const addToast = useAppStore((s) => s.addToast);
  const setProgress = useAppStore((s) => s.setProgress);

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
        setProgress({ visible: true, mode: 'determinate', pct, stage: d.name || '抓取中…', detail: `${d.done}/${d.total}` });
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
        appendLog('错误：' + (d.message || ''));
        addToast(d.message || 'K8s 快照出错', 'error');
      }),
      sse.on('k8s_finished', () => {
        if (!runningRef.current) return;
        setRunning(false);
        runningRef.current = false;
        setProgress({ visible: false });
      }),
    ];
    return () => offs.forEach((o) => o());
  }, [appendLog, setProgress, addToast]);

  const run = useCallback(async () => {
    if (runningRef.current) return;
    setRunning(true);
    runningRef.current = true;
    logsRef.current = [];
    setSummary(null);
    setRecords([]);
    setReportUrl(null);
    setOutDirText('');
    setProgress({ visible: true, mode: 'determinate', pct: 0, stage: '准备中…', detail: '' });
    appendLog('开始抓取 K8s 快照…');
    pushLog('开始抓取 K8s 快照…');
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
      appendLog('请求失败：' + ex.message);
      addToast(ex.message, 'error');
      setRunning(false);
      runningRef.current = false;
      setProgress({ visible: false });
    }
  }, [namespace, selector, podFilter, tail, restartThreshold, allLogs, includePrevious, outDir, kubeconfig, target.env, logLevel, pushLog, addToast, setProgress, appendLog]);

  const cancel = useCallback(async () => {
    try {
      await apiPost('/api/k8s/cancel', {});
      appendLog('已发送取消信号');
    } catch (ex: any) {
      appendLog('取消失败：' + ex.message);
    }
  }, [appendLog]);

  const selectPod = useCallback((name: string) => {
    setSelectedPod(name);
  }, []);

  return (
    <div className="k8s-snapshot">
      <div className="k8s-snapshot-cfg">
        <div className="cfg-grid">
          <label>命名空间<input className="input input-sm" value={namespace} onChange={(e) => setNamespace(e.target.value)} placeholder="（默认全部）" /></label>
          <label>选择器<input className="input input-sm" value={selector} onChange={(e) => setSelector(e.target.value)} placeholder="label=xxx" /></label>
          <label>Pod 过滤<input className="input input-sm" value={podFilter} onChange={(e) => setPodFilter(e.target.value)} /></label>
          <label>日志行数<input className="input input-sm" type="number" value={tail} onChange={(e) => setTail(Number(e.target.value))} /></label>
          <label>重启阈值<input className="input input-sm" type="number" value={restartThreshold} onChange={(e) => setRestartThreshold(Number(e.target.value))} /></label>
          <label>日志级别<input className="input input-sm" value={logLevel} onChange={(e) => setLogLevel(e.target.value)} /></label>
          <label>输出目录<input className="input input-sm" value={outDir} onChange={(e) => setOutDir(e.target.value)} placeholder="（默认临时目录）" /></label>
          <label>kubeconfig<input className="input input-sm" value={kubeconfig} onChange={(e) => setKubeconfig(e.target.value)} placeholder="（默认用环境配置）" /></label>
        </div>
        <div className="cfg-checks">
          <label className="chk"><input type="checkbox" checked={allLogs} onChange={(e) => setAllLogs(e.target.checked)} /> 全量日志</label>
          <label className="chk"><input type="checkbox" checked={includePrevious} onChange={(e) => setIncludePrevious(e.target.checked)} /> 包含上一次</label>
        </div>
        <div className="action-bar">
          <button className="btn btn-primary" onClick={run} disabled={running || !target.env} style={{ display: running ? 'none' : '' }}>抓取快照</button>
          <button className="btn btn-sm btn-ghost" onClick={cancel} style={{ display: running ? '' : 'none' }}>取消</button>
        </div>
      </div>

      {summary && (
        <div className="k8s-summary" style={{ display: 'flex' }}>
          <div className="k8s-stat"><div className="n">{summary.total ?? 0}</div><div className="l">Pod 总数</div></div>
          <div className="k8s-stat ok"><div className="n">{summary.ok ?? 0}</div><div className="l">正常</div></div>
          <div className="k8s-stat med"><div className="n">{summary.med ?? 0}</div><div className="l">警告</div></div>
          <div className="k8s-stat high"><div className="n">{summary.high ?? 0}</div><div className="l">异常</div></div>
          <div className="k8s-stat"><div className="n">{summary.logs ?? 0}</div><div className="l">日志数</div></div>
        </div>
      )}

      {reportUrl && (
        <div className="action-bar">
          <a className="btn btn-sm btn-ghost" href={reportUrl} target="_blank" rel="noreferrer">打开报告</a>
          {outDirText && (
            <button
              className="btn btn-sm btn-ghost"
              onClick={async () => {
                const ok = await copyText(outDirText);
                appendLog((ok ? '已复制输出目录：' : '输出目录：') + outDirText);
              }}
            >复制输出目录</button>
          )}
        </div>
      )}

      <div className="k8s-table-wrap">
        {records.length > 0 ? (
          <table className="k8s-table">
            <thead>
              <tr><th>名称</th><th>阶段</th><th>就绪</th><th>重启</th><th>问题</th><th>节点</th><th>HostIP</th><th>PodIP</th><th>年龄</th><th>级别</th></tr>
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
          <div className="empty-hint">{running ? '抓取中…' : '抓取后在此显示 Pod 健康概览'}</div>
        )}
      </div>

      <div className="k8s-log-wrap">
        <div className="k8s-log-title">
          <span>{selectedPod ? `已选 Pod · ${selectedPod}` : '在上方表格选择一个 Pod'}</span>
          <div className="spacer" />
          <button
            className="btn btn-sm btn-primary"
            title="在新页面打开完整日志（便于浏览 / 分析）"
            disabled={!selectedPod}
            onClick={() => {
              if (!selectedPod) { addToast('请先在上方选择一个 Pod 查看其日志。', 'warn'); return; }
              openLogViewer({ pod: selectedPod, env: target.env, namespace: target.namespace });
            }}
          >⧉ 新页面打开完整日志</button>
          <button
            className="btn btn-sm btn-ghost"
            title="查看选中 Pod 的 describe（含相关事件）"
            disabled={!selectedPod}
            onClick={() => {
              if (!selectedPod) { addToast('请先在上方选择一个 Pod 查看其日志。', 'warn'); return; }
              openDescribe('pod', selectedPod, target.namespace);
            }}
          >🔍 描述</button>
        </div>
      </div>
    </div>
  );
}
