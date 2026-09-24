import React from 'react';
import ReactDOM from 'react-dom/client';
import Root from './Root';
import { LogViewer } from './components/LogViewer';
import { ServicesConfig } from './components/ServicesConfig';
import './styles/global.css';
import './styles/panels.css';
import './styles/shell.css';
import './styles/logviewer.css';
import './styles/cfdebug.css';
import './styles/services-config.css';

// 视图路由：原生版把日志查看器做成独立 HTML（web/log_viewer.html）。
// React 版统一在同一个 SPA 内，用 ?view=log 切换到全屏日志视图，
// 从而保留「新窗口打开、可同时开多个 Pod」的使用方式。
// 同理 ?view=services-config 为独立「首选项」窗口（迁移自 web/services-config.html）。
const view = new URLSearchParams(location.search).get('view');

const Entry =
  view === 'log' ? LogViewer : view === 'services-config' ? ServicesConfig : Root;

if (view === 'log') {
  document.title = '日志查看 · K8s';
} else if (view === 'services-config') {
  document.title = '首选项 · 系统配置';
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Entry />
  </React.StrictMode>
);
