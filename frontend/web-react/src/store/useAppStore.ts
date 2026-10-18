import { create } from 'zustand';
import type { Repo, StatusResp, DiffStatus } from '../api/types';
import type { Locale } from '../i18n/types';
import { DEFAULT_LOCALE, LOCALE_STORAGE_KEY } from '../i18n/types';
import { setLocale as setI18nLocale } from '../i18n';
import { apiPost } from '../api/client';

export type Theme = 'light' | 'dark';
export type TabKey = 'repo' | 'diff' | 'logs' | 'k8s' | 'kibana' | 'cf' | 'cfsync' | 'clash' | 'hcm' | 'hcmapi' | 'hcmops' | 'cfdebug' | 'settings' | 'diagnose';
export type ToastType = 'success' | 'warn' | 'error' | 'info';
export type ProgressMode =
  | 'indeterminate'
  | 'determinate'
  | 'done'
  | 'error';

export interface Toast {
  id: number;
  message: string;
  type: ToastType;
  action?: { label: string; primary?: boolean; onClick: () => void };
  duration: number; // 0 = 不自动消失
}

export interface ProgressState {
  visible: boolean;
  mode: ProgressMode;
  pct: number;
  stage: string;
  detail: string;
  /** 预计剩余时间的人类可读文案（如「1 分 30 秒」），无可靠估算时为空串 */
  eta?: string;
}

export interface LogLine {
  msg: string;
  level: string;
}

/** 全局告警横幅上的一个操作按钮。
 *  label 存 i18n key（不是译文）：渲染时才翻译，切换语言即时生效。 */
export interface AppAlertAction {
  labelKey: string;
  primary?: boolean;
  onClick: () => void;
}

/**
 * 全局状态告警：**「凭据失效 / 配置缺失」这类会让功能整体不可用的前置条件**统一走这里，
 * 由 App 渲染成持久、可操作的横幅。
 *
 * 为什么要有这个通道：这类问题此前往往只写一行日志（或干脆被 catch 吞掉），
 * 用户看到的是「请选择仓库 / 无可用环境 / 无站点」这种空状态——完全不知道
 * 出了什么事、下一步该做什么。参见仓库发现 Cookie 过期那次（后端 200 + 空列表）。
 *
 * 约定：id 稳定，同一来源重复上报只覆盖不堆叠；`detail` 放后端给的具体证据。
 */
export interface AppAlert {
  id: string;
  titleKey: string;
  /** 静态说明（i18n key） */
  detailKey?: string;
  /** 动态细节（后端返回的具体原因），有值时优先于 detailKey 展示 */
  detail?: string;
  actions?: AppAlertAction[];
}

interface AppState {
  status: StatusResp | null;
  repos: Repo[];
  selectedRepo: Repo | null;
  selectedFilePath: string | null;
  theme: Theme;
  locale: Locale;
  activeTab: TabKey;
  sidebarOpen: boolean;
  toasts: Toast[];
  progress: ProgressState;
  logs: LogLine[];
  qps: number;
  concurrency: number;
  checkedPaths: string[];
  /** F1：最近一次差异扫描的状态叠加层（path -> DiffStatus），供文件树显示差异色点。 */
  diffOverlay: Record<string, DiffStatus>;
  setDiffOverlay: (m: Record<string, DiffStatus>) => void;
  clearDiffOverlay: () => void;
  /** F3：远端会话健康告警等全局横幅（Cookie 失效 / 配置缺失 / 环境缺失）。 */
  alerts: AppAlert[];
  /** 上报一条告警（同 id 覆盖，不堆叠）。 */
  setAlert: (a: AppAlert) => void;
  /** 清除/忽略一条告警。 */
  clearAlert: (id: string) => void;
  /** 「连接设置」弹窗开关。放 store 是因为多个入口（顶栏、告警横幅、连接弹窗自身）
   *  都要能打开它；此前是 App 的局部 state，横幅按钮拿不到。 */
  connectOpen: boolean;
  openConnect: () => void;
  closeConnect: () => void;
  networkWarning: string | null;

  /** 前端↔本地后端连接中断（后端未启动 / 端口被占 / 代理干扰 localhost 等）。
   *  true 时全局横幅提示网络错误并提供诊断步骤。与 SSE 的 networkWarning（后端↔上游）区分开。 */
  apiDown: boolean;
  setApiDown: (v: boolean) => void;
  treeLocalDir: string;
  setTreeLocalDir: (d: string) => void;

  // HCM token：全局唯一来源 + localStorage 持久化（hcm.token）。
  // 此前 4 个 HCM 面板各自 useState + 各写各的 localStorage，互不联动；
  // 抽到 store 后，改一处所有面板即时同步，刷新后也自动回填。
  hcmToken: string;
  setHcmToken: (t: string) => void;
  // 用 cf_accounts 中该网关已存账密重新登录，取回新 token（HCM token 默认仅 2h 有效）。
  // serverUrl 为空（面板无网关概念）时无法定位账号，返回 ok:false。
  refreshHcmToken: (serverUrl: string, proxy?: string) => Promise<{ ok: boolean; token?: string; error?: string }>;

  setStatus: (s: StatusResp | null) => void;
  setRepos: (r: Repo[]) => void;
  selectRepo: (r: Repo | null) => void;
  setSelectedFile: (p: string | null) => void;
  toggleTheme: () => void;
  setLocale: (l: Locale) => void;
  setTab: (t: TabKey) => void;
  toggleSidebar: () => void;
  addToast: (
    message: string,
    type?: ToastType,
    opts?: { action?: Toast['action']; duration?: number }
  ) => number;
  removeToast: (id: number) => void;
  setProgress: (p: Partial<ProgressState>) => void;
  pushLog: (msg: string, level?: string) => void;
  clearLogs: () => void;
  setQps: (q: number) => void;
  setConcurrency: (n: number) => void;
  toggleCheckedPath: (path: string) => void;
  clearCheckedPaths: () => void;
  setNetworkWarning: (msg: string | null) => void;
}

const THEME_KEY = 'jgg-theme';
const SIDEBAR_KEY = 'jgg-sidebar-open';
const TAB_KEY = 'jgg-active-tab';

function initialTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'dark' || v === 'light') return v;
  } catch {
    /* ignore */
  }
  return 'light';
}

function initialSidebarOpen(): boolean {
  try {
    const v = localStorage.getItem(SIDEBAR_KEY);
    if (v === '0' || v === 'false') return false;
  } catch {
    /* ignore */
  }
  return true;
}

/** 上次停留的页签：刷新后回到原页签（此前硬编码 'repo'，刷新总是被弹回仓库页）。 */
function initialTab(): TabKey {
  try {
    const v = localStorage.getItem(TAB_KEY);
    if (v && TAB_KEYS.has(v as TabKey)) return v as TabKey;
  } catch {
    /* ignore */
  }
  return 'repo';
}

const TAB_KEYS = new Set<TabKey>([
  'repo', 'diff', 'logs', 'k8s', 'kibana', 'cf', 'cfsync', 'cfdebug', 'clash', 'hcm', 'hcmapi', 'hcmops', 'diagnose', 'settings',
]);

function initialLocale(): Locale {
  try {
    const v = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (v === 'zh-CN' || v === 'en-US' || v === 'ja-JP') return v;
  } catch {
    /* ignore */
  }
  return DEFAULT_LOCALE;
}

function applyThemeClass(theme: Theme) {
  const dark = theme === 'dark';
  document.documentElement.classList.toggle('dark', dark);
  document.body.classList.toggle('dark', dark);
}

let toastSeq = 1;

// ⚠️ 不要在 store 模块顶层调用 setI18nLocale(initialLocale()) 来对齐语言！
// client.ts 也 import 了 i18n（错误文案 t()），模块求值顺序是
// App → client → i18n → (循环) store —— store 模块体会先于 i18n 模块体执行，
// 此时 i18n 的 `let currentLocale` 还在 TDZ，setLocale 会抛 ReferenceError
// 让整条模块链崩溃 → 白屏（连 ErrorBoundary 都渲染不出来）。
// 初始化对齐改由 i18n/index.ts 自己读 localStorage（同一个 LOCALE_STORAGE_KEY）完成。

export const useAppStore = create<AppState>((set, get) => ({
  status: null,
  repos: [],
  selectedRepo: null,
  selectedFilePath: null,
  theme: initialTheme(),
  locale: initialLocale(),
  activeTab: initialTab(),
  sidebarOpen: initialSidebarOpen(),
  toasts: [],
  progress: {
    visible: false,
    mode: 'determinate',
    pct: 0,
    stage: '',
    detail: '',
    eta: '',
  },
  logs: [],
  qps: 6,
  concurrency: 4,
  checkedPaths: [],
  diffOverlay: {},
  alerts: [],
  connectOpen: false,
  networkWarning: null,
  apiDown: false,
  treeLocalDir: (() => {
    try {
      return localStorage.getItem('jgg-tree-local-dir') || '';
    } catch {
      return '';
    }
  })(),
  hcmToken: (() => {
    try {
      return localStorage.getItem('hcm.token') || '';
    } catch {
      return '';
    }
  })(),

  setStatus: (s) => set({ status: s }),
  setRepos: (r) => set({ repos: r }),
  selectRepo: (r) => set({ selectedRepo: r, checkedPaths: [] }),
  setSelectedFile: (p) => set({ selectedFilePath: p }),
  toggleTheme: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark';
    applyThemeClass(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* ignore */
    }
    set({ theme: next });
  },
  setLocale: (l) => {
    setI18nLocale(l);
    try {
      localStorage.setItem(LOCALE_STORAGE_KEY, l);
    } catch {
      /* ignore */
    }
    set({ locale: l });
  },
  setTab: (t) => {
    set({ activeTab: t });
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch {
      /* ignore */
    }
  },
  toggleSidebar: () => {
    const next = !get().sidebarOpen;
    try {
      localStorage.setItem(SIDEBAR_KEY, next ? '1' : '0');
    } catch {
      /* ignore */
    }
    set({ sidebarOpen: next });
  },
  addToast: (message, type = 'info', opts) => {
    const id = toastSeq++;
    const toast: Toast = {
      id,
      message,
      type,
      action: opts?.action,
      duration: opts?.duration ?? (type === 'error' ? 8000 : 3500),
    };
    set((s) => ({ toasts: [...s.toasts, toast] }));
    return id;
  },
  removeToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  setProgress: (p) =>
    set((s) => ({ progress: { ...s.progress, ...p } })),
  pushLog: (msg, level = 'info') =>
    set((s) => {
      const logs = [...s.logs, { msg, level }];
      if (logs.length > 3000) logs.splice(0, logs.length - 3000);
      return { logs };
    }),
  clearLogs: () => set({ logs: [] }),
  setQps: (q) => set({ qps: q }),
  setConcurrency: (n) => set({ concurrency: Math.max(1, Math.min(16, n)) }),
  toggleCheckedPath: (path) =>
    set((s) => {
      const has = s.checkedPaths.includes(path);
      return {
        checkedPaths: has
          ? s.checkedPaths.filter((p) => p !== path)
          : [...s.checkedPaths, path],
      };
    }),
  clearCheckedPaths: () => set({ checkedPaths: [] }),
  setDiffOverlay: (m) => set({ diffOverlay: m }),
  clearDiffOverlay: () => set({ diffOverlay: {} }),
  setAlert: (a) =>
    set((s) => {
      const rest = s.alerts.filter((x) => x.id !== a.id);
      // 新的排前面：最相关的提醒离用户视线最近
      return { alerts: [a, ...rest] };
    }),
  clearAlert: (id) => set((s) => ({ alerts: s.alerts.filter((x) => x.id !== id) })),
  openConnect: () => set({ connectOpen: true }),
  closeConnect: () => set({ connectOpen: false }),
  setNetworkWarning: (msg) => set({ networkWarning: msg }),
  setApiDown: (v) => set({ apiDown: v }),
  setTreeLocalDir: (d) => {
    try {
      localStorage.setItem('jgg-tree-local-dir', d);
    } catch {
      /* ignore */
    }
    set({ treeLocalDir: d });
  },
  setHcmToken: (t) => {
    const v = t || '';
    try {
      if (v.trim()) localStorage.setItem('hcm.token', v.trim());
      else localStorage.removeItem('hcm.token');
    } catch {
      /* ignore */
    }
    set({ hcmToken: v });
  },
  refreshHcmToken: async (serverUrl, proxy = '') => {
    const su = (serverUrl || '').trim();
    if (!su) return { ok: false, error: '缺少 server_url，无法自动重登' };
    try {
      const d = await apiPost<{ ok?: boolean; token?: string; cookie?: string; error?: string }>(
        '/api/cf/refresh-token',
        { server_url: su, proxy },
      );
      if (d && d.ok && (d.token || d.cookie)) {
        const tk = (d.token || '').trim();
        if (tk) get().setHcmToken(tk);
        return { ok: true, token: tk };
      }
      return { ok: false, error: d?.error || 'refresh 返回非预期' };
    } catch (e: any) {
      return { ok: false, error: e?.message || String(e) };
    }
  },
}));

// 初始化主题 class
applyThemeClass(initialTheme());
// i18n 语言的初始化对齐由 i18n/index.ts 自己读 localStorage 完成（勿在此调用
// setI18nLocale——循环依赖下 store 模块体可能先于 i18n 模块体执行，currentLocale
// 处于 TDZ 会抛 ReferenceError 白屏）。运行时切换语言仍走下方 setLocale action。
