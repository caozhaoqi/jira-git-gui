import { useAppStore } from '../store/useAppStore';
import type { TabKey } from '../store/useAppStore';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'repo', label: '仓库 / 文件' },
  { key: 'commits', label: '提交记录' },
  { key: 'diff', label: '差异对比' },
  { key: 'k8s', label: 'K8s 运维' },
  { key: 'cf', label: 'CF 日志' },
];

export function Tabs() {
  const activeTab = useAppStore((s) => s.activeTab);
  const setTab = useAppStore((s) => s.setTab);
  return (
    <nav className="tabs">
      {TABS.map((t) => (
        <button
          key={t.key}
          className={`tab ${activeTab === t.key ? 'active' : ''}`}
          data-tab={t.key}
          onClick={() => setTab(t.key)}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}
