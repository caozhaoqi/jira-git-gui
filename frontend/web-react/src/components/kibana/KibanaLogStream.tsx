import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { apiGet, apiPost } from '../../api/client';
import type {
  KibanaBucket, KibanaContextResp, KibanaFieldsResp,
  KibanaLogRow, KibanaLogsReq, KibanaLogsResp,
} from '../../api/types';
import { useAppStore } from '../../store/useAppStore';
import { useT } from '../../i18n';
import { copyText } from '../../utils/clipboard';
import { saveBlob } from '../../utils/download';
import { useModalA11y } from '../../utils/useModalA11y';
import { usePanelActive } from '../../utils/panelActive';
import { KibanaContext } from './context';
import { TIME_PRESETS } from './KibanaFilters';
import { fetchExportContent, EXPORT_PAGE, type KibanaExportPost } from './kibanaExport';
import { literalPhraseOf } from './kibanaFind';

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
  // 面板隐藏（切到其它页签）时暂停自动刷新，避免后台空转打后端
  const active = usePanelActive();
  // 顶部「查询」按钮的加载态（计数制，见 context.addBusy）；独立窗口无 Provider 时静默。
  // ⚠️ 只取 addBusy（Panel 里是 useCallback([]) 的稳定引用），不能把 ctx 对象本身
  // 放进 search/loadMore 的依赖数组——ctx 每次渲染都是新字面量，会让回调每帧重建、
  // useEffect 每帧触发，造成请求风暴（ERR_INSUFFICIENT_RESOURCES）。
  const addBusy = useContext(KibanaContext)?.addBusy;

  const [rows, setRows] = useState<KibanaLogRow[]>([]);
  const [total, setTotal] = useState<{ value: number; relation?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [follow, setFollow] = useState(true);
  // 自动刷新时只在「用户已在底部」才自动滚，避免打断正在往上翻的人
  const [autoRefreshing, setAutoRefreshing] = useState(false);
  const [ctxRow, setCtxRow] = useState<KibanaLogRow | null>(null);
  // 最后一次成功查询时间 + 自动刷新倒计时（秒）
  const [lastUpdated, setLastUpdated] = useState('');
  const [countdown, setCountdown] = useState(0);
  // —— 实时查找（类似 CF 日志的 live filter）：输入关键词即时过滤已加载行 + 高亮 + 逐条定位 ——
  // ⚠️ 变量名不能叫 search：组件里已有 search() 是「向后端发查询」的函数。
  const [findText, setFindText] = useState('');
  const [caseSensitive, setCaseSensitive] = useState(false);
  // 过滤开关：开 = 只显示匹配行；关 = 保留全部行仅高亮（用于「看看有没有」而不想丢上下文）
  const [filterOn, setFilterOn] = useState(true);
  // 当前选中的匹配序号（1 起；0 = 无选中）
  const [activeMatch, setActiveMatch] = useState(0);

  // 主筛选「关键词」也能高亮：后端对普通词走 match_phrase，命中行正文里逐字包含它。
  // ⚠️ 只高亮、不过滤（见 kibanaFind.ts：通配符/布尔语法下逐字匹配会一处都不中，
  //    若拿来过滤就会把用户刚查出来的行全部过滤光）。用户在查找框输入时以其为准。
  const autoHighlight = useMemo(() => literalPhraseOf(req.keyword), [req.keyword]);
  const hlQuery = findText.trim() || autoHighlight;

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
    addBusy?.(1);
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
      const p = (n: number) => String(n).padStart(2, '0');
      const now = new Date();
      setLastUpdated(`${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`);
    } catch (ex: any) {
      setError(ex.message || String(ex));
      onReqError?.(ex.message || String(ex));
      if (!silent) setRows([]);
    } finally {
      if (!silent) setBusy(false);
      addBusy?.(-1);
    }
  }, [onReqError, addBusy]);

  // 条件变化 → 立即查一次
  useEffect(() => { search(); }, [reqKey, order, search]);

  // 自动刷新：1s 粒度倒计时（head 区显示「Xs 后刷新」），到 0 静默重查；
  // 面板隐藏时暂停；刷新间隔变化时立即重置倒计时。
  useEffect(() => {
    if (!refreshSec || refreshSec <= 0 || !active) { setCountdown(0); return; }
    setCountdown(refreshSec);
    const timer = window.setInterval(() => {
      setCountdown((c) => {
        if (c <= 1) {
          setAutoRefreshing(true);
          search({ silent: true }).finally(() => setAutoRefreshing(false));
          return refreshSec;
        }
        return c - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [refreshSec, search, active]);

  // 跟随：内容变化后滚到底。
  // ⚠️ 正在按匹配逐条定位（activeMatch > 0）时必须让位：否则上一步刚 scrollIntoView
  // 到某一行，跟随立刻把它们拽回底部，用户永远看不到自己点的匹配。
  useEffect(() => {
    if (!follow || activeMatch > 0 || !scrollRef.current) return;
    const el = scrollRef.current;
    el.scrollTop = el.scrollHeight;
  }, [rows, follow, activeMatch]);

  const loadMore = useCallback(async () => {
    if (busy || !rows.length) return;
    setBusy(true);
    addBusy?.(1);
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
      addBusy?.(-1);
    }
  }, [busy, rows.length, addToast, addBusy]);

  // 下载对话框：选择时间段 / 行数上限 / 容器后下载筛选后的日志
  const [dlOpen, setDlOpen] = useState(false);

  const copyAll = useCallback(async () => {
    const text = rows.map((r) => `[${r.ts}] ${r.level || ''} ${r.msg}`).join('\n');
    const ok = await copyText(text);
    addToast(ok ? t('kibana.copied', { n: rows.length }) : t('kibana.copyFailed'),
             ok ? 'success' : 'error');
  }, [rows, addToast, t]);

  const errCount = useMemo(
    () => rows.filter((r) => ['ERROR', 'FATAL', 'CRITICAL'].includes(r.level || '')).length,
    [rows]);

  // —— 实时查找：一次遍历算出「每行匹配区间 + 过滤后的行 + 匹配总数」——
  // 高亮口径与过滤口径必须来自同一遍计算，否则「高亮 N 处」会和「匹配 N 行」对不上。
  // hlQuery = 查找框输入 优先，否则主筛选关键词里可逐字高亮的部分；
  // 过滤只看 findText——关键词高亮绝不能把行过滤掉。
  const view = useMemo(() => {
    const q = hlQuery;
    const fq = findText.trim();
    const items: RowView[] = [];
    let matchTotal = 0;
    let matchRows = 0;
    rows.forEach((r, i) => {
      const tsText = fmtTs(r.ts);
      const srcText = r.container || r.app || '';
      const mTs = q ? findMatches(tsText, q, caseSensitive) : [];
      const mLv = q ? findMatches(r.level || '', q, caseSensitive) : [];
      const mSrc = q ? findMatches(srcText, q, caseSensitive) : [];
      const mMsg = q ? findMatches(r.msg, q, caseSensitive) : [];
      const count = mTs.length + mLv.length + mSrc.length + mMsg.length;
      // filterOn 关时不过滤：全部行都留下，只把匹配高亮出来
      if (fq && filterOn && count === 0) return;
      const ordStart = matchTotal;
      matchTotal += count;
      if (count > 0) matchRows += 1;
      items.push({
        row: r, i, tsText, srcText, mTs, mLv, mSrc, mMsg,
        count, ordStart,
      });
    });
    return { items, matchTotal, matchRows, loaded: rows.length };
  }, [rows, hlQuery, findText, caseSensitive, filterOn]);

  // 选中项变化 → 滚到对应行（定位是用户主动行为，覆盖 follow 的滚底）
  useEffect(() => {
    if (activeMatch < 1 || view.matchTotal === 0) return;
    const hit = view.items.find(
      (it) => activeMatch > it.ordStart && activeMatch <= it.ordStart + it.count
    );
    if (!hit) return;
    const el = document.getElementById(`kb-line-${hit.i}`);
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [activeMatch, view]);

  // 有查询词 / 结果集变化 → 复位选中匹配（否则会指向已不存在的序号）。
  // ⚠️ 只有用户主动在查找框输入时才自动定位到第一处：关键词自动高亮时保持
  //    activeMatch=0，否则 follow（自动刷新滚底）会被永久让位、实时追日志就断了。
  useEffect(() => {
    const total = view.matchTotal;
    if (!hlQuery || total === 0) {
      if (activeMatch !== 0) setActiveMatch(0);
    } else if (findText.trim() && (activeMatch < 1 || activeMatch > total)) {
      setActiveMatch(1);
    }
  }, [hlQuery, findText, view.matchTotal, activeMatch]);

  /** 上一个 / 下一个匹配（循环） */
  const gotoMatch = useCallback((dir: number) => {
    const total = view.matchTotal || 0;
    if (total === 0) return;
    setActiveMatch((cur) => {
      let next = (cur || 1) + dir;
      if (next < 1) next = total;
      if (next > total) next = 1;
      return next;
    });
  }, [view.matchTotal]);

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
        {lastUpdated && (
          <span className="kb-last-update"
                title={t('kibana.lastUpdate', { time: lastUpdated })}>
            {t('kibana.lastUpdate', { time: lastUpdated })}
          </span>
        )}
        {refreshSec > 0 && countdown > 0 && (
          <span className={`kb-countdown${autoRefreshing ? ' run' : ''}`}
                title={t('kibana.nextRefresh', { s: countdown })}>
            {autoRefreshing ? '⟳' : `⟳ ${countdown}s`}
          </span>
        )}
        <button className="btn btn-ghost btn-sm" onClick={() => search()}
                disabled={busy} title={t('kibana.refreshNow')}>
          <span className={busy ? 'kb-spin' : ''}>↻</span>
        </button>
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
        <button className="btn btn-ghost btn-sm" onClick={() => setDlOpen(true)}
                disabled={!req.site}
                title={t('kibana.downloadTip')}>
          {t('common.download')}
        </button>
        {extraActions}
      </div>

      {/* ===== 实时查找条（同 CF 日志 live filter）：输入即过滤已加载行 + 高亮 + ↑↓ 逐条定位 ===== */}
      <div className="kb-search-bar">
        <input
          className="input input-sm kb-search-input"
          value={findText}
          onChange={(e) => {
            setFindText(e.target.value);
            setActiveMatch(e.target.value.trim() ? 1 : 0);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              gotoMatch(e.shiftKey ? -1 : 1);
            }
          }}
          placeholder={t('kibana.searchPlaceholder')}
          aria-label={t('kibana.searchPlaceholder')}
          title={t('kibana.searchScopeHint', { n: view.loaded })}
        />
        <label className="chk kb-search-case" title={t('kibana.caseSensitive')}>
          <input type="checkbox" checked={caseSensitive}
                 onChange={(e) => setCaseSensitive(e.target.checked)} />
          {t('kibana.caseSensitive')}
        </label>
        <label className="chk kb-search-case" title={t('kibana.filterToggleHint')}>
          <input type="checkbox" checked={filterOn}
                 onChange={(e) => setFilterOn(e.target.checked)} />
          {t('kibana.filterToggle')}
        </label>
        <span className="kb-search-count">
          {hlQuery
            ? filterOn && findText.trim()
              ? t('kibana.matchCount', { n: view.items.length, m: view.loaded })
              : t('kibana.highlightCount', { n: view.matchTotal, m: view.matchRows })
            : t('kibana.loadedCount', { n: view.loaded })}
        </span>
        {/* 高亮来源提示：查找框为空但主筛选关键词可逐字高亮时，说明高亮从哪来 */}
        {autoHighlight && !findText.trim() && (
          <span className="kb-hl-src" title={t('kibana.highlightSrcHint')}>
            {t('kibana.highlightSrc')}: {autoHighlight}
          </span>
        )}
        {hlQuery && view.matchTotal > 0 && (
          <span className="kb-match-nav">
            <button className="btn btn-xs btn-ghost" onClick={() => gotoMatch(-1)}
                    title={t('kibana.prevMatch')}>↑</button>
            <span className="kb-match-pos">{activeMatch} / {view.matchTotal}</span>
            <button className="btn btn-xs btn-ghost" onClick={() => gotoMatch(1)}
                    title={t('kibana.nextMatch')}>↓</button>
          </span>
        )}
      </div>

      {error && <div className="kb-stream-err">{error}</div>}

      <div className={`kb-log${wrap ? ' wrap' : ''}`} ref={scrollRef}>
        {rows.length === 0 ? (
          <div className="empty-hint">{busy ? t('common.loading') : (emptyHint || t('kibana.noLogs'))}</div>
        ) : view.items.length === 0 ? (
          <div className="empty-hint">
            {findText.trim() ? t('kibana.noLoadedMatch') : t('kibana.noLogs')}
          </div>
        ) : (
          <>
            {view.items.map((v) => (
              <div
                key={`${v.row.id}-${v.i}`}
                id={`kb-line-${v.i}`}
                className={`kb-line lv-${(v.row.level || 'none').toLowerCase()}${v.count > 0 ? ' has-hl' : ''}`}
                onDoubleClick={() => setCtxRow(v.row)}
                title={t('kibana.dblContext')}
                role="button"
                tabIndex={0}
                aria-label={t('kibana.dblContext')}
                onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); setCtxRow(v.row); } }}
              >
                <span className="kb-ts">{highlightNodes(v.tsText, v.mTs, v.ordStart, activeMatch)}</span>
                <span className={`kb-lv lv-${(v.row.level || 'none').toLowerCase()}`}>
                  {highlightNodes((v.row.level || '·').padEnd(5, ' '), v.mLv, v.ordStart + v.mTs.length, activeMatch)}
                </span>
                <span className="kb-src">{highlightNodes(v.srcText, v.mSrc, v.ordStart + v.mTs.length + v.mLv.length, activeMatch)}</span>
                <span className="kb-msg">{highlightNodes(v.row.msg, v.mMsg, v.ordStart + v.mTs.length + v.mLv.length + v.mSrc.length, activeMatch)}</span>
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
      {dlOpen && <KibanaDownloadModal req={req} onClose={() => setDlOpen(false)} />}
    </div>
  );
}

/** 下载对话框：时间段 / 行数上限 / 容器可自选，其余筛选条件沿用上方 query。 */
function KibanaDownloadModal({ req, onClose }: { req: LogStreamProps['req']; onClose: () => void }) {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);
  const dialogRef = useModalA11y<HTMLDivElement>(onClose);
  const startIsPreset = TIME_PRESETS.some((p) => p.key === req.start);
  const [preset, setPreset] = useState(startIsPreset ? (req.start as string) : 'custom');
  const [start, setStart] = useState(req.start || 'now-1h');
  const [end, setEnd] = useState(req.end || '');
  const [maxRows, setMaxRows] = useState(EXPORT_PAGE);
  const [container, setContainer] = useState(req.container || '');
  const [containers, setContainers] = useState<KibanaBucket[]>([]);
  const [loadingContainers, setLoadingContainers] = useState(true);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [err, setErr] = useState('');

  useEffect(() => {
    let alive = true;
    setLoadingContainers(true);
    (async () => {
      try {
        const d = await apiGet<KibanaFieldsResp>(
          `/api/kibana/fields?start=now-24h&site=${encodeURIComponent(req.site || '')}`
        );
        if (alive && d.ok !== false) setContainers(d.containers || []);
      } catch { /* 候选列表加载失败不阻塞：仍可手动输入 */ }
      finally { if (alive) setLoadingContainers(false); }
    })();
    return () => { alive = false; };
  }, [req.site]);

  const onPreset = (v: string) => {
    setPreset(v);
    if (v !== 'custom') { setStart(v); setEnd(''); }
  };

  const onDownload = async () => {
    if (busy) return;
    setBusy(true);
    setErr('');
    setProgress(0);
    try {
      // 时间段 / 行数 / 容器用弹窗里的选择覆盖，其余筛选（站点/关键词/级别/Pod…）
      // 沿用上方 query —— 保证下载到的就是「筛选后的日志」。
      const merged = { ...req, start: start || 'now-1h', end, container: container || undefined };
      const exportPost: KibanaExportPost = (path, body) => apiPost(path, body);
      const res = await fetchExportContent(merged, maxRows, setProgress, exportPost);
      const blob = new Blob([res.parts.join('\n')], { type: 'text/plain;charset=utf-8' });
      saveBlob(blob, res.filename || 'kibana-logs.log');
      addToast(res.capped ? t('kibana.exportCapped', { n: res.total })
                          : t('kibana.exported', { n: res.total }),
               res.capped ? 'warn' : 'success');
      onClose();
    } catch (ex: any) {
      setErr(ex.message || t('kibana.exportFailed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal kb-dl-modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="kb-dl-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <span id="kb-dl-title">⬇ {t('kibana.download')}</span>
          <button className="btn btn-ghost btn-sm" onClick={onClose}
                  aria-label={t('common.close')}>✕</button>
        </div>
        <div className="modal-body kb-dl-body">
          <p className="kb-dl-tip">{t('kibana.dlTip')}</p>

          <label className="kb-dl-field">
            <span className="kb-dl-label">{t('kibana.dlTimeRange')}</span>
            <select className="sel" value={preset}
                    onChange={(e) => onPreset(e.target.value)}>
              {TIME_PRESETS.map((p) => (
                <option key={p.key} value={p.key}>{t(p.labelKey)}</option>
              ))}
              <option value="custom">{t('kibana.time.custom')}</option>
            </select>
          </label>
          {preset === 'custom' && (
            <div className="kb-dl-time-inputs">
              <input className="input input-sm" value={start}
                     onChange={(e) => setStart(e.target.value)}
                     placeholder={t('kibana.time.startPh')} />
              <span className="kb-time-sep">→</span>
              <input className="input input-sm" value={end}
                     onChange={(e) => setEnd(e.target.value)}
                     placeholder={t('kibana.time.endPh')} />
            </div>
          )}

          <label className="kb-dl-field">
            <span className="kb-dl-label">{t('kibana.dlContainer')}</span>
            <select className="sel" value={container}
                    onChange={(e) => setContainer(e.target.value)}>
              <option value="">{t('kibana.dlAllContainers')}</option>
              {containers.map((b) => (
                <option key={b.key} value={b.key}>{b.key} ({b.count})</option>
              ))}
            </select>
            {loadingContainers && (
              <span className="kb-dl-loading">{t('kibana.dlLoadingContainers')}</span>
            )}
          </label>

          <label className="kb-dl-field">
            <span className="kb-dl-label">{t('kibana.dlMaxRows')}</span>
            <input className="input input-sm kb-dl-rows" type="number"
                   min={1} max={200000} value={maxRows}
                   onChange={(e) => setMaxRows(Number(e.target.value))}
                   placeholder={t('kibana.dlRowsPh')} />
          </label>

          {err && <div className="kb-stream-err">{err}</div>}
          {busy && <div className="kb-dl-progress">{t('kibana.dlProgress', { n: progress })}</div>}
        </div>
        <div className="modal-foot kb-dl-foot">
          <div className="spacer" />
          <button className="btn btn-ghost" onClick={onClose} disabled={busy}>
            {t('kibana.dlCancel')}
          </button>
          <button className="btn btn-primary" onClick={onDownload}
                  disabled={busy || !req.site}>
            {busy ? <span className="kb-spin">⬇</span> : t('kibana.dlApply')}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 每行的实时查找结果（匹配区间 + 全局序号起点），供渲染高亮与逐条定位共用 */
interface RowView {
  row: KibanaLogRow;
  /** 在 rows（已加载全集）中的下标，用作 DOM id 后缀保证稳定唯一 */
  i: number;
  tsText: string;
  srcText: string;
  mTs: Array<[number, number]>;
  mLv: Array<[number, number]>;
  mSrc: Array<[number, number]>;
  mMsg: Array<[number, number]>;
  /** 本行命中处数（四列之和） */
  count: number;
  /** 本行第一处命中的全局序号（1 起）之前的累计值 */
  ordStart: number;
}

/** 实时查找：定位 text 中所有 q 的出现区间（大小写可配）。与过滤口径同源。 */
function findMatches(text: string, q: string, caseSensitive: boolean): Array<[number, number]> {
  if (!q) return [];
  const hay = caseSensitive ? text : text.toLowerCase();
  const needle = caseSensitive ? q : q.toLowerCase();
  const out: Array<[number, number]> = [];
  let idx = hay.indexOf(needle);
  while (idx !== -1) {
    out.push([idx, idx + needle.length]);
    idx = hay.indexOf(needle, idx + needle.length);
  }
  return out;
}

/** 按命中区间把 text 切成「普通片段 / <mark>」节点；ordStart 用于标记当前选中项。 */
function highlightNodes(
  text: string,
  matches: Array<[number, number]>,
  ordStart: number,
  activeMatch: number,
): ReactNode {
  if (!matches.length) return text;
  const nodes: ReactNode[] = [];
  let last = 0;
  matches.forEach(([s, e], i) => {
    if (s > last) nodes.push(text.slice(last, s));
    const ord = ordStart + i + 1;
    nodes.push(
      <mark key={i} className={`kb-hl${ord === activeMatch ? ' active' : ''}`}>
        {text.slice(s, e)}
      </mark>
    );
    last = e;
  });
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
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
  const dialogRef = useModalA11y<HTMLDivElement>(onClose);
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
      <div
        className="modal kb-ctx-modal"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="kb-ctx-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <span id="kb-ctx-title">{t('kibana.context')} · {row.pod || row.container || ''}</span>
          <button className="btn btn-ghost btn-sm" onClick={onClose} aria-label={t('common.close')}>✕</button>
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
