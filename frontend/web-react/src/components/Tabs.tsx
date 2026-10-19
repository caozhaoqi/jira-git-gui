import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';
import { useT } from '../i18n';

type TabDef = { key: TabKey; icon: string; labelKey: string };

// 折叠分组定义：同类功能收进一组（点击组头展开/收起），复用原「系统」分组的
// 交互与样式（tab-group / tab-sub）。组本身不是页面，不能加 .active（否则组与
// 子项同时高亮），用 --contains-active 修饰类标示「组内含当前页」的弱提示。
type TabGroup = { key: string; icon: string; labelKey: string; tabs: TabDef[]; electronHidden?: boolean };

// 高频页签：平铺展示（含两组内最高频的「云函数日志」「HCM 对象」）
const MAIN_TABS: TabDef[] = [
  { key: 'repo', icon: '📦', labelKey: 'tab.repo' },
  { key: 'diff', icon: '🔀', labelKey: 'tab.diff' },
  { key: 'cf', icon: '🔬', labelKey: 'tab.cf' },
  { key: 'hcm', icon: '🗂', labelKey: 'tab.hcm' },
  { key: 'k8s', icon: '☸', labelKey: 'tab.k8s' },
  { key: 'kibana', icon: '📈', labelKey: 'tab.kibana' },
];

// 同类功能分组：组内保留次高频项
const CF_GROUP: TabGroup = {
  key: 'group-cf', icon: '☁', labelKey: 'tab.groupCF',
  tabs: [
    { key: 'cfsync', icon: '🔄', labelKey: 'tab.cfsync' },
    { key: 'cfdebug', icon: '🐞', labelKey: 'tab.cfdebug' },
  ],
};
const HCM_GROUP: TabGroup = {
  key: 'group-hcm', icon: '🧩', labelKey: 'tab.groupHCM',
  tabs: [
    { key: 'hcmapi', icon: '🛠', labelKey: 'tab.hcmapi' },
    { key: 'hcmops', icon: '🧰', labelKey: 'tab.hcmops' },
  ],
};
const TAB_GROUPS: TabGroup[] = [CF_GROUP, HCM_GROUP];

// 低频页签：Electron 下收进原生菜单（应用菜单「首选项…」区域，Cmd/Ctrl+Alt+1..4）；
// 纯浏览器模式没有原生菜单，保留侧栏「系统」折叠分组兜底。
const SYS_GROUP: TabGroup = {
  key: 'group-sys', icon: '🛠', labelKey: 'tab.system', electronHidden: true,
  tabs: [
    { key: 'logs', icon: '📋', labelKey: 'tab.logs' },
    { key: 'clash', icon: '🛰', labelKey: 'tab.clash' },
    { key: 'diagnose', icon: '🔍', labelKey: 'tab.diagnose' },
    { key: 'settings', icon: '⚙', labelKey: 'tab.settings' },
  ],
};

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

  const renderTab = (tab: TabDef, extraClass = '') => (
    <button
      key={tab.key}
      className={`tab ${extraClass} ${activeTab === tab.key ? 'active' : ''}`}
      data-tab={tab.key}
      onClick={() => setTab(tab.key)}
      title={sidebarOpen ? undefined : t(tab.labelKey)}
      aria-current={activeTab === tab.key ? 'page' : undefined}
    >
      <span className="tab-ico" aria-hidden="true">{tab.icon}</span>
      <span className="tab-txt">{t(tab.labelKey)}</span>
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
          <span className="tab-ico" aria-hidden="true">{g.icon}</span>
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
      <nav className="tabs" aria-label="主导航">
        {MAIN_TABS.map((tab) => renderTab(tab))}
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
