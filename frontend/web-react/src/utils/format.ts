// 通用格式化工具，复刻 web/js/01-core.js 的 esc / renderDiff / 辅助函数。
// 在 React 中：纯文本一律走 JSX 插值（自动转义）；需要富文本的（如 diff）
// 用 renderDiff 生成已转义 HTML 字符串，配合 dangerouslySetInnerHTML 渲染。

/** HTML 转义（用于 dangerouslySetInnerHTML 场景） */
export function esc(s: unknown): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ===== Diff 工具（行级 LCS） =====
const DIFF_MAX_LINES = 5000;

/** 渲染两文本的行级 diff（红绿行），返回已转义 HTML 字符串。 */
export function renderDiff(oldText: string, newText: string): string {
  const a = (oldText || '').split('\n');
  const b = (newText || '').split('\n');
  if (a.length > DIFF_MAX_LINES || b.length > DIFF_MAX_LINES) {
    return (
      `<div class="diff-truncated">⚠ 文件过大（${a.length} → ${b.length} 行），仅渲染前 ${DIFF_MAX_LINES} 行差异</div>` +
      renderDiffCore(a.slice(0, DIFF_MAX_LINES), b.slice(0, DIFF_MAX_LINES))
    );
  }
  return renderDiffCore(a, b);
}

function renderDiffCore(a: string[], b: string[]): string {
  const m = a.length;
  const n = b.length;
  const dp: Uint32Array[] = new Array(m + 1);
  for (let i = 0; i <= m; i++) dp[i] = new Uint32Array(n + 1);
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops: { type: 'eq' | 'del' | 'add'; text: string }[] = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      ops.push({ type: 'eq', text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ type: 'del', text: a[i] });
      i++;
    } else {
      ops.push({ type: 'add', text: b[j] });
      j++;
    }
  }
  while (i < m) ops.push({ type: 'del', text: a[i++] });
  while (j < n) ops.push({ type: 'add', text: b[j++] });

  const lines: string[] = [];
  let oldNo = 0;
  let newNo = 0;
  let adds = 0;
  let dels = 0;
  for (const op of ops) {
    if (op.type === 'eq') {
      oldNo++;
      newNo++;
    } else if (op.type === 'del') {
      oldNo++;
      dels++;
    } else {
      newNo++;
      adds++;
    }
    const cls = op.type === 'add' ? 'diff-add' : op.type === 'del' ? 'diff-del' : 'diff-eq';
    const sign = op.type === 'add' ? '+' : op.type === 'del' ? '-' : ' ';
    const oldN = String(oldNo).padStart(4);
    const newN = String(newNo).padStart(4);
    lines.push(
      `<div class="diff-row ${cls}">` +
        `<span class="diff-gutter diff-gutter-old">${op.type === 'add' ? '' : oldN}</span>` +
        `<span class="diff-gutter diff-gutter-new">${op.type === 'del' ? '' : newN}</span>` +
        `<span class="diff-sign">${sign}</span>` +
        `<span class="diff-text">${esc(op.text || ' ')}</span>` +
        `</div>`
    );
  }
  return (
    `<div class="diff-stats">+${adds} -${dels}</div>` +
    `<div class="diff-body">${lines.join('')}</div>`
  );
}

/** 相对时间：'刚刚' / 'X 分钟前' / ... / yyyy-mm-dd */
export function formatRelativeTime(iso?: string): string {
  if (!iso) return '';
  const t = new Date(iso);
  if (isNaN(t.getTime())) return (iso || '').slice(0, 10);
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / (86400 * 7))} 周前`;
  return t.toISOString().slice(0, 10);
}

/** 作者徽章颜色（稳定 hash 到 HSL 色相） */
export function authorColor(author?: string): string {
  const s = String(author || '?').trim();
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff;
  return `hsl(${h % 360}, 55%, 45%)`;
}
export function authorInitial(author?: string): string {
  const s = String(author || '?').trim();
  return s ? s[0].toUpperCase() : '?';
}

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
