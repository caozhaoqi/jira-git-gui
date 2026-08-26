import { useEffect, useRef } from 'react';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';

export function LogPanel() {
  const logs = useAppStore((s) => s.logs);
  const clearLogs = useAppStore((s) => s.clearLogs);
  const { t } = useT();
  const ref = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [logs]);

  return (
    <section className="logs-pane">
      <div className="panel-header">
        <h2 className="section-title">{t('log.title')}</h2>
        <button className="btn btn-sm btn-ghost" onClick={clearLogs}>
          {t('log.clear')}
        </button>
      </div>
      <pre className="log-block" ref={ref}>
        {logs.length === 0 ? (
          <span className="empty-hint" style={{ display: 'block', padding: 12 }}>
            {t('log.empty')}
          </span>
        ) : (
          logs.map((l, i) => (
            <div key={i} className={`log-line ${l.level}`}>
              {l.msg}
            </div>
          ))
        )}
      </pre>
    </section>
  );
}
