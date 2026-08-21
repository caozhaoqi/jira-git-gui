import { useEffect } from 'react';
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
import { RepoPanel } from './components/RepoPanel';
import { CommitsPanel } from './components/CommitsPanel';
import { DiffPanel } from './components/DiffPanel';
import { K8sPanel } from './components/k8s/K8sPanel';
import { CfPanel } from './components/CfPanel';
import { ToastStack } from './components/Toast';
import { ProgressBar } from './components/ProgressBar';
import { LogPanel } from './components/LogPanel';
import { ConnectModal } from './components/ConnectModal';
import { useState } from 'react';

export default function App() {
  const setStatus = useAppStore((s) => s.setStatus);
  const setRepos = useAppStore((s) => s.setRepos);
  const pushLog = useAppStore((s) => s.pushLog);
  const setProgress = useAppStore((s) => s.setProgress);
  const addToast = useAppStore((s) => s.addToast);
  const activeTab = useAppStore((s) => s.activeTab);
  const [connectOpen, setConnectOpen] = useState(false);

  // 初始化：状态 + 仓库列表 + SSE 接线
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
        pushLog(d.message || '网络中断', 'error');
        addToast(d.message || '网络中断', 'warn');
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [setStatus, setRepos, pushLog, setProgress, addToast]);

  return (
    <div className="app-shell">
      <TopBar onOpenConnect={() => setConnectOpen(true)} />
      <Tabs />
      <main className="app-main">
        {activeTab === 'repo' && <RepoPanel />}
        {activeTab === 'commits' && <CommitsPanel />}
        {activeTab === 'diff' && <DiffPanel />}
        {activeTab === 'k8s' && <K8sPanel />}
        {activeTab === 'cf' && <CfPanel />}
      </main>
      <LogPanel />
      <ProgressBar />
      <ToastStack />
      {connectOpen && <ConnectModal onClose={() => setConnectOpen(false)} />}
    </div>
  );
}
