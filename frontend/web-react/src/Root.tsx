import App from './App';
import { HcmModelDetail } from './components/hcm/HcmModelDetail';
import { HcmCloudFuncErrorLocator } from './components/hcm/HcmCloudFuncErrorLocator';

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
  // Root 自身不调用任何 Hook，条件 return 安全。
  const qs = new URLSearchParams(window.location.search);
  if (qs.has('hcm-detail') || qs.has('hcm-meta')) return <DetailWindow />;
  if (qs.has('hcm-cf-err')) return <CfErrWindow />;
  return <App />;
}
