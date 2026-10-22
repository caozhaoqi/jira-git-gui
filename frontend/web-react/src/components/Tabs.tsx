import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';
import { useT } from '../i18n';
import { TabIcon } from './icons';

// 折叠分组定义：同类功能收进一组（点击组头展开/收起），组头为 Linear 式
// 小号 section 行。组本身不是页面，不能加 .active（否则组与子项同时高亮），
// 用 --contains-active 修饰类标示「组内含当前页」的弱提示。
type TabDef = { key: TabKey; labelKey: string; icon: string };

// 高频页签：平铺展示（顺序即 ⌘/Ctrl+1..6 的编号，新加项注意快捷键语义）
const MAIN_TABS: TabDef[] = [
  { key: 'repo', labelKey: 'tab.repo', icon: 'repo' },
  { key: 'diff', labelKey: 'tab.diff', icon: 'diff' },
  { key: 'cf', labelKey: 'tab.cf', icon: 'cf' },
  { key: 'hcm', labelKey: 'tab.hcm', icon: 'hcm' },
  { key: 'k8s', labelKey: 'tab.k8s', icon: 'k8s' },
  { key: 'kibana', labelKey: 'tab.kibana', icon: 'kibana' },
];

// 同类功能分组：组内保留次高频项
const CF_GROUP = {
  key: 'group-cf', icon: 'groupCF', labelKey: 'tab.groupCF',
  tabs: [
    { key: 'cfmonitor', labelKey: 'tab.cfmonitor', icon: 'cfmonitor' },
    { key: 'cfsync', labelKey: 'tab.cfsync', icon: 'cfsync' },
    { key: 'cfdebug', labelKey: 'tab.cfdebug', icon: 'cfdebug' },
  ] as TabDef[],
};
const HCM_GROUP = {
  key: 'group-hcm', icon: 'groupHCM', labelKey: 'tab.groupHCM',
  tabs: [
    { key: 'hcmapi', labelKey: 'tab.hcmapi', icon: 'hcmapi' },
    { key: 'hcmops', labelKey: 'tab.hcmops', icon: 'hcmops' },
  ] as TabDef[],
};
const TAB_GROUPS = [CF_GROUP, HCM_GROUP];

type TabGroup = {
  key: string; icon: string; labelKey: string; tabs: TabDef[]; electronHidden?: boolean;
};

function makeSysGroup(): TabGroup {
  return {
    key: 'group-sys', icon: 'groupSys', labelKey: 'tab.system', electronHidden: true,
  tabs: [
    { key: 'logs', labelKey: 'tab.logs', icon: 'logs' },
    { key: 'clash', labelKey: 'tab.clash', icon: 'clash' },
    { key: 'diagnose', labelKey: 'tab.diagnose', icon: 'diagnose' },
    { key: 'aibridge', labelKey: 'tab.aibridge', icon: 'bridge' },
    { key: 'settings', labelKey: 'tab.settings', icon: 'settings' },
  ],
  };
}
const SYS_GROUP = makeSysGroup();

// macOS 下快捷键提示用 ⌘，其它平台 Ctrl
const IS_MAC = /Mac|iPod|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const modKey = IS_MAC ? '⌘' : 'Ctrl+';

export function Tabs() {
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  const sidebarOpen = useAppStore((s) => s.sidebarOpen);
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const { t } = useT();

  const isElectron = !!(window as any).electronAPI?.isElectron;

  // 原生菜单跳转页签（Electron「首选项」区域的日志 / Clash / 诊断 / 设置）
  useEffect(() => {
    const api = (window as any).electronAPI;
    if (!api?.onNavTab) return;
    const off = api.onNavTab((tabKey: string) => {
      if (tabKey) setTab(tabKey as TabKey);
    });
    return off;
  }, [setTab]);

  // ⌘/Ctrl+1..6 直达平铺页签（编号 = MAIN_TABS 顺序）。
  // Electron 原生菜单的 Cmd/Ctrl+Alt+1..4 走 alt 修饰，这里排除 alt 避免冲突；
  // 纯浏览器下浏览器可能保留 Cmd+1（切浏览器标签页），不拦截成功也无副作用。
  useEffect(() => {
    const onKey = (e: WindowEventMap['keydown']) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      const n = Number(e.key);
      if (!Number.isInteger(n) || n < 1 || n > MAIN_TABS.length) return;
      e.preventDefault();
      setTab(MAIN_TABS[n - 1].key);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setTab]);

  // 折叠态：expanded = manual ?? 自动展开（激活项落在组内时）。
  // 只用布尔会有「激活组永远展开、点组头收不起来」的坑：containsActive 持续为
  // true 会压过 toggle。改为记录**用户手动操作**，未手动操作过时跟随自动展开；
  // 切到组内其它子项时清除该组 manual，恢复「激活组自动展开」的跟随行为。
  const groups = isElectron ? TAB_GROUPS : [...TAB_GROUPS, SYS_GROUP];
  const [manual, setManual] = useState<Record<string, boolean>>({});

  const expandedOf = (g: TabGroup) =>
    manual[g.key] ?? g.tabs.some((tab) => tab.key === activeTab);

  // 激活页签变化：清除「新激活项所在组」的手动记录，让该组恢复自动展开
  useEffect(() => {
    setManual((m) => {
      let changed = false;
      const next = { ...m };
      for (const g of groups) {
        if (g.tabs.some((tab) => tab.key === activeTab) && g.key in next) {
          delete next[g.key];
          changed = true;
        }
      }
      return changed ? next : m;
    });
    // groups 每次渲染都是新数组，依赖只放 activeTab / isElectron
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, isElectron]);

  const toggleTitle = sidebarOpen ? t('app.sidebarCollapse') : t('app.sidebarExpand');

  // 可见的叶子页签（折叠组未展开时其子树不计入方向键循环），用于 ARIA tab 的
  // ←/→/Home/End 焦点-选择联动。顺序即 DOM 渲染顺序。
  const visibleTabs = [
    ...MAIN_TABS,
    ...groups.flatMap((g) => (expandedOf(g) ? g.tabs : [])),
  ];
  const navRef = useRef<HTMLElement>(null);

  // ARIA tab 模式：方向键在叶子页签间移动并切换（焦点与选中同步）
  const onNavKeyDown = (e: ReactKeyboardEvent) => {
    if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return;
    const keys = visibleTabs.map((t) => t.key);
    const idx = keys.indexOf(activeTab);
    if (idx < 0) return;
    let next = idx;
    if (e.key === 'ArrowRight') next = (idx + 1) % keys.length;
    else if (e.key === 'ArrowLeft') next = (idx - 1 + keys.length) % keys.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = keys.length - 1;
    e.preventDefault();
    const key = keys[next];
    setTab(key);
    // 选中后把焦点移到新页签按钮（roving tabindex：仅选中项可 Tab 进入）
    requestAnimationFrame(() => {
      navRef.current?.querySelector<HTMLElement>(`#tab-${key}`)?.focus();
    });
  };

  const renderTab = (tab: TabDef, extraClass = '', hint?: string) => (
    <button
      key={tab.key}
      id={`tab-${tab.key}`}
      role="tab"
      aria-selected={activeTab === tab.key}
      aria-controls={`panel-${tab.key}`}
      tabIndex={activeTab === tab.key ? 0 : -1}
      className={`tab ${extraClass} ${activeTab === tab.key ? 'active' : ''}`}
      data-tab={tab.key}
      onClick={() => setTab(tab.key)}
      title={sidebarOpen ? hint : t(tab.labelKey)}
    >
      <span className="tab-ico" aria-hidden="true"><TabIcon name={tab.icon} /></span>
      <span className="tab-txt">{t(tab.labelKey)}</span>
      {hint && sidebarOpen && (
        <span className="tab-hotkey" aria-hidden="true">{hint}</span>
      )}
    </button>
  );

  const renderGroup = (g: TabGroup) => {
    const containsActive = g.tabs.some((tab) => tab.key === activeTab);
    const expanded = expandedOf(g);
    return (
      <div key={g.key} className="tab-group-wrap">
        <button
          type="button"
          className={`tab tab-group${containsActive ? ' tab-group--contains-active' : ''}`}
          onClick={() => setManual((m) => ({ ...m, [g.key]: !expanded }))}
          title={sidebarOpen ? undefined : t(g.labelKey)}
          aria-expanded={expanded}
        >
          {/* 组图标只在折叠窄条（rail）模式显示：展开态组头是纯文字 section 标签 */}
          <span className="tab-ico tab-group-ico" aria-hidden="true"><TabIcon name={g.icon} /></span>
          <span className="tab-txt">{t(g.labelKey)}</span>
          {/* 箭头只渲染一个朝向，展开态由 CSS 旋转（与面板内其他折叠控件一致） */}
          <span className="tab-caret" aria-hidden="true">▸</span>
        </button>
        {expanded && g.tabs.map((tab) => renderTab(tab, 'tab-sub'))}
      </div>
    );
  };

  return (
    <aside className={`sidebar${sidebarOpen ? '' : ' sidebar-collapsed'}`}>
      <nav className="tabs" role="tablist" aria-label="主导航" ref={navRef} onKeyDown={onNavKeyDown}>
        {MAIN_TABS.map((tab, i) => renderTab(tab, '', `${modKey}${i + 1}`))}
        {groups.map(renderGroup)}
      </nav>
      <div className="sidebar-foot">
        <div className="sidebar-tip">{t('app.connectHint')}</div>
        <button
          type="button"
          className="sidebar-toggle"
          onClick={toggleSidebar}
          title={toggleTitle}
          aria-label={toggleTitle}
        >
          <span className="sidebar-toggle-ico" aria-hidden="true">{sidebarOpen ? '◀' : '▶'}</span>
          {sidebarOpen && <span className="sidebar-toggle-txt">{toggleTitle}</span>}
        </button>
      </div>
    </aside>
  );
}
