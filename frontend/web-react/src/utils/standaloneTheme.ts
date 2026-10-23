/**
 * 独立轻窗口的主题同步。
 *
 * 为什么需要
 * ----------
 * 主窗口的主题由 store 统一落地（给 ``<html>`` / ``<body>`` 加 ``.dark`` 类）。
 * 但 ``?hcm-table`` / ``?hcm-detail`` / ``?hcm-cf-err`` 这类独立窗口渲染的是
 * 独立根组件，**不经过 App / store 的 UI 初始化路径**，于是暗色主题下它们
 * 仍然显示浅色（实测：深色模式打开「新窗口」表格，整个窗口是白的，很割裂）。
 *
 * 这里直接读 localStorage 里同一个 key 落地类名；同时监听 storage 事件，
 * 主窗口切主题时已经打开的独立窗口也能跟着变。
 */
import { useEffect } from 'react';

const THEME_KEY = 'jgg-theme';

function apply(theme: string) {
  const dark = theme === 'dark';
  document.documentElement.classList.toggle('dark', dark);
  document.body.classList.toggle('dark', dark);
}

export function applyStandaloneTheme(): void {
  let theme = 'light';
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === 'dark' || v === 'light') theme = v;
  } catch {
    /* ignore */
  }
  apply(theme);
}

/** 在独立窗口组件里调用一次：立即应用 + 跟随主窗口切换。 */
export function useStandaloneTheme(): void {
  useEffect(() => {
    applyStandaloneTheme();
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_KEY && e.newValue) apply(e.newValue);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);
}
