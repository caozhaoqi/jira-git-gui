import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { LOCALES } from '../i18n/types';
import { TabIcon } from './icons';

export function TopBar({ onOpenConnect }: { onOpenConnect: () => void }) {
  const status = useAppStore((s) => s.status);
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);
  const locale = useAppStore((s) => s.locale);
  const setLocale = useAppStore((s) => s.setLocale);
  const { t } = useT();

  const credOk = !!(status?.cookie_set || status?.pat_set);

  return (
    <header className="appbar">
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
    </header>
  );
}
