import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * 错误边界：把「一个组件抛错 → 整站白屏」收敛为「只有那块区域显示错误卡片」。
 *
 * 为什么必须有：本应用的面板策略是「访问过的页签永久挂载、只 display:none 隐藏」
 * （见 App.tsx 的 visited 集合）。因此任何一个页签内部 render 抛错，都会顺着
 * React 树冒到根，把**所有页签连同侧栏壳一起**卸载掉 —— 用户看到全白，
 * 连"切到别的页签"这条自救路径都没有。
 *
 * 用法：
 *   <ErrorBoundary label="K8s 快照">…面板…</ErrorBoundary>
 *   <ErrorBoundary>…整个应用…</ErrorBoundary>
 */
interface Props {
  children: ReactNode;
  /** 出错时展示的区域名（如页签名），便于用户知道是哪一块坏了 */
  label?: string;
  /** 区域级边界点「重试」时回调（顶层边界无此项，只能重载页面） */
  onReset?: () => void;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 同时打到控制台与 Electron 日志（与全局错误上报路径一致，便于事后排查）
    console.error('[ErrorBoundary] 渲染异常：', error, info.componentStack);
    const api = (window as unknown as {
      electronAPI?: { log?: (level: string, msg: string) => void };
    }).electronAPI;
    try {
      api?.log?.('error', `[ErrorBoundary] ${this.props.label || 'app'}: ${error?.message || error}`);
    } catch {
      /* 上报失败不影响降级展示 */
    }
  }

  private reset = (): void => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    const label = this.props.label;
    const isArea = Boolean(label);
    return (
      <div className={isArea ? 'error-boundary error-boundary--area' : 'error-boundary error-boundary--app'} role="alert">
        <div className="error-boundary-card">
          <div className="error-boundary-icon" aria-hidden="true">⚠️</div>
          <h2 className="error-boundary-title">
            {isArea ? `「${label}」渲染出错` : '界面渲染出错'}
          </h2>
          <p className="error-boundary-msg">
            {isArea
              ? '这个面板已停止渲染，其余功能不受影响。可点「重试本面板」，或切换到其它页签继续使用。'
              : '应用无法继续渲染。可点「重试」，或重载页面恢复。'}
          </p>
          <pre className="error-boundary-detail">{String(error?.message || error)}</pre>
          <div className="error-boundary-actions">
            <button className="btn btn-sm btn-primary" onClick={this.reset}>
              重试{isArea ? '本面板' : ''}
            </button>
            <button className="btn btn-sm" onClick={() => window.location.reload()}>
              重载页面
            </button>
          </div>
        </div>
      </div>
    );
  }
}
