import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { LogViewer } from './components/LogViewer';
import './styles/global.css';
import './styles/panels.css';
import './styles/shell.css';
import './styles/logviewer.css';

// 视图路由：原生版把日志查看器做成独立 HTML（web/log_viewer.html）。
// React 版统一在同一个 SPA 内，用 ?view=log 切换到全屏日志视图，
// 从而保留「新窗口打开、可同时开多个 Pod」的使用方式。
const view = new URLSearchParams(location.search).get('view');

const Root = view === 'log' ? LogViewer : App;

if (view === 'log') {
  document.title = '日志查看 · K8s';
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
