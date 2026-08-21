// 独立日志查看页的 URL 构造 / 打开。
//
// 原生实现是一个独立 HTML（web/log_viewer.html?pod=..&env=..）。React 版把它做成
// 同一个 SPA 的「独立视图」：main.tsx 检测 ?view=log 时只渲染 LogViewer，
// 不挂载主界面。这样既保留「新窗口打开、可多开」的能力，又复用同一套 API 客户端。
//
// base 用 import.meta.env.BASE_URL：dev 为 '/'，生产构建（--base /web/）为 '/web/'，
// 因此 Electron / Tauri / 浏览器三端都不需要区分。

export interface LogViewerParams {
  pod: string;
  env?: string;
  container?: string;
  namespace?: string;
}

export function logViewerUrl(p: LogViewerParams): string {
  const q = new URLSearchParams({ view: 'log', pod: p.pod });
  if (p.env) q.set('env', p.env);
  if (p.container) q.set('container', p.container);
  if (p.namespace) q.set('namespace', p.namespace);
  return `${import.meta.env.BASE_URL}?${q.toString()}`;
}

export function openLogViewer(p: LogViewerParams): void {
  window.open(logViewerUrl(p), '_blank');
}
