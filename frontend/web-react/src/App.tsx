import { useEffect, useRef, useState, type ReactNode } from 'react';
import { sse } from './api/events';
import { apiGet } from './api/client';
import { useAppStore, type TabKey } from './store/useAppStore';
import { EtaTracker, formatEta } from './utils/eta';
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
import { HcmCloudFuncErrorLocator } from './components/hcm/HcmCloudFuncErrorLocator';
import { UnifiedDiagnosisPanel } from './components/UnifiedDiagnosisPanel';
import { ToastStack } from './components/Toast';
import { ProgressBar } from './components/ProgressBar';
import { LogPanel } from './components/LogPanel';
import { ConnectModal } from './components/ConnectModal';

const ACTIONBAR_TABS = new Set(['repo']);

// 每个标签页对应的面板。一次性建好元素引用，App 重渲染时复用同一引用，
// 配合下面的「访问过就常驻挂载、非当前页用 display:none 隐藏」策略，
// 让切换标签页时组件实例不卸载 —— 文件树、diff 结果、扫描进度、终端会话、
// 表单输入、滚动位置等本地状态全部保留，直到整个程序（窗口）关闭才随 React 卸载而清理。
// 未访问过的标签页不会预挂载，避免冷启动就拉起所有面板（K8s 终端等）。
const PANELS: { key: TabKey; el: ReactNode }[] = [
  { key: 'repo', el: <RepoPanel /> },
  { key: 'diff', el: <DiffPanel /> },
  { key: 'logs', el: <LogPanel /> },
  { key: 'k8s', el: <K8sPanel /> },
  { key: 'cf', el: <CfPanel /> },
  { key: 'clash', el: <ClashPanel /> },
  { key: 'hcm', el: <HcmObjectBrowser /> },
  { key: 'diagnose', el: <UnifiedDiagnosisPanel /> },
  { key: 'settings', el: <SettingsPanel /> },
];

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
  // 访问过的标签页集合：首次切到某标签页就加入并永久保持挂载（仅隐藏），
  // 因此来回切换不会重置该面板内容。清空只发生在整个 App 卸载（程序关闭）。
  const [visited, setVisited] = useState<Set<TabKey>>(
    () => new Set<TabKey>([activeTab]),
  );
  useEffect(() => {
    setVisited((prev) =>
      prev.has(activeTab) ? prev : new Set(prev).add(activeTab),
    );
  }, [activeTab]);
  // 克隆 / 下载进度的 ETA 估算（后端给真实 total，可用 etaFromTotal）
  const cloneEta = useRef(new EtaTracker());
  const cloneEtaStarted = useRef(false);

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

  // ?hcm-cf-err=1：云函数错误定位（独立轻窗口）。可带 ?hcm-loc=<文本> / ?hcm-loc-model / ?hcm-loc-id / ?hcm-loc-field 预填。
  const isCfErrWindow = typeof window !== 'undefined' &&
    new URLSearchParams(window.location.search).has('hcm-cf-err');
  if (isCfErrWindow) {
    return (
      <div className="app-shell app-shell--detail">
        <main className="workspace">
          <div className="workspace-body">
            <HcmCloudFuncErrorLocator />
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
        const total = d.total ?? 0;
        const done = d.done ?? 0;
        if (!cloneEtaStarted.current) {
          cloneEta.current.reset(done);
          cloneEtaStarted.current = true;
        }
        const etaSec = cloneEta.current.etaFromTotal(done, total);
        const eta = etaSec != null ? formatEta(etaSec) : '';
        setProgress({
          visible: true,
          mode: total > 0 ? 'determinate' : 'indeterminate',
          pct: total > 0 ? Math.round((done / total) * 100) : 0,
          stage: '处理中',
          detail: total > 0 ? `${done}/${total} (${Math.round((done / total) * 100)}%)` : `已处理 ${done}…`,
          eta,
        });
      }),
      sse.on('clone_done', (d: SSECloneDone) => {
        cloneEtaStarted.current = false;
        if (d.ok) {
          pushLog(`克隆结果：${d.msg}`);
          if (d.path) pushLog(`本地路径：${d.path}`);
        }
        setProgress({ visible: false });
      }),
      sse.on('download_done', (d: SSEDownloadDone) => {
        cloneEtaStarted.current = false;
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
            {PANELS.map(({ key, el }) =>
              visited.has(key) || activeTab === key ? (
                <div
                  key={key}
                  className={`tab-pane${activeTab === key ? '' : ' tab-pane--hidden'}`}
                >
                  {el}
                </div>
              ) : null,
            )}
          </div>
        </main>
      </div>
      <ToastStack />
      <ProgressBar />
      {connectOpen && <ConnectModal onClose={() => setConnectOpen(false)} />}
    </div>
  );
}
