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

  /** 读取系统剪贴板纯文本（Electron 原生模块，绕过浏览器权限） */
  readClipboardText() {
    return ipcRenderer.invoke('clipboard:read-text');
  },

  /** 写入系统剪贴板纯文本 */
  writeClipboardText(text) {
    return ipcRenderer.invoke('clipboard:write-text', text);
  },

  getLogPath() {
    return ipcRenderer.invoke('log:get-path');
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
