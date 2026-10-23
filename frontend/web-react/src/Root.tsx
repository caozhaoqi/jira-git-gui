import { useEffect } from 'react';
import App from './App';
import { HcmModelDetail } from './components/hcm/HcmModelDetail';
import { HcmCloudFuncErrorLocator } from './components/hcm/HcmCloudFuncErrorLocator';
import { HcmTableWindow } from './components/hcm/HcmTableWindow';
import { applyStandaloneTheme } from './utils/standaloneTheme';

// 三种「独立轻窗口」根组件：双击对象打开的模型详情 / 元数据窗口、云函数错误定位窗口。
// 抽成独立根组件，由 main.tsx 按 URL 查询串选择渲染，避免在 App 内用条件 return
// 提前退出导致后续 Hook 被跳过（URL 查询串一旦变化、Hook 数量突变会白屏）。

function DetailWindow() {
  return (
    <div className="app-shell app-shell--detail">
      <main className="workspace">
        <div className="workspace-body">
          <HcmModelDetail />
        </div>
      </main>
    </div>
  );
}

function TableWindow() {
  return (
    <div className="app-shell app-shell--detail">
      <main className="workspace">
        <div className="workspace-body">
          <HcmTableWindow />
        </div>
      </main>
    </div>
  );
}

function CfErrWindow() {
  return (
    <div className="app-shell app-shell--detail">
      <main className="workspace">
        <div className="workspace-body">
          <HcmCloudFuncErrorLocator />
        </div>
      </main>
    </div>
  );
}

export default function Root() {
  const qs = new URLSearchParams(window.location.search);

  // 独立轻窗口不经过 App/store 的主题初始化：这里统一补上类名，
  // 否则暗色主题下这些窗口会是白底（与主窗口割裂）。
  const standalone =
    qs.has('hcm-detail') || qs.has('hcm-meta') ||
    qs.has('hcm-table') || qs.has('hcm-cf-err');
  useEffect(() => {
    if (standalone) applyStandaloneTheme();
  }, [standalone]);

  // Root 自身不依赖条件 Hook，条件 return 安全（useEffect 已在 return 之前无条件调用）。
  if (qs.has('hcm-detail') || qs.has('hcm-meta')) return <DetailWindow />;
  if (qs.has('hcm-table')) return <TableWindow />;
  if (qs.has('hcm-cf-err')) return <CfErrWindow />;
  return <App />;
}
