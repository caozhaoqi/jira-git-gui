import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';
import { useT } from '../i18n';

type TabDef = { key: TabKey; icon: string; labelKey: string };

// 高频页签：平铺展示
const MAIN_TABS: TabDef[] = [
  { key: 'repo', icon: '📦', labelKey: 'tab.repo' },
  { key: 'diff', icon: '🔀', labelKey: 'tab.diff' },
  { key: 'k8s', icon: '☸', labelKey: 'tab.k8s' },
  { key: 'kibana', icon: '📈', labelKey: 'tab.kibana' },
  { key: 'cf', icon: '🔬', labelKey: 'tab.cf' },
  { key: 'cfdebug', icon: '🐞', labelKey: 'tab.cfdebug' },
  { key: 'hcm', icon: '🗂', labelKey: 'tab.hcm' },
];

// 低频页签：Electron 下收进原生菜单（应用菜单「首选项…」区域，Cmd/Ctrl+Alt+1..4）；
// 纯浏览器模式没有原生菜单，保留侧栏「系统」折叠分组兜底。
const SYS_TABS: TabDef[] = [
  { key: 'logs', icon: '📋', labelKey: 'tab.logs' },
  { key: 'clash', icon: '🛰', labelKey: 'tab.clash' },
  { key: 'diagnose', icon: '🔍', labelKey: 'tab.diagnose' },
  { key: 'settings', icon: '⚙', labelKey: 'tab.settings' },
];

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

  // 「系统」分组折叠态；激活页签落在组内时强制展开，保证高亮项可见
  const [sysOpen, setSysOpen] = useState(false);
  const activeInSys = SYS_TABS.some((tab) => tab.key === activeTab);
  const sysExpanded = !isElectron && (sysOpen || activeInSys);
  const toggleTitle = sidebarOpen ? t('app.sidebarCollapse') : t('app.sidebarExpand');

  const renderTab = (tab: TabDef, extraClass = '') => (
    <button
      key={tab.key}
      className={`tab ${extraClass} ${activeTab === tab.key ? 'active' : ''}`}
      data-tab={tab.key}
      onClick={() => setTab(tab.key)}
      title={sidebarOpen ? undefined : t(tab.labelKey)}
    >
      <span className="tab-ico">{tab.icon}</span>
      <span className="tab-txt">{t(tab.labelKey)}</span>
    </button>
  );

  return (
    <aside className={`sidebar${sidebarOpen ? '' : ' sidebar-collapsed'}`}>
      <nav className="tabs" aria-label="主导航">
        {MAIN_TABS.map((tab) => renderTab(tab))}
        {!isElectron && (
          <button
            type="button"
            className={`tab tab-group${activeInSys ? ' active' : ''}`}
            onClick={() => setSysOpen((o) => !o)}
            title={sidebarOpen ? undefined : t('tab.system')}
            aria-expanded={sysExpanded}
          >
            <span className="tab-ico">🛠</span>
            <span className="tab-txt">{t('tab.system')}</span>
            <span className="tab-caret">{sysExpanded ? '▾' : '▸'}</span>
          </button>
        )}
        {sysExpanded && SYS_TABS.map((tab) => renderTab(tab, 'tab-sub'))}
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
          <span className="sidebar-toggle-ico">{sidebarOpen ? '◀' : '▶'}</span>
          {sidebarOpen && <span className="sidebar-toggle-txt">{toggleTitle}</span>}
        </button>
      </div>
    </aside>
  );
}
