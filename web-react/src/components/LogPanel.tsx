import { useEffect, useRef } from 'react';
import { useAppStore } from '../store/useAppStore';

export function LogPanel() {
  const logs = useAppStore((s) => s.logs);
  const clearLogs = useAppStore((s) => s.clearLogs);
  const ref = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [logs]);

  return (
    <section className="logs-pane">
      <div className="panel-header">
        <h2 className="section-title">日志</h2>
        <button className="btn btn-sm btn-ghost" onClick={clearLogs}>
          清空
        </button>
      </div>
      <pre className="log-block" ref={ref}>
        {logs.length === 0 ? (
          <span className="empty-hint" style={{ display: 'block', padding: 12 }}>
            尚无日志。克隆 / 下载 / 扫描过程会在此输出。
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
