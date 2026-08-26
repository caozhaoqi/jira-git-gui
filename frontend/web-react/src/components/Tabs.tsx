import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';
import { useT } from '../i18n';

const TABS: { key: TabKey; icon: string; labelKey: string }[] = [
  { key: 'repo', icon: '📦', labelKey: 'tab.repo' },
  { key: 'diff', icon: '🔀', labelKey: 'tab.diff' },
  { key: 'logs', icon: '📋', labelKey: 'tab.logs' },
  { key: 'k8s', icon: '☸', labelKey: 'tab.k8s' },
  { key: 'cf', icon: '🔬', labelKey: 'tab.cf' },
  { key: 'clash', icon: '🛰', labelKey: 'tab.clash' },
  { key: 'hcm', icon: '🗂', labelKey: 'tab.hcm' },
  { key: 'settings', icon: '⚙', labelKey: 'tab.settings' },
];

export function Tabs() {
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  const { t } = useT();
  return (
    <aside className="sidebar">
      <nav className="tabs" aria-label="主导航">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={`tab ${activeTab === tab.key ? 'active' : ''}`}
            data-tab={tab.key}
            onClick={() => setTab(tab.key)}
          >
            <span className="tab-ico">{tab.icon}</span>
            <span className="tab-txt">{t(tab.labelKey)}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className="sidebar-tip">{t('app.connectHint')}</div>
      </div>
    </aside>
  );
}
