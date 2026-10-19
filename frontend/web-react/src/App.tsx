import { Suspense, lazy, useEffect, useRef, useState, type ComponentType, type ReactNode } from 'react';
import { sse } from './api/events';
import { apiGet, onNetworkDown } from './api/client';
import { useAppStore, type TabKey } from './store/useAppStore';
import { EtaTracker, formatEta } from './utils/eta';
import { applyRepoAlerts, raiseJiraAuthAlert } from './utils/appAlerts';
import { useT } from './i18n';
import type {
  StatusResp,
  ReposResp,
  SSELog,
  SSEProgress,
  SSECloneDone,
  SSEDownloadDone,
  SSENetworkWarning,
  SSECookieExpired,
} from './api/types';
import { normalizeStatus } from './api/types';
import { TopBar } from './components/TopBar';
import { Tabs } from './components/Tabs';
import { ActionBar } from './components/ActionBar';
import { LogPanel } from './components/LogPanel';
// 面板按需加载（代码分割）：11 个面板此前全部静态 import，导致首屏必须下载并解析
// 整个应用代码（实测应用自身 chunk 达 417KB）。改用 React.lazy 后，每个面板各自成包，
// 只有首次切到该页签时才拉取；切走后仍按下面的「永久挂载」策略保留实例，因此不会因为
// 懒加载而丢失终端会话/表单输入等本地状态。
const lazyNamed = <P,>(loader: () => Promise<Record<string, unknown>>, name: string) =>
  lazy(() => loader().then((m) => ({ default: m[name] as ComponentType<P> })));

const RepoPanel = lazyNamed<Record<string, never>>(() => import('./components/RepoPanel'), 'RepoPanel');
const DiffPanel = lazyNamed<Record<string, never>>(() => import('./components/DiffPanel'), 'DiffPanel');
const K8sPanel = lazyNamed<Record<string, never>>(() => import('./components/k8s/K8sPanel'), 'K8sPanel');
const KibanaPanel = lazyNamed<Record<string, never>>(() => import('./components/kibana/KibanaPanel'), 'KibanaPanel');
const CfPanel = lazyNamed<Record<string, never>>(() => import('./components/CfPanel'), 'CfPanel');
const CfConsistencyPanel = lazyNamed<Record<string, never>>(() => import('./components/CfConsistencyPanel'), 'CfConsistencyPanel');
const CfDebugPanel = lazyNamed<Record<string, never>>(() => import('./components/CfDebugPanel'), 'CfDebugPanel');
const ClashPanel = lazyNamed<Record<string, never>>(() => import('./components/ClashPanel'), 'ClashPanel');
const HcmObjectBrowser = lazyNamed<Record<string, never>>(() => import('./components/hcm/HcmObjectBrowser'), 'HcmObjectBrowser');
const HcmApiTester = lazyNamed<Record<string, never>>(() => import('./components/hcm/HcmApiTester'), 'HcmApiTester');
const HcmOps = lazyNamed<Record<string, never>>(() => import('./components/hcm/HcmOps'), 'HcmOps');
const UnifiedDiagnosisPanel = lazyNamed<Record<string, never>>(() => import('./components/UnifiedDiagnosisPanel'), 'UnifiedDiagnosisPanel');
const SettingsPanel = lazyNamed<Record<string, never>>(() => import('./components/SettingsPanel'), 'SettingsPanel');
const AiBridgePanel = lazyNamed<Record<string, never>>(() => import('./components/AiBridgePanel'), 'AiBridgePanel');
import { ToastStack } from './components/Toast';
import { ProgressBar } from './components/ProgressBar';
import { ConnectModal } from './components/ConnectModal';
import { ConfirmHost } from './components/ConfirmHost';
import { ErrorBoundary } from './components/ErrorBoundary';
import { PanelActiveContext } from './utils/panelActive';
import { TabIcon } from './components/icons';
import { AiApiFab } from './components/AiApiFab';

const ACTIONBAR_TABS = new Set(['repo']);

// 每个标签页对应的面板。一次性建好元素引用，App 重渲染时复用同一引用，
// 配合下面的「访问过就常驻挂载、非当前页用 display:none 隐藏」策略，
// 让切换标签页时组件实例不卸载 —— 文件树、diff 结果、扫描进度、终端会话、
// 表单输入、滚动位置等本地状态全部保留，直到整个程序（窗口）关闭才随 React 卸载而清理。
// 未访问过的标签页不会预挂载，避免冷启动就拉起所有面板（K8s 终端等）。
// 每个面板各自的错误边界：面板「访问过即永久挂载」，若某面板 render 抛错又无边界，
// 异常会冒到根节点把全部页签一起卸载（整站白屏，连切页签自救都不行）。
// labelKey 用于错误卡片上显示「哪个页签坏了」。
const PANELS: { key: TabKey; labelKey: string; el: ReactNode }[] = [
  { key: 'repo', labelKey: 'tab.repo', el: <RepoPanel /> },
  { key: 'diff', labelKey: 'tab.diff', el: <DiffPanel /> },
  { key: 'logs', labelKey: 'tab.logs', el: <LogPanel /> },
  { key: 'k8s', labelKey: 'tab.k8s', el: <K8sPanel /> },
  { key: 'kibana', labelKey: 'tab.kibana', el: <KibanaPanel /> },
  { key: 'cf', labelKey: 'tab.cf', el: <CfPanel /> },
  { key: 'cfsync', labelKey: 'tab.cfsync', el: <CfConsistencyPanel /> },
  { key: 'cfdebug', labelKey: 'tab.cfdebug', el: <CfDebugPanel /> },
  { key: 'clash', labelKey: 'tab.clash', el: <ClashPanel /> },
  { key: 'hcm', labelKey: 'tab.hcm', el: <HcmObjectBrowser /> },
  { key: 'hcmapi', labelKey: 'tab.hcmapi', el: <HcmApiTester /> },
  { key: 'hcmops', labelKey: 'tab.hcmops', el: <HcmOps /> },
  { key: 'diagnose', labelKey: 'tab.diagnose', el: <UnifiedDiagnosisPanel /> },
  { key: 'aibridge', labelKey: 'tab.aibridge', el: <AiBridgePanel /> },
  { key: 'settings', labelKey: 'tab.settings', el: <SettingsPanel /> },
];

export default function App() {
  const setStatus = useAppStore((s) => s.setStatus);
  const setRepos = useAppStore((s) => s.setRepos);
  const pushLog = useAppStore((s) => s.pushLog);
  const setProgress = useAppStore((s) => s.setProgress);
  const addToast = useAppStore((s) => s.addToast);
  const setNetworkWarning = useAppStore((s) => s.setNetworkWarning);
  const networkWarning = useAppStore((s) => s.networkWarning);
  const apiDown = useAppStore((s) => s.apiDown);
  const setApiDown = useAppStore((s) => s.setApiDown);
  const [retrying, setRetrying] = useState(false);
  const alerts = useAppStore((s) => s.alerts);
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  const { t } = useT();
  // 连接设置弹窗开关放 store：顶栏、告警横幅、连接弹窗自身都要能打开它
  const connectOpen = useAppStore((s) => s.connectOpen);
  const openConnect = useAppStore((s) => s.openConnect);
  const closeConnect = useAppStore((s) => s.closeConnect);
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
  // 告警横幅容器：量它的底边，让右上角 toast 让开（见下方 effect）
  const alertBoxRef = useRef<HTMLDivElement>(null);

  // 注：?hcm-detail / ?hcm-meta / ?hcm-cf-err 三种「独立轻窗口」已抽到 src/Root.tsx，
  // 由 main.tsx 按 URL 选择渲染。这样主 App 不再用条件 return 提前退出而跳过后续
  // Hook（否则 URL 查询串一旦变化、Hook 数量突变就会白屏）。本组件只渲染主窗口。

  // 双击对象打开的新窗口带 ?hcm-model=<id>，自动切到 HCM 面板以触发自动定位
  useEffect(() => {
    if (new URLSearchParams(window.location.search).has('hcm-model')) {
      setTab('hcm');
    }
  }, [setTab]);

  // 原生菜单打开的独立窗口带 ?tab=<key>：初始即落到对应页签
  useEffect(() => {
    const tab = new URLSearchParams(window.location.search).get('tab') as TabKey | null;
    if (tab && PANELS.some((p) => p.key === tab)) setTab(tab);
  }, [setTab]);

  // ?embed=1：首选项式专注窗口，隐藏侧栏
  const [embed] = useState(
    () => new URLSearchParams(window.location.search).get('embed') === '1',
  );

  // 主窗口把当前激活页签上报给原生菜单（用于「新窗口打开当前功能」）；
  // embed 独立窗口不报，避免覆盖主窗口记录的 currentMainTab。
  useEffect(() => {
    const api = (window as unknown as {
      electronAPI?: { setActiveTab?: (tab: string) => void };
    }).electronAPI;
    if (api?.setActiveTab && !embed) api.setActiveTab(activeTab);
  }, [activeTab, embed]);

  // 注册前端↔后端连接中断的全局回调：任意本地 API 请求抛 network/timeout 即点亮横幅
  useEffect(() => {
    onNetworkDown(() => setApiDown(true));
  }, [setApiDown]);

  // 重试连接：后端恢复后让界面自愈（重拉状态/仓库 + 重连 SSE）
  const retryConnection = async () => {
    setRetrying(true);
    try {
      const s = await apiGet<StatusResp>('/api/status');
      const ns = normalizeStatus(s);
      setStatus(ns);
      if (typeof ns.qps === 'number') useAppStore.getState().setQps(ns.qps);
      setApiDown(false);
      sse.connect();
      apiGet<ReposResp>('/api/repos')
        .then((r) => {
          if (!r.error) setRepos(r.repos || []);
          applyRepoAlerts(r);
        })
        .catch(() => {});
      addToast(t('net.recovered'), 'success');
    } catch {
      addToast(t('net.stillDown'), 'error');
    } finally {
      setRetrying(false);
    }
  };

  useEffect(() => {
    apiGet<StatusResp>('/api/status')
      .then((s) => {
        const ns = normalizeStatus(s);
        setStatus(ns);
        if (typeof ns.qps === 'number') useAppStore.getState().setQps(ns.qps);
        setApiDown(false);
      })
      .catch(() => {
        // 后端未就绪：点亮「后端不可达」横幅（含诊断步骤），不静默
        setApiDown(true);
      });

    apiGet<ReposResp>('/api/repos')
      .then((r) => {
        // 配置/会话信号统一映射（与手动刷新、差异页共用同一套告警构造）
        applyRepoAlerts(r);
        if (r.error) {
          pushLog(`发现仓库错误：${r.error}`, 'warning');
        } else {
          const list = r.repos || [];
          setRepos(list);
          pushLog(`【发现仓库】返回 ${list.length} 个`);
          // 按后端当前仓库回填选中项：后端（client.set_repo）在刷新后仍记得已选仓库，
          // 而前端 store 的 selectedRepo 是内存态。此前不回填 → 顶栏显示着仓库名，
          // 文件树却是空的并提示「请先选择仓库」，两边状态互相矛盾。
          const st = useAppStore.getState();
          if (!st.selectedRepo && st.status?.repo_id) {
            const hit = list.find((x) => String(x.repo_id) === String(st.status?.repo_id));
            if (hit) {
              st.selectRepo(hit);
              pushLog(`已恢复上次选择的仓库：${hit.display_name || hit.repo_id}`);
            }
          }
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
          setProgress({ visible: false });
        } else {
          // 失败必须在进度区显性呈现（此前直接隐藏进度条，失败与成功看起来一样）
          pushLog(`克隆失败：${d.msg}`, 'error');
          addToast(`克隆失败：${d.msg}`, 'error');
          setProgress({
            visible: true,
            mode: 'error',
            stage: '克隆失败',
            detail: d.msg || '',
            eta: '',
          });
        }
      }),
      sse.on('download_done', (d: SSEDownloadDone) => {
        cloneEtaStarted.current = false;
        pushLog(
          `下载完成：成功 ${d.ok_count}（跳过 ${d.skipped}），失败 ${d.fail_count}。`
        );
        if (d.dest) pushLog(`已保存到：${d.dest}`);
        if ((d.fail_count || 0) > 0) {
          addToast(`下载完成，但有 ${d.fail_count} 项失败`, 'error');
          setProgress({
            visible: true,
            mode: 'error',
            stage: `下载失败 ${d.fail_count} 项`,
            detail: d.dest ? `已保存到：${d.dest}（成功 ${d.ok_count}，跳过 ${d.skipped}）` : '',
            eta: '',
          });
        } else {
          setProgress({ visible: false });
        }
      }),
      sse.on('network_warning', (d: SSENetworkWarning) => {
        const msg = d.message || '网络中断';
        pushLog(msg, 'error');
        addToast(msg, 'warn');
        setNetworkWarning(msg);
      }),
      sse.on('cookie_expired', (d: SSECookieExpired) => {
        // F3：远端会话失效（Cookie 过期 / 浏览页不可用）。横幅常驻直到用户处理，
        // 不自动消失；后端可能重复广播，setAlert 幂等覆盖即可。
        raiseJiraAuthAlert(d.detail);
        pushLog(`远端会话告警：${d.detail || d.status}`, 'error');
      }),
    ];
    return () => offs.forEach((off) => off());
  }, [setStatus, setRepos, pushLog, setProgress, addToast, setNetworkWarning]);

  // 持久告警横幅和右上角 toast 都锚在顶部，同时出现时 toast 会压住横幅右端
  // （横幅高度随文案行数、按钮换行、语言切换变化，写死偏移量必然对不齐）。
  // 把横幅的**视口底边**写进 CSS 变量，toast 据此下移。.workspace 本身不滚动
  // （滚动的是内层 .workspace-body），所以这个值不会随滚动失效。
  //
  // ⚠️ 必须**每次渲染都重量一次**：横幅上方的 <ActionBar> 会随标签页出现/消失，
  // 那会让横幅整体位移、而它自身高度不变 —— ResizeObserver 收不到这种变化，
  // 只挂 RO 会留下一个按旧位置算出的过期值，toast 照样压上来（实测过）。
  useEffect(() => {
    const el = alertBoxRef.current;
    const root = document.documentElement;
    if (!el) {
      root.style.removeProperty('--app-alert-bottom');
      return;
    }
    root.style.setProperty('--app-alert-bottom', `${Math.round(el.getBoundingClientRect().bottom)}px`);
  });

  // 高度变化（换语言、文本换行、字体加载完）不一定伴随 React 重渲染，用 RO 兜住。
  useEffect(() => {
    const el = alertBoxRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      document.documentElement.style.setProperty(
        '--app-alert-bottom',
        `${Math.round(el.getBoundingClientRect().bottom)}px`,
      );
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [alerts.length]);

  return (
    <div className="app-shell">
      <TopBar onOpenConnect={openConnect} />
      <div className="app-body">
        {!embed && <Tabs />}
        <main className="workspace">
          {ACTIONBAR_TABS.has(activeTab) && <ActionBar />}
          {networkWarning && (
            <div className="network-warning" onClick={() => setNetworkWarning(null)}>
              <TabIcon name="alert" size={13} /> {networkWarning}
            </div>
          )}
          {/* 全局状态告警：凭据失效 / 配置缺失这类「会让功能整体不可用」的前置条件，
              统一由这里渲染成持久、可操作的横幅（文案与按钮由 utils/appAlerts 定义）。
              外层容器供上面那个 effect 量高度，用于把 toast 让开（见 --app-alert-bottom）。 */}
          {alerts.length > 0 && (
            <div className="app-alerts" ref={alertBoxRef}>
              {alerts.map((a) => (
                <div className="app-alert" role="alert" key={a.id}>
                  <div className="app-alert-text">
                    <strong><TabIcon name="alert" size={13} /> {t(a.titleKey)}</strong>
                    <span>{a.detail || (a.detailKey ? t(a.detailKey) : '')}</span>
                  </div>
                  {!!a.actions?.length && (
                    <div className="app-alert-actions">
                      {a.actions.map((act) => (
                        <button
                          key={act.labelKey}
                          className={`btn btn-sm ${act.primary ? 'btn-primary' : 'btn-ghost'}`}
                          onClick={act.onClick}
                        >
                          {t(act.labelKey)}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {apiDown && (
            <div className="api-down-banner" role="alert">
              <div className="api-down-text">
                <strong><TabIcon name="alert" size={13} /> {t('net.title')}</strong>
                <span>{t('net.desc')}</span>
                <ul className="api-down-steps">
                  <li>{t('net.step1')}</li>
                  <li>{t('net.step2')}</li>
                  <li>{t('net.step3')}</li>
                  <li>{t('net.step4')}</li>
                </ul>
              </div>
              <div className="api-down-actions">
                <button
                  className="btn btn-sm btn-primary"
                  disabled={retrying}
                  onClick={retryConnection}
                >
                  {retrying ? t('net.retrying') : t('net.retry')}
                </button>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={() => setApiDown(false)}
                >
                  {t('net.dismiss')}
                </button>
              </div>
            </div>
          )}
          <div className="workspace-body">
            {PANELS.map(({ key, labelKey, el }) =>
              visited.has(key) || activeTab === key ? (
                <div
                  key={key}
                  className={`tab-pane${activeTab === key ? '' : ' tab-pane--hidden'}`}
                >
                  <ErrorBoundary label={t(labelKey)}>
                    {/* 把「是否当前页签」告知面板：隐藏面板据此暂停轮询 */}
                    <PanelActiveContext.Provider value={activeTab === key}>
                      <Suspense fallback={<div className="panel-loading">{t('common.loading')}</div>}>
                        {el}
                      </Suspense>
                    </PanelActiveContext.Provider>
                  </ErrorBoundary>
                </div>
              ) : null,
            )}
          </div>
        </main>
      </div>
      <ToastStack />
      <ProgressBar />
      {/* AI 接口悬浮按钮：跟随当前页签，一键复制该功能的 AI 数据接口（embed 窗口不显示） */}
      {!embed && <AiApiFab tab={activeTab} />}
      {connectOpen && <ConnectModal onClose={closeConnect} />}
      <ConfirmHost />
    </div>
  );
}
