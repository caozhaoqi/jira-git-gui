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
import { DiffPanel } from './components/DiffPanel';
import { K8sPanel } from './components/k8s/K8sPanel';
import { CfPanel } from './components/CfPanel';
import { ClashPanel } from './components/ClashPanel';
import { HcmObjectBrowser } from './components/hcm/HcmObjectBrowser';
import { SettingsPanel } from './components/SettingsPanel';
import { HcmModelDetail } from './components/hcm/HcmModelDetail';
import { ToastStack } from './components/Toast';
import { LogPanel } from './components/LogPanel';
import { ConnectModal } from './components/ConnectModal';

const ACTIONBAR_TABS = new Set(['repo']);

export default function App() {
  const setStatus = useAppStore((s) => s.setStatus);
  const setRepos = useAppStore((s) => s.setRepos);
  const pushLog = useAppStore((s) => s.pushLog);
  const setProgress = useAppStore((s) => s.setProgress);
  const addToast = useAppStore((s) => s.addToast);
  const setNetworkWarning = useAppStore((s) => s.setNetworkWarning);
  const networkWarning = useAppStore((s) => s.networkWarning);
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  const [connectOpen, setConnectOpen] = useState(false);

  // 双击对象打开的新窗口带 ?hcm-model=<id>&hcm-detail=1：直接渲染独立模型详情页，
  // 不加载主应用外壳（TopBar/Tabs），保持轻量独立窗口。
  const isDetailWindow = typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).has('hcm-detail');
  if (isDetailWindow) {
    return (
      <div className="app-shell app-shell--detail">
        <main className="workspace">
          <div className="workspace-body">
            <HcmModelDetail />
          </div>
        </main>
      </div>
    );
  }

  // ?hcm-meta=<model>：与 ?hcm-detail 合并为同一个「对象详情 + 元数据文件」窗口。
  // 该窗口内 HcmModelDetail 会读取 hcm-meta 作为模型并默认定位到「元数据文件」tab，
  // 不再单独渲染 HcmMetaFileBrowser 独立窗口（两者合二为一）。
  const isMetaWindow = typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).has('hcm-meta');
  if (isMetaWindow) {
    return (
      <div className="app-shell app-shell--detail">
        <main className="workspace">
          <div className="workspace-body">
            <HcmModelDetail />
          </div>
        </main>
      </div>
    );
  }

  // 双击对象打开的新窗口带 ?hcm-model=<id>，自动切到 HCM 面板以触发自动定位
  useEffect(() => {
    if (new URLSearchParams(window.location.search).has('hcm-model')) {
      setTab('hcm');
    }
  }, [setTab]);

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
            {activeTab === 'diff' && <DiffPanel />}
            {activeTab === 'logs' && <LogPanel />}
            {activeTab === 'k8s' && <K8sPanel />}
            {activeTab === 'cf' && <CfPanel />}
            {activeTab === 'clash' && <ClashPanel />}
            {activeTab === 'hcm' && <HcmObjectBrowser />}
            {activeTab === 'settings' && <SettingsPanel />}
          </div>
        </main>
      </div>
      <ToastStack />
      {connectOpen && <ConnectModal onClose={() => setConnectOpen(false)} />}
    </div>
  );
}
