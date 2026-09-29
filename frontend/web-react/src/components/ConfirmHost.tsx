import { useConfirmStore } from '../utils/confirmStore';
import { useT } from '../i18n';
import { useModalA11y } from '../utils/useModalA11y';

/**
 * 全局确认弹窗宿主：在 App 根部挂载一次，统一接管原先散落的 window.confirm。
 * 通过 useModalA11y 提供无障碍（焦点陷阱 / Esc 关闭 / 焦点还原）。
 * 业务侧只需 `if (!(await requestConfirm(...))) return;`。
 */
export function ConfirmHost() {
  const { t } = useT();
  const open = useConfirmStore((s) => s.open);
  const title = useConfirmStore((s) => s.title);
  const message = useConfirmStore((s) => s.message);
  const confirmText = useConfirmStore((s) => s.confirmText);
  const cancelText = useConfirmStore((s) => s.cancelText);
  const danger = useConfirmStore((s) => s.danger);
  const confirm = useConfirmStore((s) => s.confirm);
  const cancel = useConfirmStore((s) => s.cancel);

  const dialogRef = useModalA11y<HTMLDivElement>(cancel);

  if (!open) return null;

  return (
    <div className="modal-mask" onClick={cancel}>
      <div
        className="modal"
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-modal-title"
        aria-describedby="confirm-modal-msg"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3 id="confirm-modal-title">{title ?? t('common.confirm')}</h3>
          <button
            className="btn btn-icon"
            onClick={cancel}
            aria-label={t('common.close')}
          >
            ×
          </button>
        </div>
        <div className="modal-body">
          <p id="confirm-modal-msg" className="confirm-msg">
            {message}
          </p>
        </div>
        <div className="modal-footer">
          <button className="btn btn-ghost" onClick={cancel}>
            {cancelText ?? t('common.cancel')}
          </button>
          <button
            className={'btn ' + (danger ? 'btn-danger' : 'btn-primary')}
            onClick={confirm}
          >
            {confirmText ?? t('common.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
