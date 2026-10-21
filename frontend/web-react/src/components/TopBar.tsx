import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { LOCALES } from '../i18n/types';
import { TabIcon } from './icons';
import {
  controlDesktopWindow,
  isDesktopApp,
  isMacDesktopApp,
  type DesktopWindowAction,
} from '../utils/desktopWindow';

function WindowControls({ mac = false }: { mac?: boolean }) {
  const { t } = useT();
  const controls: { action: DesktopWindowAction; label: string; icon: string }[] = mac
    ? [
        { action: 'close', label: 'app.windowClose', icon: 'close' },
        { action: 'minimize', label: 'app.windowMinimize', icon: 'minimize' },
        { action: 'toggle-maximize', label: 'app.windowToggleMaximize', icon: 'maximize' },
      ]
    : [
        { action: 'minimize', label: 'app.windowMinimize', icon: 'minimize' },
        { action: 'toggle-maximize', label: 'app.windowToggleMaximize', icon: 'maximize' },
        { action: 'close', label: 'app.windowClose', icon: 'close' },
      ];

  const run = (action: DesktopWindowAction) => {
    void controlDesktopWindow(action).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`Window control failed (${action}): ${message}`);
      window.electronAPI?.log?.('error', `Window control failed (${action}): ${message}`);
    });
  };

  return (
    <div className={`window-controls${mac ? ' window-controls--mac' : ' window-controls--desktop'}`}>
      {controls.map(({ action, label, icon }) => (
        <button
          key={action}
          type="button"
          className={`window-control window-control--${icon}`}
          onClick={() => run(action)}
          title={t(label)}
          aria-label={t(label)}
        >
          <svg viewBox="0 0 12 12" aria-hidden="true">
            {icon === 'close' && <path d="m3 3 6 6M9 3 3 9" />}
            {icon === 'minimize' && <path d="M2.5 6h7" />}
            {icon === 'maximize' && <rect x="2.5" y="2.5" width="7" height="7" rx=".7" />}
          </svg>
        </button>
      ))}
    </div>
  );
}

const WINDOWS_MENUS = [
  ['settings', 'app.menuSettings'],
  ['new-window', 'app.menuNewWindow'],
  ['edit', 'app.menuEdit'],
  ['view', 'app.menuView'],
  ['debug', 'app.menuDebug'],
  ['help', 'app.menuHelp'],
] as const;

function WindowsMenuBar() {
  const { t } = useT();

  const openMenu = (menuId: string, event: React.MouseEvent<HTMLButtonElement>) => {
    const showAppMenu = window.electronAPI?.showAppMenu;
    if (!showAppMenu) return;
    const rect = event.currentTarget.getBoundingClientRect();
    void showAppMenu(menuId, Math.round(rect.left), Math.round(rect.bottom)).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`Application menu failed (${menuId}): ${message}`);
      window.electronAPI?.log?.('error', `Application menu failed (${menuId}): ${message}`);
    });
  };

  return (
    <nav className="app-menu-bar" aria-label={t('app.menuBar')}>
      {WINDOWS_MENUS.map(([id, label]) => (
        <button key={id} type="button" onClick={(event) => openMenu(id, event)}>
          {t(label)}
        </button>
      ))}
    </nav>
  );
}

export function TopBar({ onOpenConnect }: { onOpenConnect: () => void }) {
  const status = useAppStore((s) => s.status);
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);
  const locale = useAppStore((s) => s.locale);
  const setLocale = useAppStore((s) => s.setLocale);
  const { t } = useT();
  const desktop = isDesktopApp();
  const macDesktop = desktop && isMacDesktopApp();
  const windowsElectron = window.electronAPI?.isElectron && window.electronAPI.platform === 'win32';

  const credOk = !!(status?.cookie_set || status?.pat_set);

  return (
    <header className={`appbar${desktop ? ' appbar--desktop' : ''}`}>
      {macDesktop && <WindowControls mac />}
      <div className="appbar-brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
            {/* 圆角渐变底 */}
            <defs>
              <linearGradient id="brand-grad" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
                <stop stopColor="#6d5ef2" />
                <stop offset="1" stopColor="#4f46e5" />
              </linearGradient>
            </defs>
            <rect x="1" y="1" width="22" height="22" rx="7" fill="url(#brand-grad)" />
            {/* 分支-合并图形：一笔主线 + 两条分叉，隐喻 git 拉取/合并 */}
            <path d="M7 8v7a3 3 0 0 0 3 3h3" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" opacity="0.95" />
            <circle cx="7" cy="7" r="1.9" fill="#fff" />
            <circle cx="17" cy="7" r="1.9" fill="#fff" />
            <path d="M7 7h4a3 3 0 0 1 3 3v3a3 3 0 0 0 3 3h1" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" opacity="0.95" />
          </svg>
        </span>
        <span className="brand-name">
          Jira&nbsp;Git <b>GUI</b>
        </span>
      </div>
      {windowsElectron && <WindowsMenuBar />}
      {desktop && (
        <div
          className="appbar-drag-region"
          data-tauri-drag-region=""
          onDoubleClick={() => {
            void controlDesktopWindow('toggle-maximize').catch((error: unknown) => {
              const message = error instanceof Error ? error.message : String(error);
              console.error(`Window maximize failed: ${message}`);
            });
          }}
          aria-hidden="true"
        />
      )}
      <div className="appbar-actions">
        <span className="appbar-status" title={t('app.statusTitle')}>
          {(status?.mode || '-').toUpperCase()} · {status?.repo_id || t('repo.noRepo')}
        </span>
        <button className="btn btn-ghost" onClick={onOpenConnect} title={t('app.connectSettings')}>
          <TabIcon name="settings" size={14} />
          <span className="lbl">{t('app.connectSettings')}</span>
        </button>
        <select
          className="locale-select"
          value={locale}
          onChange={(e) => setLocale(e.target.value as typeof locale)}
          title="Language / 言語"
          aria-label="Language"
        >
          {LOCALES.map((l) => (
            <option key={l.value} value={l.value}>
              {l.flag} {l.label}
            </option>
          ))}
        </select>
        <span
          className={`status-dot ${credOk ? 'ok' : 'warn'}`}
          title={credOk ? t('app.credOk') : t('app.credWarn')}
        />
        <button
          className="btn btn-icon"
          onClick={toggleTheme}
          title={t('app.themeToggle')}
        >
          <TabIcon name={theme === 'dark' ? 'sun' : 'moon'} size={14} />
        </button>
      </div>
      {desktop && !macDesktop && <WindowControls />}
    </header>
  );
}
