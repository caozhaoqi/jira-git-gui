// 通用格式化工具。
// 注：遗留 web/js/01-core.js 的 esc / renderDiff / formatRelativeTime /
// authorColor 等已删除（React 下零引用——DiffPanel 有自己的 renderDiffContent，
// FileTree 有自己的 fmtSize）。保留仍在使用的 fmtSize / K8s 路径 / top 解析。

/** 文件大小格式化 */
export function fmtSize(n?: number | null): string {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} K`;
  return `${(n / 1048576).toFixed(1)} M`;
}

// ===== K8s 路径工具 =====
export function k8sPathJoin(base: string, name: string): string {
  if (!base || base === '/') return '/' + name;
  if (base.endsWith('/')) return base + name;
  return base + '/' + name;
}
export function k8sPathParent(p: string): string {
  if (!p || p === '/') return '/';
  const s = p.endsWith('/') ? p.slice(0, -1) : p;
  const i = s.lastIndexOf('/');
  return i <= 0 ? '/' : s.slice(0, i);
}

/** 解析 kubectl top 数值（CPU/内存）为可比数字 */
export function parseTopVal(s?: string): number {
  s = (s || '').trim();
  if (!s || s === '?') return 0;
  if (s.endsWith('m')) {
    const v = parseFloat(s.slice(0, -1));
    return isNaN(v) ? 0 : v / 1000;
  }
  const m = s.match(/^([\d.]+)(Ki|Mi|Gi|Ti|K|M|G|T|i|n)?$/);
  if (!m) {
    const v = parseFloat(s);
    return isNaN(v) ? 0 : v;
  }
  const val = parseFloat(m[1]);
  const unit = m[2] || '';
  const mult: Record<string, number> = {
    n: 1e-9,
    Ki: 1 / 1024,
    Mi: 1,
    Gi: 1024,
    Ti: 1048576,
    K: 1e-6,
    M: 1e-3,
    G: 1,
    T: 1e3,
    i: 1,
  };
  return val * (mult[unit] || 1);
}
