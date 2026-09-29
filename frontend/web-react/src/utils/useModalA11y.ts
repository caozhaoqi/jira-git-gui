import { useCallback, useEffect, useRef, useState } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * Modal 无障碍封装（WCAG 2.1 AA：对话框模式）。
 *
 * 调用方需在对话框元素上声明：
 *   role="dialog" aria-modal="true" aria-labelledby="<标题 id>" tabIndex={-1}
 * 本 hook 负责：
 *   - 打开时把焦点移入对话框（优先首个可聚焦元素，否则对话框自身）
 *   - Esc 关闭
 *   - Tab / Shift+Tab 焦点陷阱（焦点始终锁在对话框内）
 *   - 关闭时把焦点还原到打开前的元素
 *
 * 采用 callback ref：对话框元素挂载/卸载时自动建立/拆除陷阱，
 * 因此既适用于始终挂载的弹窗，也适用于条件渲染（{open && <Modal/>}）的弹窗。
 */
export function useModalA11y<T extends HTMLElement = HTMLDivElement>(
  onClose: () => void
): (node: T | null) => void {
  const [el, setEl] = useState<T | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const dialog = el;
    if (!dialog) return undefined;

    const previouslyFocused = document.activeElement as HTMLElement | null;

    const getFocusable = () =>
      Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (node) => node.offsetParent !== null || node === document.activeElement
      );

    const first = getFocusable()[0];
    if (first) first.focus();
    else dialog.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;

      const items = getFocusable();
      if (items.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;

      if (e.shiftKey) {
        if (active === firstEl || !dialog.contains(active)) {
          e.preventDefault();
          lastEl.focus();
        }
      } else if (active === lastEl || !dialog.contains(active)) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    dialog.addEventListener('keydown', onKeyDown);

    return () => {
      dialog.removeEventListener('keydown', onKeyDown);
      if (previouslyFocused && previouslyFocused.isConnected) {
        previouslyFocused.focus();
      }
    };
  }, [el]);

  return useCallback((node: T | null) => setEl(node), []);
}
