import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';

const TABS: { key: TabKey; icon: string; label: string }[] = [
  { key: 'repo', icon: '📦', label: '仓库' },
  // { key: 'commits', icon: '📜', label: '提交记录' },
  { key: 'diff', icon: '🔀', label: '差异对比' },
  { key: 'logs', icon: '📋', label: '日志' },
  { key: 'k8s', icon: '☸', label: 'K8s 快照' },
  { key: 'cf', icon: '🔬', label: '云函数日志' },
];

export function Tabs() {
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  return (
    <aside className="sidebar">
      <nav className="tabs" aria-label="主导航">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`tab ${activeTab === t.key ? 'active' : ''}`}
            data-tab={t.key}
            onClick={() => setTab(t.key)}
          >
            <span className="tab-ico">{t.icon}</span>
            <span className="tab-txt">{t.label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className="sidebar-tip">提示：先「连接设置」→ 选仓库 → 看文件 / 比差异</div>
      </div>
    </aside>
  );
}
