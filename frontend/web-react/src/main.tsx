import React from 'react';
import ReactDOM from 'react-dom/client';
import Root from './Root';
import { LogViewer } from './components/LogViewer';
import { ServicesConfig } from './components/ServicesConfig';
import { KibanaSitesView } from './components/kibana/KibanaFilters';
import { ErrorBoundary } from './components/ErrorBoundary';
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
// ?view=kibana-sites 为「Kibana 站点管理」独立窗口（页内小弹窗看不全编辑表单）。
const view = new URLSearchParams(location.search).get('view');

const Entry =
  view === 'log' ? LogViewer
  : view === 'services-config' ? ServicesConfig
  : view === 'kibana-sites' ? KibanaSitesView
  : Root;

if (view === 'log') {
  document.title = '日志查看 · K8s';
} else if (view === 'services-config') {
  document.title = '首选项 · 系统配置';
} else if (view === 'kibana-sites') {
  document.title = 'Kibana 站点管理';
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* 顶层兜底：任何未被区域级边界捕获的渲染异常都会在这里变成可读的错误卡片，
        而不是整页白屏（面板永久挂载策略会放大单点故障，见 ErrorBoundary 注释）。 */}
    <ErrorBoundary>
      <Entry />
    </ErrorBoundary>
  </React.StrictMode>
);
