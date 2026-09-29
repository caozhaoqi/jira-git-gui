/**
 * Electron Preload —— 通过 contextBridge 暴露安全 API 给渲染进程。
 *
 * 主要：
 *  - electronAPI.log(level, msg) 把前端日志 -> 主进程统一落盘
 *  - electronAPI.onAppLog(cb)  主进程/Python 日志 -> 前端 UI 日志面板
 *  - electronAPI.getAppInfo()   读取平台/日志文件路径等基础信息
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  platform: process.platform,
  isElectron: true,

  log(level, msg) {
    ipcRenderer.send('log:from-renderer', {
      level: level || 'info',
      msg: msg ?? '',
    });
  },

  getAppInfo() {
    return ipcRenderer.invoke('app:get-info');
  },

  openExternal(url) {
    return ipcRenderer.invoke('shell:open-external', String(url || ''));
  },

  /** 多开窗口：独立窗口打开指定页签（cf = 云函数日志），用于同时查看多个环境日志 */
  openTabWindow(tab, title) {
    return ipcRenderer.invoke('window:open-tab', {
      tab: String(tab || ''),
      title: String(title || ''),
    });
  },

  /** 独立窗口打开应用内任意 URL 路径（如 /?view=kibana-sites 站点管理器），与 openTabWindow 同款；maximize=true 铺满工作区 */
  openAppWindow(path, title, maximize) {
    return ipcRenderer.invoke('window:open-url', {
      path: String(path || '/'),
      title: String(title || ''),
      maximize: Boolean(maximize),
    });
  },

  /** 原生目录选择对话框，返回所选绝对路径；取消返回 null */
  openDirectory(title, defaultPath) {
    return ipcRenderer.invoke('dialog:open-directory', {
      title: String(title || ''),
      defaultPath: String(defaultPath || ''),
    });
  },

  /** 主窗口上报当前激活页签，供原生菜单「新窗口打开当前功能」使用 */
  setActiveTab(tab) {
    ipcRenderer.send('window:active-tab', String(tab || ''));
  },

  /** 在内置浏览器窗口（应用内嵌 Chromium）打开外部 HCM Cloud 网页，可选注入 cookie 自动登录。 */
  openBuiltinBrowser(url, cookies) {
    return ipcRenderer.invoke('builtin-browser:open', {
      url: String(url || ''),
      cookies: Array.isArray(cookies) ? cookies : [],
    });
  },

  /** 读取系统剪贴板纯文本（Electron 原生模块，绕过浏览器权限） */
  readClipboardText() {
    return ipcRenderer.invoke('clipboard:read-text');
  },

  /** 写入系统剪贴板纯文本 */
  writeClipboardText(text) {
    return ipcRenderer.invoke('clipboard:write-text', text);
  },

  /** 原生菜单跳转页签（日志 / Clash 分流 / 统一诊断 / 系统设置） @param {(tab: string) => void} cb */
  onNavTab(cb) {
    const handler = (_ev, payload) => {
      try { cb(payload && payload.tab); } catch (_) {}
    };
    ipcRenderer.on('nav:tab', handler);
    // 返回注销函数
    return () => ipcRenderer.removeListener('nav:tab', handler);
  },

  /** @param {(text: string) => void} cb */
  onAppLog(cb) {
    const handler = (_ev, payload) => {
      try { cb(payload.text); } catch (_) {}
    };
    ipcRenderer.on('log:append', handler);
    // 返回注销函数
    return () => ipcRenderer.removeListener('log:append', handler);
  },
});
