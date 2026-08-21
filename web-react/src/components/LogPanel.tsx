import { useEffect, useRef, useState } from 'react';
import { useAppStore } from '../store/useAppStore';

export function LogPanel() {
  const logs = useAppStore((s) => s.logs);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [logs, open]);

  return (
    <section className={`log-panel ${open ? 'open' : ''}`}>
      <div className="log-header" onClick={() => setOpen((v) => !v)}>
        <span>日志 {logs.length > 0 && `(${logs.length})`}</span>
        <span className="log-toggle">{open ? '▾ 收起' : '▴ 展开'}</span>
      </div>
      {open && (
        <div className="log-content" ref={ref} id="log-content">
          {logs.map((l, i) => (
            <div key={i} className={`log-line ${l.level}`}>
              {l.msg}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
