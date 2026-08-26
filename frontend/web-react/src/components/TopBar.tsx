import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { LOCALES } from '../i18n/types';

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
          🌿
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
          <span>⚙</span>
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
          {theme === 'dark' ? '☀' : '🌓'}
        </button>
      </div>
    </header>
  );
}
