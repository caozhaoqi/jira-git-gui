import { create } from 'zustand';

export interface ConfirmOptions {
  /** 弹窗正文（已由调用方完成 i18n） */
  message: string;
  /** 可选标题；不传则 ConfirmHost 回退到 t('common.confirm') */
  title?: string;
  /** 确认按钮文案；不传回退到 t('common.confirm') */
  confirmText?: string;
  /** 取消按钮文案；不传回退到 t('common.cancel') */
  cancelText?: string;
  /** 是否为危险操作（删除等）；为 true 时确认按钮用 btn-danger 红色样式 */
  danger?: boolean;
}

interface ConfirmState {
  open: boolean;
  title?: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  danger: boolean;
  resolve?: (ok: boolean) => void;
  /** 弹出确认框，返回 Promise<boolean>（true=确认 / false=取消或 Esc） */
  requestConfirm: (opts: ConfirmOptions | string) => Promise<boolean>;
  /** ConfirmHost 内部调用：用户点确认 */
  confirm: () => void;
  /** ConfirmHost 内部调用：用户点取消 / 点遮罩 / 按 Esc */
  cancel: () => void;
}

export const useConfirmStore = create<ConfirmState>((set, get) => ({
  open: false,
  message: '',
  danger: false,
  requestConfirm: (opts) => {
    const norm: ConfirmOptions =
      typeof opts === 'string' ? { message: opts } : opts;
    return new Promise<boolean>((resolve) => {
      set({
        open: true,
        title: norm.title,
        message: norm.message,
        confirmText: norm.confirmText,
        cancelText: norm.cancelText,
        danger: norm.danger ?? false,
        resolve,
      });
    });
  },
  confirm: () => {
    const r = get().resolve;
    set({ open: false, resolve: undefined });
    r?.(true);
  },
  cancel: () => {
    const r = get().resolve;
    set({ open: false, resolve: undefined });
    r?.(false);
  },
}));

/** 命令式确认入口：可脱离 React 组件树在任意 async 函数里直接 await。 */
export function requestConfirm(opts: ConfirmOptions | string): Promise<boolean> {
  return useConfirmStore.getState().requestConfirm(opts);
}
