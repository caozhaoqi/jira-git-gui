import { useAppStore } from '../store/useAppStore';

export function ProgressBar() {
  const progress = useAppStore((s) => s.progress);
  if (!progress.visible) return null;

  const isError = progress.mode === 'error';
  const isDone = progress.mode === 'done';
  const pct =
    progress.mode === 'determinate' ? Math.max(0, Math.min(100, progress.pct)) : 0;

  return (
    <div className={`progress-wrap ${isError ? 'error' : ''} ${isDone ? 'done' : ''}`}>
      <div className="progress-inner">
        <span className="progress-stage">{progress.stage}</span>
        {progress.mode === 'determinate' && (
          <span className="progress-pct">{pct}%</span>
        )}
      </div>
      {progress.mode === 'determinate' && (
        <progress className="progress-bar" max={100} value={pct} />
      )}
      {progress.mode === 'indeterminate' && (
        <progress className="progress-bar indeterminate" />
      )}
      {progress.detail && <div className="progress-detail">{progress.detail}</div>}
    </div>
  );
}
