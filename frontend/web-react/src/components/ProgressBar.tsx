import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';

export function ProgressBar() {
  const progress = useAppStore((s) => s.progress);
  const setProgress = useAppStore((s) => s.setProgress);
  const { t } = useT();
  if (!progress.visible) return null;

  const isError = progress.mode === 'error';
  const isDone = progress.mode === 'done';
  const pct =
    progress.mode === 'determinate' ? Math.max(0, Math.min(100, progress.pct)) : 0;

  return (
    <div
      className={`progress-wrap ${isError ? 'error' : ''} ${isDone ? 'done' : ''}`}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
    >
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
      {progress.eta && <div className="progress-eta">⏱ 预计剩余 {progress.eta}</div>}
      {/* 终态（成功/失败）需要一个明确的关闭动作：否则错误横幅会一直挂在窗口底部 */}
      {(isError || isDone) && (
        <button
          className="progress-close"
          onClick={() => setProgress({ visible: false })}
          aria-label={t('common.close')}
          title={t('common.close')}
        >
          ✕
        </button>
      )}
    </div>
  );
}
