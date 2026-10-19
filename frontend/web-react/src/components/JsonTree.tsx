import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useT } from '../i18n';

/**
 * JSON 响应树（对齐 HCM 网关调试页的结果区）：
 * - 可折叠节点 + {n}/[n] 计数徽标；根节点默认展开、嵌套默认折叠
 * - 搜索：命中键/值的节点高亮，祖先链自动展开，命中计数 + 上一个/下一个跳转
 * - 每行可复制值（JSON）与复制路径（a.b[0].c）
 */

interface JsonTreeProps {
  data: unknown;
  /** 搜索词（空 = 不高亮） */
  search?: string;
}

type NodeKind = 'object' | 'array' | 'string' | 'number' | 'boolean' | 'null';

function kindOf(v: unknown): NodeKind {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  switch (typeof v) {
    case 'object': return 'object';
    case 'number': return 'number';
    case 'boolean': return 'boolean';
    default: return 'string';
  }
}

function isBranch(v: unknown): boolean {
  return typeof v === 'object' && v !== null;
}

function childCount(v: unknown): number {
  return Array.isArray(v) ? v.length : Object.keys(v as object).length;
}

function fmtScalar(v: unknown): string {
  if (typeof v === 'string') return v;
  return String(v);
}

/** 节点在树中的稳定路径（a.b[0].c），用于展开态集合与搜索定位 */
function joinPath(parent: string, key: string | number): string {
  if (typeof key === 'number') return `${parent}[${key}]`;
  const needsQuote = /[^A-Za-z0-9_$]/.test(key);
  return needsQuote ? `${parent}["${key}"]` : (parent ? `${parent}.${key}` : key);
}

interface FlatNode {
  path: string;
  label: string;   // 键名或 [i]
  value: unknown;
  kind: NodeKind;
  depth: number;
  branch: boolean;
}

/** 深度优先展开当前可见节点（用于搜索命中收集） */
function flatten(node: unknown, path: string, label: string, depth: number, out: FlatNode[]): void {
  const kind = kindOf(node);
  out.push({ path, label, value: node, kind, depth, branch: isBranch(node) });
  if (!isBranch(node)) return;
  if (Array.isArray(node)) {
    node.forEach((v, i) => flatten(v, `${path}[${i}]`, String(i), depth + 1, out));
  } else {
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      flatten(v, joinPath(path, k), k, depth + 1, out);
    }
  }
}

export function JsonTree({ data, search = '' }: JsonTreeProps) {
  const { t } = useT();
  const q = search.trim();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(['$']));
  const [hitIdx, setHitIdx] = useState(0);
  const nodeRefs = useRef(new Map<string, HTMLDivElement>());
  const rootRef = useRef<HTMLDivElement>(null);

  // data 变化 → 重置展开态与命中
  useEffect(() => {
    setExpanded(new Set(['$']));
    setHitIdx(0);
  }, [data]);

  const hits = useMemo(() => {
    if (!q) return [] as FlatNode[];
    const all: FlatNode[] = [];
    flatten(data, '$', '', 0, all);
    const ql = q.toLowerCase();
    return all.filter((n) => {
      if (n.label.toLowerCase().includes(ql)) return true;
      if (!n.branch && fmtScalar(n.value).toLowerCase().includes(ql)) return true;
      return false;
    });
  }, [data, q]);

  // 命中变化 → 展开命中节点的全部祖先链 + 跳转焦点
  useEffect(() => {
    if (!hits.length) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      for (const h of hits) {
        // 路径 $ 是根；逐段还原祖先
        let p = h.path;
        while (p && p !== '$') {
          next.add(p);
          // 向上剥一层：$["a"].b[0] → $["a"].b → $["a"] → $
          const m = p.match(/^(.*?)(\["[^"]*"\]|\[[^\]]*\]|\.[^.\[\]]+)$/);
          if (!m) break;
          p = m[1];
          if (p.startsWith('$.')) p = '$' + p.slice(1);
          if (p === '$') break;
        }
        next.add('$');
      }
      return next;
    });
  }, [hits]);

  useEffect(() => { setHitIdx(0); }, [q]);

  const focusHit = useCallback((idx: number) => {
    const h = hits[idx];
    if (!h) return;
    setHitIdx(idx);
    const el = nodeRefs.current.get(h.path);
    if (el) {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.classList.add('jtree-hit--focus');
      window.setTimeout(() => el.classList.remove('jtree-hit--focus'), 1200);
    }
  }, [hits]);

  const toggle = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  }, []);

  const copy = useCallback(async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      const el = nodeRefs.current.get(key);
      if (el) {
        el.classList.add('jtree-row--copied');
        window.setTimeout(() => el.classList.remove('jtree-row--copied'), 900);
      }
    } catch { /* 剪贴板不可用时静默 */ }
  }, []);

  const hitSet = useMemo(() => new Set(hits.map((h) => h.path)), [hits]);

  const renderNode = (node: unknown, path: string, label: string, depth: number): React.ReactNode => {
    const kind = kindOf(node);
    const branch = isBranch(node);
    const open = expanded.has(path);
    const isHit = hitSet.has(path);
    const count = branch ? childCount(node) : 0;

    return (
      <div key={path} className="jtree-node">
        <div
          ref={(el) => { if (el) nodeRefs.current.set(path, el); else nodeRefs.current.delete(path); }}
          className={`jtree-row${isHit ? ' jtree-row--hit' : ''}`}
          style={{ paddingLeft: depth * 14 + 6 }}
        >
          {branch ? (
            <button
              type="button"
              className="jtree-toggle"
              aria-expanded={open}
              onClick={() => toggle(path)}
              title={open ? t('hcm.jtreeCollapse') : t('hcm.jtreeExpand')}
            >
              {open ? '▾' : '▸'}
            </button>
          ) : (
            <span className="jtree-toggle jtree-toggle--leaf" aria-hidden="true">·</span>
          )}
          {label !== '' && (
            <span className={`jtree-key jtree-key--${kind}`}>
              {typeof label === 'string' && /^\d+$/.test(label) ? `[${label}]` : `${label}`}
              <span className="jtree-colon">:</span>
            </span>
          )}
          {branch ? (
            <span className="jtree-badge">
              {kind === 'array' ? `[${count}]` : `{${count}}`}
            </span>
          ) : (
            <span className={`jtree-val jtree-val--${kind}`}>
              {kind === 'string' ? `"${fmtScalar(node)}"` : fmtScalar(node)}
            </span>
          )}
          <span className="jtree-actions">
            <button
              type="button"
              className="jtree-act"
              title={t('hcm.jtreeCopyValue')}
              onClick={() => void copy(JSON.stringify(node, null, 2), path)}
            >
              {t('hcm.jtreeCopyValue')}
            </button>
            <button
              type="button"
              className="jtree-act"
              title={t('hcm.jtreeCopyPath')}
              onClick={() => void copy(path, path)}
            >
              {path}
            </button>
          </span>
        </div>
        {branch && open && (
          <div className="jtree-children">
            {Array.isArray(node)
              ? node.map((v, i) => renderNode(v, `${path}[${i}]`, String(i), depth + 1))
              : Object.entries(node as Record<string, unknown>).map(([k, v]) =>
                  renderNode(v, joinPath(path, k), k, depth + 1))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="jtree" ref={rootRef}>
      {q && (
        <div className="jtree-searchbar">
          <span className="jtree-hits">
            {hits.length
              ? t('hcm.jtreeHits', { n: hits.length, i: hits.length ? hitIdx + 1 : 0 })
              : t('hcm.jtreeNoHit')}
          </span>
          <button
            type="button"
            className="jtree-nav"
            disabled={!hits.length}
            onClick={() => focusHit((hitIdx - 1 + hits.length) % hits.length)}
            title={t('hcm.jtreePrev')}
          >
            ↑
          </button>
          <button
            type="button"
            className="jtree-nav"
            disabled={!hits.length}
            onClick={() => focusHit((hitIdx + 1) % hits.length)}
            title={t('hcm.jtreeNext')}
          >
            ↓
          </button>
        </div>
      )}
      <div className="jtree-body">{renderNode(data, '$', '', 0)}</div>
    </div>
  );
}
