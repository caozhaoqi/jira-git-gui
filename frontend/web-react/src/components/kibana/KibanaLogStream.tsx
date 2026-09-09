import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiPost } from '../../api/client';
import type {
  KibanaContextResp, KibanaExportResp, KibanaLogRow, KibanaLogsReq, KibanaLogsResp,
} from '../../api/types';
import { useAppStore } from '../../store/useAppStore';
import { useT } from '../../i18n';
import { copyText } from '../../utils/clipboard';

export interface LogStreamProps {
  /** 检索条件（不含分页/排序） */
  req: Omit<KibanaLogsReq, 'size' | 'from_' | 'order'>;
  /** 自动刷新间隔秒；0 = 关闭 */
  refreshSec: number;
  wrap: boolean;
  onReqError?: (msg: string) => void;
  /** 由父级持有「自动换行」开关，两个子标签共享同一个偏好 */
  onToggleWrap?: () => void;
  /** 顶部附加操作（如「导出」按钮由父级决定放哪） */
  extraActions?: React.ReactNode;
  /** 紧跟头部下方的自定义区（容器视角放 Pod 名称） */
  headerExtra?: React.ReactNode;
  emptyHint?: string;
}

const PAGE = 300;

/**
 * 日志流：查询 / 分页 / 自动刷新 / 跟随 / 复制 / 导出 / 双击看上下文。
 * 容器视角与检索视角共用，保证两侧交互完全一致。
 */
export function KibanaLogStream({
  req, refreshSec, wrap, onReqError, onToggleWrap, extraActions, headerExtra, emptyHint,
}: LogStreamProps) {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);

  const [rows, setRows] = useState<KibanaLogRow[]>([]);
  const [total, setTotal] = useState<{ value: number; relation?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [follow, setFollow] = useState(true);
  // 自动刷新时只在「用户已在底部」才自动滚，避免打断正在往上翻的人
  const [autoRefreshing, setAutoRefreshing] = useState(false);
  const [ctxRow, setCtxRow] = useState<KibanaLogRow | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const reqRef = useRef(req);
  reqRef.current = req;
  const orderRef = useRef(order);
  orderRef.current = order;

  const reqKey = JSON.stringify(req);

  const search = useCallback(async (opts?: { silent?: boolean; size?: number }) => {
    const silent = opts?.silent;
    if (!silent) setBusy(true);
    setError('');
    try {
      const d = await apiPost<KibanaLogsResp>('/api/kibana/logs', {
        ...reqRef.current,
        size: opts?.size ?? PAGE,
        from_: 0,
        order: orderRef.current,
      });
      if (d.ok === false) {
        setError(d.error || '');
        onReqError?.(d.error || '');
        if (!silent) setRows([]);
        return;
      }
      setRows(d.rows || []);
      setTotal(d.total ?? null);
    } catch (ex: any) {
      setError(ex.message || String(ex));
      onReqError?.(ex.message || String(ex));
      if (!silent) setRows([]);
    } finally {
      if (!silent) setBusy(false);
    }
  }, [onReqError]);

  // 条件变化 → 立即查一次
  useEffect(() => { search(); }, [reqKey, order, search]);

  // 自动刷新：静默重查，不置 busy（避免按钮闪烁）
  useEffect(() => {
    if (!refreshSec || refreshSec <= 0) return;
    const timer = window.setInterval(async () => {
      setAutoRefreshing(true);
      await search({ silent: true });
      setAutoRefreshing(false);
    }, refreshSec * 1000);
    return () => window.clearInterval(timer);
  }, [refreshSec, search]);

  // 跟随：内容变化后滚到底
  useEffect(() => {
    if (!follow || !scrollRef.current) return;
    const el = scrollRef.current;
    el.scrollTop = el.scrollHeight;
  }, [rows, follow]);

  const loadMore = useCallback(async () => {
    if (busy || !rows.length) return;
    setBusy(true);
    try {
      const d = await apiPost<KibanaLogsResp>('/api/kibana/logs', {
        ...reqRef.current, size: PAGE, from_: rows.length, order: orderRef.current,
      });
      if (d.ok !== false && d.rows?.length) {
        setRows((prev) => [...prev, ...d.rows!]);
      }
    } catch (ex: any) {
      addToast(ex.message || String(ex), 'error');
    } finally {
      setBusy(false);
    }
  }, [busy, rows.length, addToast]);

  const doExport = useCallback(async () => {
    try {
      const d = await apiPost<KibanaExportResp>('/api/kibana/export', {
        ...reqRef.current, size: 20000,
      });
      if (d.ok === false || !d.content) {
        addToast(d.error || t('kibana.exportFailed'), 'error');
        return;
      }
      const blob = new Blob([d.content], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = d.filename || 'kibana-logs.log';
      a.click();
      URL.revokeObjectURL(url);
      addToast(t('kibana.exported', { n: d.count ?? 0 }), 'success');
    } catch (ex: any) {
      addToast(ex.message || String(ex), 'error');
    }
  }, [addToast, t]);

  const copyAll = useCallback(async () => {
    const text = rows.map((r) => `[${r.ts}] ${r.level || ''} ${r.msg}`).join('\n');
    const ok = await copyText(text);
    addToast(ok ? t('kibana.copied', { n: rows.length }) : t('kibana.copyFailed'),
             ok ? 'success' : 'error');
  }, [rows, addToast, t]);

  const errCount = useMemo(
    () => rows.filter((r) => ['ERROR', 'FATAL', 'CRITICAL'].includes(r.level || '')).length,
    [rows]);

  return (
    <div className="kb-stream">
      <div className="kb-stream-head">
        {headerExtra}
        <div className="spacer" />
        {total && (
          <span className="kb-count">
            {t('kibana.hits', {
              n: total.value,
              more: total.relation === 'gte' ? '+' : '',
            })}
            {errCount > 0 && <b className="kb-err-count"> · {errCount} ERROR</b>}
          </span>
        )}
        {autoRefreshing && <span className="kb-tick">⟳</span>}
        <button
          className={`btn btn-ghost btn-sm${order === 'desc' ? ' btn-active' : ''}`}
          onClick={() => setOrder(order === 'desc' ? 'asc' : 'desc')}
          title={t('kibana.toggleOrder')}
        >
          {order === 'desc' ? '↓ 最新' : '↑ 最早'}
        </button>
        <label className="chk kb-chk" title={t('kibana.followTip')}>
          <input type="checkbox" checked={follow}
                 onChange={(e) => setFollow(e.target.checked)} />
          {t('kibana.follow')}
        </label>
        <label className="chk kb-chk">
          <input type="checkbox" checked={wrap} onChange={() => onToggleWrap?.()} />
          {t('kibana.wrap')}
        </label>
        <button className="btn btn-ghost btn-sm" onClick={copyAll}
                disabled={!rows.length}>{t('common.copy')}</button>
        <button className="btn btn-ghost btn-sm" onClick={doExport}
                disabled={!rows.length}>{t('common.download')}</button>
        {extraActions}
      </div>

      {error && <div className="kb-stream-err">{error}</div>}

      <div className={`kb-log${wrap ? ' wrap' : ''}`} ref={scrollRef}>
        {rows.length === 0 ? (
          <div className="empty-hint">{busy ? t('common.loading') : (emptyHint || t('kibana.noLogs'))}</div>
        ) : (
          <>
            {rows.map((r, i) => (
              <div
                key={`${r.id}-${i}`}
                className={`kb-line lv-${(r.level || 'none').toLowerCase()}`}
                onDoubleClick={() => setCtxRow(r)}
                title={t('kibana.dblContext')}
              >
                <span className="kb-ts">{fmtTs(r.ts)}</span>
                <span className={`kb-lv lv-${(r.level || 'none').toLowerCase()}`}>
                  {(r.level || '·').padEnd(5, ' ')}
                </span>
                <span className="kb-src">{r.container || r.app || ''}</span>
                <span className="kb-msg">{r.msg}</span>
              </div>
            ))}
            <div className="kb-more">
              <button className="btn btn-ghost btn-sm" onClick={loadMore} disabled={busy}>
                {busy ? t('common.loading') : t('kibana.loadMore')}
              </button>
            </div>
          </>
        )}
      </div>

      {ctxRow && <KibanaContextModal row={ctxRow} onClose={() => setCtxRow(null)} />}
    </div>
  );
}

/** 把 UTC ISO 时间显示成本地 HH:MM:SS.mmm */
function fmtTs(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function KibanaContextModal({ row, onClose }: { row: KibanaLogRow; onClose: () => void }) {
  const { t } = useT();
  const [rows, setRows] = useState<KibanaLogRow[]>([]);
  const [anchorIdx, setAnchorIdx] = useState(-1);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await apiPost<KibanaContextResp>('/api/kibana/context', {
          anchor: row.ts, pod: row.pod || '', container: row.container || '',
          namespace: row.namespace || '', before: 60, after: 60,
        });
        if (!alive) return;
        if (d.ok === false) { setErr(d.error || ''); return; }
        setRows(d.rows || []);
        setAnchorIdx(typeof d.anchor_index === 'number' ? d.anchor_index : -1);
      } catch (ex: any) {
        if (alive) setErr(ex.message || String(ex));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [row]);

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal kb-ctx-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <span>{t('kibana.context')} · {row.pod || row.container || ''}</span>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body kb-ctx-body">
          {loading && <div className="empty-hint">{t('common.loading')}</div>}
          {err && <div className="kb-stream-err">{err}</div>}
          {rows.map((r, i) => (
            <div key={`${r.id}-${i}`}
                 className={`kb-line wrap${i === anchorIdx ? ' anchor' : ''}`}>
              <span className="kb-ts">{fmtTs(r.ts)}</span>
              <span className={`kb-lv lv-${(r.level || 'none').toLowerCase()}`}>
                {(r.level || '·').padEnd(5, ' ')}
              </span>
              <span className="kb-msg">{r.msg}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
