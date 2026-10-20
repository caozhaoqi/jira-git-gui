export type DesktopWindowAction = 'minimize' | 'toggle-maximize' | 'close';

export function isDesktopApp(): boolean {
  return Boolean(window.electronAPI?.isElectron || window.__TAURI__?.window);
}

export function isMacDesktopApp(): boolean {
  const platform = window.electronAPI?.platform || navigator.platform || navigator.userAgent;
  return /darwin|mac/i.test(platform);
}

export async function controlDesktopWindow(action: DesktopWindowAction): Promise<void> {
  const electron = window.electronAPI;
  if (electron?.isElectron) {
    const control = {
      minimize: electron.minimizeWindow,
      'toggle-maximize': electron.toggleMaximizeWindow,
      close: electron.closeWindow,
    }[action];
    if (!control) throw new Error(`Electron window control is unavailable: ${action}`);
    await control();
    return;
  }

  const getCurrentWindow = window.__TAURI__?.window?.getCurrentWindow;
  if (getCurrentWindow) {
    const current = getCurrentWindow();
    if (action === 'minimize') await current.minimize();
    else if (action === 'toggle-maximize') await current.toggleMaximize();
    else await current.close();
    return;
  }

  throw new Error('Desktop window controls are unavailable in this environment');
}
