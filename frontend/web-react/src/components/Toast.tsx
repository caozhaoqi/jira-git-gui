import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import type { Toast as ToastModel } from '../store/useAppStore';

const ICONS: Record<string, string> = {
  success: '✓',
  warn: '!',
  error: '✕',
  info: 'i',
};

// 退出动画时长，需与 shell.css 的 toast-out 动画时长保持一致
const LEAVE_MS = 220;

function ToastItem({ toast }: { toast: ToastModel }) {
  const removeToast = useAppStore((s) => s.removeToast);
  const [leaving, setLeaving] = useState(false);

  // 自动消失：先播退出动画，动画结束再真正移除 DOM（避免消息突然消失）。
  // duration<=0 表示常驻（错误类默认常驻，需手动关闭）。
  useEffect(() => {
    if (toast.duration > 0 && !leaving) {
      const t = setTimeout(() => setLeaving(true), toast.duration);
      return () => clearTimeout(t);
    }
  }, [toast.id, toast.duration, leaving, removeToast]);

  // 退出动画播完 → 真正移除
  useEffect(() => {
    if (!leaving) return undefined;
    const t = setTimeout(() => removeToast(toast.id), LEAVE_MS);
    return () => clearTimeout(t);
  }, [leaving, toast.id, removeToast]);

  // 统一出口：关闭 / 动作点击都先进入 leaving 态播放退出动画
  const dismiss = () => setLeaving(true);

  return (
    <div className={`toast ${toast.type}${leaving ? ' leaving' : ''}`}>
      <span className="toast-icon">{ICONS[toast.type] || 'i'}</span>
      <div className="toast-body">
        <span>{toast.message}</span>
        {toast.action && (
          <div className="toast-actions">
            <button
              type="button"
              className={`btn btn-sm${toast.action.primary ? ' btn-primary' : ''}`}
              onClick={() => {
                toast.action!.onClick();
                dismiss();
              }}
            >
              {toast.action.label}
            </button>
          </div>
        )}
      </div>
      <button type="button" className="toast-close" onClick={dismiss} aria-label="关闭">
        ×
      </button>
    </div>
  );
}

export function ToastStack() {
  const toasts = useAppStore((s) => s.toasts);
  if (!toasts.length) return null;
  return (
    <div className="toast-stack" id="toast-stack">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} />
      ))}
    </div>
  );
}
