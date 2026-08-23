import { useEffect, useState } from 'react';
import { sse } from './api/events';
import { apiGet } from './api/client';
import { useAppStore } from './store/useAppStore';
import type {
  StatusResp,
  ReposResp,
  SSELog,
  SSEProgress,
  SSECloneDone,
  SSEDownloadDone,
  SSENetworkWarning,
} from './api/types';
import { normalizeStatus } from './api/types';
import { TopBar } from './components/TopBar';
import { Tabs } from './components/Tabs';
import { ActionBar } from './components/ActionBar';
import { RepoPanel } from './components/RepoPanel';
import { CommitsPanel } from './components/CommitsPanel';
import { DiffPanel } from './components/DiffPanel';
import { K8sPanel } from './components/k8s/K8sPanel';
import { CfPanel } from './components/CfPanel';
import { ClashPanel } from './components/ClashPanel';
import { ToastStack } from './components/Toast';
import { LogPanel } from './components/LogPanel';
import { ConnectModal } from './components/ConnectModal';

const ACTIONBAR_TABS = new Set(['repo', 'commits', 'diff']);

export default function App() {
  const setStatus = useAppStore((s) => s.setStatus);
  const setRepos = useAppStore((s) => s.setRepos);
  const pushLog = useAppStore((s) => s.pushLog);
  const setProgress = useAppStore((s) => s.setProgress);
  const addToast = useAppStore((s) => s.addToast);
  const setNetworkWarning = useAppStore((s) => s.setNetworkWarning);
  const networkWarning = useAppStore((s) => s.networkWarning);
  const activeTab = useAppStore((s) => s.activeTab);
  const [connectOpen, setConnectOpen] = useState(false);

  useEffect(() => {
    apiGet<StatusResp>('/api/status')
      .then((s) => {
        const ns = normalizeStatus(s);
        setStatus(ns);
        if (typeof ns.qps === 'number') useAppStore.getState().setQps(ns.qps);
      })
      .catch(() => {
        /* 后端未就绪时静默，SSE 会重连 */
      });

    apiGet<ReposResp>('/api/repos')
      .then((r) => {
        if (r.error) {
          pushLog(`发现仓库错误：${r.error}`, 'warning');
        } else {
          setRepos(r.repos || []);
          pushLog(`【发现仓库】返回 ${(r.repos || []).length} 个`);
        }
      })
      .catch((e) => pushLog(`发现仓库异常：${e.message}`, 'error'));

    sse.connect();
    const offs = [
      sse.on('log', (d: SSELog) => pushLog(d.msg, d.level || 'info')),
      sse.on('progress', (d: SSEProgress) => {
        setProgress({
          visible: true,
          mode: d.total > 0 ? 'determinate' : 'indeterminate',
          pct: d.total > 0 ? Math.round((d.done / d.total) * 100) : 0,
          stage: '处理中',
          detail: d.total > 0 ? `${d.done}/${d.total} (${d.pct ?? 0}%)` : `已处理 ${d.done}…`,
        });
      }),
      sse.on('clone_done', (d: SSECloneDone) => {
        if (d.ok) {
          pushLog(`克隆结果：${d.msg}`);
          if (d.path) pushLog(`本地路径：${d.path}`);
        }
        setProgress({ visible: false });
      }),
      sse.on('download_done', (d: SSEDownloadDone) => {
        pushLog(
          `下载完成：成功 ${d.ok_count}（跳过 ${d.skipped}），失败 ${d.fail_count}。`
        );
        if (d.dest) pushLog(`已保存到：${d.dest}`);
        setProgress({ visible: false });
      }),
      sse.on('network_warning', (d: SSENetworkWarning) => {
        const msg = d.message || '网络中断';
        pushLog(msg, 'error');
        addToast(msg, 'warn');
        setNetworkWarning(msg);
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [setStatus, setRepos, pushLog, setProgress, addToast, setNetworkWarning]);

  return (
    <div className="app-shell">
      <TopBar onOpenConnect={() => setConnectOpen(true)} />
      <div className="app-body">
        <Tabs />
        <main className="workspace">
          {ACTIONBAR_TABS.has(activeTab) && <ActionBar />}
          {networkWarning && (
            <div className="network-warning" onClick={() => setNetworkWarning(null)}>
              ⚠ {networkWarning}
            </div>
          )}
          <div className="workspace-body">
            {activeTab === 'repo' && <RepoPanel />}
            {activeTab === 'commits' && <CommitsPanel />}
            {activeTab === 'diff' && <DiffPanel />}
            {activeTab === 'logs' && <LogPanel />}
            {activeTab === 'k8s' && <K8sPanel />}
            {activeTab === 'cf' && <CfPanel />}
            {activeTab === 'clash' && <ClashPanel />}
          </div>
        </main>
      </div>
      <ToastStack />
      {connectOpen && <ConnectModal onClose={() => setConnectOpen(false)} />}
    </div>
  );
}
