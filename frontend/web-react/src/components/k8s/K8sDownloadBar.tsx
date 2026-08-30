import { useT } from '../../i18n';
import { fmtSize } from '../../utils/format';
import { fmtEta, fmtSpeed, type DownloadProgress } from '../../utils/k8sFileDownload';

interface Props {
  /** 正在下载的文件名（仅用于展示） */
  name: string;
  prog: DownloadProgress;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  /** 收起进度条（结束/失败/取消后手动关闭） */
  onDismiss: () => void;
}

/**
 * 分片下载进度条：百分比 / 已下载 / 总量 / 速度 / 剩余时间 / 重试次数 + 暂停·继续·取消。
 *
 * total 未知时（stat 失败）percent 为 -1，此时走「不确定」动画条，
 * 只显示已下载字节数，避免进度条卡在 0% 让人以为没在动。
 */
export function K8sDownloadBar({ name, prog, onPause, onResume, onCancel, onDismiss }: Props) {
  const { t } = useT();
  const known = prog.total > 0 && prog.percent >= 0;
  const pct = known ? prog.percent : 0;
  const done = prog.status === 'done';
  const failed = prog.status === 'error';
  const paused = prog.status === 'paused';
  const finished = done || failed || prog.status === 'cancelled';

  const statusText = (() => {
    if (failed) return `${t('k8s.files.dlFailed')}${prog.message || ''}`;
    if (done) return t('k8s.files.dlDone');
    if (prog.status === 'cancelled') return t('k8s.files.dlCancelled');
    if (paused) return t('k8s.files.dlPaused');
    if (prog.status === 'preparing') return t('k8s.files.dlPreparing');
    return prog.message || t('k8s.files.dlDownloading');
  })();

  return (
    <div className={'k8s-dl' + (failed ? ' is-error' : done ? ' is-done' : paused ? ' is-paused' : '')}>
      <div className="k8s-dl-top">
        <span className="k8s-dl-name" title={name}>
          ⬇ {name}
        </span>
        <span className="k8s-dl-status">{statusText}</span>
        <div className="spacer" />
        {prog.retries > 0 && (
          <span className="k8s-dl-retry" title={t('k8s.files.dlRetried', { n: prog.retries })}>
            ⟳ {prog.retries}
          </span>
        )}
        {prog.status === 'preparing' && (
          <button className="btn btn-sm btn-ghost" onClick={onCancel}>
            ✕ {t('k8s.files.dlCancel')}
          </button>
        )}
        {!finished && prog.status !== 'preparing' && (
          <>
            {paused ? (
              <button className="btn btn-sm btn-primary" onClick={onResume}>
                ▶ {t('k8s.files.dlResume')}
              </button>
            ) : (
              <button className="btn btn-sm" onClick={onPause}>
                ⏸ {t('k8s.files.dlPause')}
              </button>
            )}
            <button className="btn btn-sm btn-ghost" onClick={onCancel}>
              ✕ {t('k8s.files.dlCancel')}
            </button>
          </>
        )}
        {finished && (
          <button className="btn btn-sm btn-ghost" onClick={onDismiss} title={t('k8s.files.dlDismiss')}>
            ✕
          </button>
        )}
      </div>

      <div className="k8s-dl-track">
        <div
          className={'k8s-dl-fill' + (known ? '' : ' is-indeterminate')}
          style={known ? { width: `${pct.toFixed(2)}%` } : undefined}
        />
      </div>

      <div className="k8s-dl-stats">
        <span className="k8s-dl-pct">{known ? `${pct.toFixed(1)}%` : '—'}</span>
        <span className="k8s-dl-bytes">
          {fmtSize(prog.received)}
          {prog.total > 0 ? ` / ${fmtSize(prog.total)}` : ''}
        </span>
        {prog.chunkTotal > 0 && (
          <span className="k8s-dl-chunk">
            {prog.chunkIndex}/{prog.chunkTotal}
          </span>
        )}
        <div className="spacer" />
        {!finished && !paused && <span className="k8s-dl-speed">{fmtSpeed(prog.speed)}</span>}
        {known && !finished && !paused && (
          <span className="k8s-dl-eta">
            {t('k8s.files.dlEta')} {fmtEta(prog.received, prog.total, prog.speed)}
          </span>
        )}
      </div>
    </div>
  );
}
