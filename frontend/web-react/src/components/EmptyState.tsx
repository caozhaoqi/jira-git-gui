import type { ReactNode } from 'react';

interface EmptyStateProps {
  /** 主标题（图标下方第一行，强调） */
  title?: ReactNode;
  /** 次要说明（第二行，弱色） */
  hint?: ReactNode;
  /** 图标（可选，置于顶部，装饰性自动 aria-hidden） */
  icon?: ReactNode;
  /** 操作区（可选，如「+ 新增」按钮） */
  actions?: ReactNode;
  /** 额外类名：保留旧样式钩子（hcm-empty-state / kb-site-empty 等），便于平滑迁移 */
  className?: string;
}

/**
 * 统一的空状态组件：替代此前散落在各面板、命名各异的空状态实现
 * （empty-hint / hcm-empty-state / kb-site-empty / kb-site-placeholder /
 * ai-bridge-empty / settings-empty / cfd-var-empty / diff-dir-empty …）。
 * 结构统一为 icon → title → hint → actions，外观由 .empty-state 基类接管。
 */
export function EmptyState({ title, hint, icon, actions, className }: EmptyStateProps) {
  return (
    <div className={`empty-state${className ? ' ' + className : ''}`}>
      {icon != null && <div className="empty-state-icon" aria-hidden="true">{icon}</div>}
      {title != null && <div className="empty-state-title">{title}</div>}
      {hint != null && <div className="empty-state-hint">{hint}</div>}
      {actions != null && <div className="empty-state-actions">{actions}</div>}
    </div>
  );
}
