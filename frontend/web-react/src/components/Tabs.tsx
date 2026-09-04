import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';
import { useT } from '../i18n';

const TABS: { key: TabKey; icon: string; labelKey: string }[] = [
  { key: 'repo', icon: '📦', labelKey: 'tab.repo' },
  { key: 'diff', icon: '🔀', labelKey: 'tab.diff' },
  { key: 'logs', icon: '📋', labelKey: 'tab.logs' },
  { key: 'k8s', icon: '☸', labelKey: 'tab.k8s' },
  { key: 'cf', icon: '🔬', labelKey: 'tab.cf' },
  { key: 'cfdebug', icon: '🐞', labelKey: 'tab.cfdebug' },
  { key: 'clash', icon: '🛰', labelKey: 'tab.clash' },
  { key: 'hcm', icon: '🗂', labelKey: 'tab.hcm' },
  { key: 'diagnose', icon: '🔍', labelKey: 'tab.diagnose' },
  { key: 'settings', icon: '⚙', labelKey: 'tab.settings' },
];

export function Tabs() {
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  const sidebarOpen = useAppStore((s) => s.sidebarOpen);
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const { t } = useT();
  const toggleTitle = sidebarOpen ? t('app.sidebarCollapse') : t('app.sidebarExpand');
  return (
    <aside className={`sidebar${sidebarOpen ? '' : ' sidebar-collapsed'}`}>
      <nav className="tabs" aria-label="主导航">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={`tab ${activeTab === tab.key ? 'active' : ''}`}
            data-tab={tab.key}
            onClick={() => setTab(tab.key)}
            title={sidebarOpen ? undefined : t(tab.labelKey)}
          >
            <span className="tab-ico">{tab.icon}</span>
            <span className="tab-txt">{t(tab.labelKey)}</span>
          </button>
        ))}
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