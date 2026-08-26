import { useEffect } from 'react';
import { useAppStore } from '../store/useAppStore';
import type { Toast as ToastModel } from '../store/useAppStore';

const ICONS: Record<string, string> = {
  success: '✓',
  warn: '!',
  error: '✕',
  info: 'i',
};

function ToastItem({ toast }: { toast: ToastModel }) {
  const removeToast = useAppStore((s) => s.removeToast);
  useEffect(() => {
    if (toast.duration > 0) {
      const t = setTimeout(() => removeToast(toast.id), toast.duration);
      return () => clearTimeout(t);
    }
  }, [toast.id, toast.duration, removeToast]);

  return (
    <div className={`toast ${toast.type}`}>
      <span className="toast-icon">{ICONS[toast.type] || 'i'}</span>
      <div className="toast-body">
        <span>{toast.message}</span>
        {toast.action && (
          <div className="toast-actions">
            <button
              className={toast.action.primary ? 'primary' : ''}
              onClick={() => {
                toast.action!.onClick();
                removeToast(toast.id);
              }}
            >
              {toast.action.label}
            </button>
          </div>
        )}
      </div>
      <button className="toast-close" onClick={() => removeToast(toast.id)}>
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
