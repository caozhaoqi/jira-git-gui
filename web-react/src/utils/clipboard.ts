// 剪贴板（三端统一），复刻 web/js/01-core.js 的 readClipboardText / writeClipboardText。
// Electron 走 preload 暴露的原生 clipboard；Tauri 走 Rust clipboard 插件；
// 纯 Web 回退浏览器 navigator.clipboard（受页面权限限制，需用户授权）。

interface ElectronAPI {
  isElectron?: boolean;
  readClipboardText?: () => Promise<string>;
  writeClipboardText?: (text: string) => Promise<void>;
}

declare global {
  interface Window {
    electronAPI?: ElectronAPI;
    __TAURI__?: {
      core?: {
        invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
      };
    };
  }
}

export async function readClipboardText(): Promise<string> {
  if (window.electronAPI?.isElectron && window.electronAPI.readClipboardText) {
    return window.electronAPI.readClipboardText();
  }
  if (window.__TAURI__?.core) {
    return (await window.__TAURI__.core.invoke('plugin:clipboard-manager|read_text')) as string;
  }
  if (navigator.clipboard && navigator.clipboard.readText) {
    return navigator.clipboard.readText();
  }
  throw new Error('当前环境不支持剪贴板读取 API');
}

export async function writeClipboardText(text: string): Promise<void> {
  if (window.electronAPI?.isElectron && window.electronAPI.writeClipboardText) {
    await window.electronAPI.writeClipboardText(text);
    return;
  }
  if (window.__TAURI__?.core) {
    await window.__TAURI__.core.invoke('plugin:clipboard-manager|write_text', { text });
    return;
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
  }
}
