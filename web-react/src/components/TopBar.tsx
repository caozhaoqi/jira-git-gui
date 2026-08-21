import { useAppStore } from '../store/useAppStore';

export function TopBar({ onOpenConnect }: { onOpenConnect: () => void }) {
  const status = useAppStore((s) => s.status);
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);

  const credOk = !!(status?.cookie_set || status?.pat_set);

  return (
    <header className="appbar">
      <div className="appbar-brand">
        <span className="brand-mark" aria-hidden="true">
          🌿
        </span>
        <span className="brand-name">
          Jira&nbsp;Git <b>GUI</b>
        </span>
      </div>
      <div className="appbar-actions">
        <span className="appbar-status" title="当前连接状态">
          {(status?.mode || '-').toUpperCase()} · {status?.repo_id || '未选仓库'}
        </span>
        <button className="btn btn-ghost" onClick={onOpenConnect} title="连接设置">
          <span>⚙</span>
          <span className="lbl">连接设置</span>
        </button>
        <span
          className={`status-dot ${credOk ? 'ok' : 'warn'}`}
          title={credOk ? '后端已连接，凭证已配置' : '后端未配置凭证'}
        />
        <button
          className="btn btn-icon"
          onClick={toggleTheme}
          title="切换浅色 / 深色主题"
        >
          {theme === 'dark' ? '☀' : '🌓'}
        </button>
      </div>
    </header>
  );
}
