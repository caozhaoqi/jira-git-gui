/**
 * HCM 查询结果的表格视图（面板内 / 全屏 / 独立窗口共用）。
 *
 * 为什么需要
 * ----------
 * 「数据」子页签原本只有 JSON 视图。记录一多（实测 983 条 × 几十个字段），
 * 想对比某几列、找某个值、横向扫一遍字段，读 JSON 非常费劲。这里把
 * ``hcm.model.list`` 的 ``list`` 数组渲染成表：列 = 字段并集，行 = 记录。
 *
 * 设计约束
 * --------
 * 1. **不丢信息**：嵌套对象/数组不展开成多层列（会爆列宽），而是折叠成
 *    ``{…}`` / ``[n]`` 摘要 + 点开看完整 JSON。
 * 2. **列多时可横向滚动，首列吸附**：真实模型几十列，必须能横向拖看。
 * 3. **空值可辨**：``null`` / 缺失统一显示 ``—`` 弱色。
 * 4. **点列头排序**：数值按数值、字符串按本地化顺序。
 * 5. **就地搜索**：客户端全字段模糊过滤（不重打接口），服务端查询由调用方负责。
 * 6. **分页**：小屏 / 记录多时必须分页，否则表格变成一条无尽长条。
 * 7. **多平台适配**：桌面保留 hover；触控设备（无 hover）按钮按 34px 目标尺寸放大，
 *    避免指纹目标太小时点不到（用户反馈「按钮点不到」）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useT } from '../../i18n';

type Row = Record<string, unknown>;

export type HcmDataTableProps = {
  rows: Row[];
  emptyText?: string;
  showIndex?: boolean;
  dense?: boolean;
  /** 是否启用就地搜索框（独立窗口 / 全屏建议开） */
  searchable?: boolean;
  /**
   * 是否启用客户端分页。
   * 传 0 / 不传 = 不翻页（数据已由服务端分页）。传数字 = 每页行数。
   */
  clientPageSize?: number;
  /** 顶部工具条右侧的附加动作（如「复制表格」「全屏」） */
  toolbarExtra?: React.ReactNode;
  /** 显示「行 × 列」统计（默认 true） */
  showMeta?: boolean;
};

const SORT_NONE = 0;
const SORT_ASC = 1;
const SORT_DESC = 2;

function cellText(v: unknown): { text: string; kind: 'empty' | 'scalar' | 'complex' } {
  if (v === null || v === undefined || v === '') return { text: '—', kind: 'empty' };
  if (Array.isArray(v)) return { text: `[${v.length}]`, kind: 'complex' };
  if (typeof v === 'object') return { text: `{${Object.keys(v as Row).length}}`, kind: 'complex' };
  if (typeof v === 'boolean') return { text: v ? 'true' : 'false', kind: 'scalar' };
  return { text: String(v), kind: 'scalar' };
}

function cmp(a: unknown, b: unknown): number {
  const ae = a === null || a === undefined || a === '';
  const be = b === null || b === undefined || b === '';
  if (ae && be) return 0;
  if (ae) return 1;
  if (be) return -1;
  const an = typeof a === 'number' ? a : Number(a);
  const bn = typeof b === 'number' ? b : Number(b);
  const aNumeric = typeof a === 'number' || (typeof a === 'string' && a.trim() !== '' && Number.isFinite(an));
  const bNumeric = typeof b === 'number' || (typeof b === 'string' && b.trim() !== '' && Number.isFinite(bn));
  if (aNumeric && bNumeric) return an - bn;
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

function collectColumns(rows: Row[]): string[] {
  const seen = new Set<string>();
  const cols: string[] = [];
  for (const r of rows) {
    if (!r || typeof r !== 'object') continue;
    for (const k of Object.keys(r)) {
      if (!seen.has(k)) { seen.add(k); cols.push(k); }
    }
  }
  return cols;
}

/** 一行是否命中搜索词（全字段包含匹配，大小写不敏感）。 */
function rowMatches(row: Row, cols: string[], q: string): boolean {
  const needle = q.toLowerCase();
  for (const c of cols) {
    const v = row[c];
    if (v === null || v === undefined) continue;
    const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
    if (s.toLowerCase().includes(needle)) return true;
  }
  return false;
}

export function HcmDataTable({
  rows,
  emptyText,
  showIndex = true,
  dense = false,
  searchable = false,
  clientPageSize = 0,
  toolbarExtra,
  showMeta = true,
}: HcmDataTableProps) {
  const { t } = useT();
  const [sortKey, setSortKey] = useState('');
  const [sortDir, setSortDir] = useState<number>(SORT_NONE);
  const [peek, setPeek] = useState<{ key: string; idx: number } | null>(null);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(clientPageSize || 50);
  const pageSizeRef = useRef(pageSize);
  pageSizeRef.current = pageSize;

  // 外部页大小变化时跟随（调用方切换「每页 N 行」）
  useEffect(() => {
    if (clientPageSize > 0) setPageSize(clientPageSize);
  }, [clientPageSize]);

  const columns = useMemo(() => collectColumns(rows), [rows]);

  // 1) 搜索过滤（客户端）
  const filtered = useMemo(() => {
    const q = query.trim();
    if (!q) return rows;
    return rows.filter((r) => rowMatches(r, columns, q));
  }, [rows, columns, query]);

  // 2) 排序
  const sorted = useMemo(() => {
    if (!sortKey || sortDir === SORT_NONE) return filtered;
    const copy = [...filtered];
    copy.sort((a, b) => {
      const r = cmp(a?.[sortKey], b?.[sortKey]);
      return sortDir === SORT_ASC ? r : -r;
    });
    return copy;
  }, [filtered, sortKey, sortDir]);

  // 3) 分页（客户端）
  const totalPages = useMemo(() => {
    if (!clientPageSize) return 1;
    return Math.max(1, Math.ceil(sorted.length / pageSize));
  }, [sorted.length, pageSize, clientPageSize]);

  const paged = useMemo(() => {
    if (!clientPageSize) return sorted;
    const start = (Math.min(page, totalPages) - 1) * pageSize;
    return sorted.slice(start, start + pageSize);
  }, [sorted, page, pageSize, totalPages, clientPageSize]);

  // 过滤后页数变少时回到有效页
  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  function toggleSort(key: string) {
    if (sortKey !== key) { setSortKey(key); setSortDir(SORT_ASC); return; }
    setSortDir((d) => (d === SORT_ASC ? SORT_DESC : d === SORT_DESC ? SORT_NONE : SORT_ASC));
  }

  const resetPage = useCallback(() => setPage(1), []);

  const visibleColumns = columns;
  const offset = clientPageSize ? (Math.min(page, totalPages) - 1) * pageSize : 0;

  return (
    <div className={`hcm-dt-shell${dense ? ' is-dense' : ''}`}>
      {/* 工具条：搜索 + 统计 + 调用方动作。窄屏自动换行，不压扁控件 */}
      <div className="hcm-dt-toolbar">
        {searchable && (
          <label className="hcm-dt-search">
            <span className="hcm-dt-search-ico" aria-hidden="true">⌕</span>
            <input
              className="input hcm-dt-search-input"
              value={query}
              onChange={(e) => { setQuery(e.target.value); resetPage(); }}
              placeholder={t('hcm.dt.searchPlaceholder')}
              aria-label={t('hcm.dt.searchPlaceholder')}
              spellCheck={false}
            />
            {query && (
              <button
                type="button"
                className="hcm-dt-search-clear"
                onClick={() => { setQuery(''); resetPage(); }}
                aria-label={t('hcm.dt.clearSearch')}
                title={t('hcm.dt.clearSearch')}
              >
                ×
              </button>
            )}
          </label>
        )}
        {showMeta && (
          <span className="hcm-dt-meta hcm-mono">
            {query.trim()
              ? t('hcm.dt.metaFiltered', {
                  shown: sorted.length,
                  total: rows.length,
                  cols: visibleColumns.length,
                })
              : t('hcm.dt.meta', { rows: rows.length, cols: visibleColumns.length })}
          </span>
        )}
        <span className="spacer" />
        {toolbarExtra}
      </div>

      {rows.length === 0 ? (
        <div className="hcm-dt-empty">
          <div className="hcm-dt-empty-icon" aria-hidden="true">▦</div>
          <div>{emptyText || t('hcm.dataTableEmpty')}</div>
        </div>
      ) : sorted.length === 0 ? (
        <div className="hcm-dt-empty">
          <div className="hcm-dt-empty-icon" aria-hidden="true">⌕</div>
          <div>{t('hcm.dt.noMatch')}</div>
          <div className="hcm-dt-empty-hint">{t('hcm.dt.noMatchHint')}</div>
        </div>
      ) : (
        <div className="hcm-dt-wrap" role="region" aria-label={t('hcm.dataViewTable')} tabIndex={0}>
          <table className="hcm-dt">
            <thead>
              <tr>
                {showIndex && <th className="hcm-dt-idx" scope="col">#</th>}
                {visibleColumns.map((c) => (
                  <th
                    key={c}
                    scope="col"
                    className={`hcm-dt-th${sortKey === c ? ' is-sorted' : ''}`}
                    onClick={() => toggleSort(c)}
                    title={t('hcm.dt.sortHint', { col: c })}
                    aria-sort={
                      sortKey === c && sortDir === SORT_ASC ? 'ascending'
                        : sortKey === c && sortDir === SORT_DESC ? 'descending'
                        : 'none'
                    }
                  >
                    <span className="hcm-dt-th-name">{c}</span>
                    <span className="hcm-dt-sort" aria-hidden="true">
                      {sortKey === c && sortDir === SORT_ASC ? '▲' : sortKey === c && sortDir === SORT_DESC ? '▼' : ''}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paged.map((row, i) => {
                const absIdx = offset + i;
                return (
                  <tr key={absIdx}>
                    {showIndex && <td className="hcm-dt-idx">{absIdx + 1}</td>}
                    {visibleColumns.map((c) => {
                      const v = row?.[c];
                      const cell = cellText(v);
                      const isOpen = peek?.key === c && peek?.idx === absIdx;
                      return (
                        <td
                          key={c}
                          className={`hcm-dt-td hcm-dt-td--${cell.kind}${isOpen ? ' is-peek' : ''}`}
                          title={cell.kind === 'complex' ? JSON.stringify(v, null, 2) : cell.text}
                          onClick={() => {
                            if (cell.kind !== 'complex') return;
                            setPeek(isOpen ? null : { key: c, idx: absIdx });
                          }}
                        >
                          {cell.text}
                          {isOpen && <pre className="hcm-dt-peek">{JSON.stringify(v, null, 2)}</pre>}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 分页条：客户端分页时才出现。触屏/窄屏下按钮保持足够点按面积。 */}
      {clientPageSize > 0 && sorted.length > 0 && (
        <div className="hcm-dt-pager">
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => setPage(1)}
            disabled={page <= 1}
            title={t('hcm.dt.firstPage')}
            aria-label={t('hcm.dt.firstPage')}
          >
            «
          </button>
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            title={t('hcm.dt.prevPage')}
            aria-label={t('hcm.dt.prevPage')}
          >
            ‹
          </button>
          <span className="hcm-dt-page-info">
            {t('hcm.dt.pageInfo', { page: Math.min(page, totalPages), total: totalPages })}
          </span>
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            title={t('hcm.dt.nextPage')}
            aria-label={t('hcm.dt.nextPage')}
          >
            ›
          </button>
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => setPage(totalPages)}
            disabled={page >= totalPages}
            title={t('hcm.dt.lastPage')}
            aria-label={t('hcm.dt.lastPage')}
          >
            »
          </button>
          <span className="spacer" />
          <label className="hcm-dt-pagesize">
            {t('hcm.dt.perPage')}
            <select
              className="sel"
              value={pageSize}
              onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
            >
              {[20, 50, 100, 200, 500].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>
        </div>
      )}
    </div>
  );
}
