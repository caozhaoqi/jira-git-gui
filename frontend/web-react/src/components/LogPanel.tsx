import { useCallback, useEffect, useRef, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { requestConfirm } from '../utils/confirmStore';

export function LogPanel() {
  const logs = useAppStore((s) => s.logs);
  const clearLogs = useAppStore((s) => s.clearLogs);
  const addToast = useAppStore((s) => s.addToast);
  const { t } = useT();
  const ref = useRef<HTMLPreElement>(null);
  // 是否「跟随最新」：仅当视图贴在底部时才自动滚动。
  // 旧实现无条件 scrollTop = scrollHeight，导致日志由 SSE 持续推送时，
  // 用户向上翻看历史会被立刻拽回底部，根本没法读。
  const pinnedRef = useRef(true);
  const [pinned, setPinned] = useState(true);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atBottom !== pinnedRef.current) {
      pinnedRef.current = atBottom;
      setPinned(atBottom);
    }
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [logs]);

  const handleClear = async () => {
    if (!(await requestConfirm({ message: t('log.clearConfirm'), danger: true }))) return;
    clearLogs();
    addToast(t('log.cleared'), 'success');
  };

  const jumpToEnd = () => {
    const el = ref.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    pinnedRef.current = true;
    setPinned(true);
  };

  return (
    <section className="logs-pane">
      <div className="panel-header">
        <h2 className="section-title">{t('log.title')}</h2>
        <div className="panel-header-actions">
          <span className="log-count">{t('log.count', { n: logs.length })}</span>
          <button className="btn btn-sm btn-ghost" onClick={handleClear}>
            {t('log.clear')}
          </button>
        </div>
      </div>
      <div className="log-scroll-wrap">
        <pre className="log-block" ref={ref} onScroll={onScroll}>
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
        {!pinned && logs.length > 0 && (
          <button className="log-jump-end btn btn-sm btn-primary" onClick={jumpToEnd}>
            ↓ {t('log.jumpToLatest')}
          </button>
        )}
      </div>
    </section>
  );
}
