import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { reportPanelOutput } from '../api/aiBridge';
import { apiGet, apiPost } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { requestConfirm } from '../utils/confirmStore';

/* ============================================================
   云函数「线上 ↔ 本地」一致性看板
   ------------------------------------------------------------
   解决的问题：云函数部署绕过了 CI/CD —— 现场做法是把源码写成 Python 字符串
   字面量、用 hcm.model.edit(DynamicScriptList) 直推平台，靠 md5 比对确认。
   于是没有 diff 评审、没有版本台账、没有回滚点，「线上与本地源码不一致」
   这类缺陷只能靠人撞上。

   本面板把这条链路补上：扫描 → 并排 diff → 拉取 / 推送（推送前自动归档）
   → 部署台账 → 一键回滚。
   ============================================================ */

type Status =
  | 'in_sync'
  | 'differs'
  | 'only_online'
  | 'only_local'
  | 'dup_local'
  | 'unknown';

interface Entry {
  id: string;
  category: string;
  description: string;
  enabled: boolean;
  online_mtime: string;
  online_operator: number | string;
  match_by: string;
  local_path: string;
  local_root: string;
  local_mtime: number;
  local_size: number;
  local_md5: string;
  dup_paths: string[];
  online_md5: string;
  online_size?: number;
  status: Status;
  error: string;
}

interface LocalOnly {
  id: string;
  local_path: string;
  local_mtime: number;
  local_size: number;
  local_md5: string;
  classes: string[];
  status: 'only_local';
}

interface ScanResp {
  ok: boolean;
  target: string;
  roots: string[];
  online_count: number;
  local_files: number;
  entries: Entry[];
  local_only: LocalOnly[];
  summary: Record<string, number>;
  elapsed_ms: number;
  errors: string[];
}

interface DiffResp {
  ok: boolean;
  id: string;
  local_path: string;
  same: boolean;
  diff: string;
  online: { script: string; md5: string; lines: number };
  local: { script: string; md5: string; lines: number; exists: boolean };
}

interface LedgerEntry {
  entry_id: string;
  ts: string;
  cf_id: string;
  action: 'push' | 'pull' | 'rollback';
  target: string;
  local_path: string;
  before_md5: string;
  after_md5: string;
  ok: boolean;
  note: string;
  error: string;
  attempts?: number;
  rollback_of?: string;
  has_before?: boolean;
}

const STATUS_ORDER: Status[] = ['differs', 'dup_local', 'only_online', 'in_sync', 'unknown'];

const TARGET_LS = 'jgg-cfc-target';
const ROOTS_LS = 'jgg-cfc-roots';

function fmtTime(sec: number): string {
  if (!sec) return '—';
  const d = new Date(sec * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtSize(n: number): string {
  if (!n) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function shortPath(p: string): string {
  if (!p) return '—';
  const parts = p.split('/');
  return parts.length <= 2 ? p : `…/${parts.slice(-2).join('/')}`;
}

export function CfConsistencyPanel() {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);

  const [target, setTarget] = useState(() => {
    try { return localStorage.getItem(TARGET_LS) || ''; } catch { return ''; }
  });
  const [roots, setRoots] = useState<string[]>(() => {
    try {
      const v = localStorage.getItem(ROOTS_LS);
      return v ? (JSON.parse(v) as string[]) : [];
    } catch { return []; }
  });
  /* 后端「发现」到的全部本地源码根；roots 只是其中被勾选的子集（都为空 = 用默认） */
  const [allRoots, setAllRoots] = useState<string[]>([]);
  const [ledgerRoot, setLedgerRoot] = useState('');

  const [scan, setScan] = useState<ScanResp | null>(null);

  // AI 数据桥：一致性扫描结果同步给 /api/ai/output/cfsync
  useEffect(() => {
    if (!scan) return;
    reportPanelOutput('cfsync', {
      text: JSON.stringify(scan, null, 2).slice(0, 200000),
      data: { kind: 'cfsync-scan' },
    });
  }, [scan]);
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<Status | 'all' | 'only_local'>('all');
  const [q, setQ] = useState('');
  const [sel, setSel] = useState<string>('');
  const [detail, setDetail] = useState<DiffResp | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [acting, setActing] = useState(false);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [ledgerOpen, setLedgerOpen] = useState(false);
  const [err, setErr] = useState('');
  /* 网关候选：配置默认值 + 云函数账号里出现过的服务器地址（去重）。
     输入框挂 datalist，点右侧箭头即可选，也保留手输任意地址的能力。 */
  const [gwOptions, setGwOptions] = useState<string[]>([]);
  const detailReq = useRef(0);

  /* -------- 初始配置 -------- */
  useEffect(() => {
    let alive = true;
    apiGet<{ ok: boolean; target: string; roots: string[]; ledger_root: string }>(
      '/api/cf-consistency/config'
    )
      .then((r) => {
        if (!alive) return;
        setTarget((prev) => prev || r.target || '');
        setAllRoots(r.roots || []);
        setRoots((prev) => (prev.length ? prev : r.roots || []));
        setLedgerRoot(r.ledger_root || '');
        // 网关候选列表：失败不阻塞面板（只是下拉没有候选项）
        apiGet<{ ok: boolean; items: { server_url?: string }[] }>(
          '/api/services/cloud-functions'
        )
          .then((a) => {
            if (!alive) return;
            const norm = (u: string) => (u || '').trim().replace(/\/+$/, '');
            const seen = new Set<string>();
            const opts: string[] = [];
            for (const u of [r.target, ...(a.items || []).map((i) => i.server_url || '')]) {
              const k = norm(u);
              if (k && !seen.has(k)) {
                seen.add(k);
                opts.push(k);
              }
            }
            setGwOptions(opts);
          })
          .catch(() => { /* ignore */ });
      })
      .catch((e) => alive && setErr(e.message || String(e)));
    return () => { alive = false; };
  }, []);

  /* 展示列表 = 后端发现的根 ∪ 已勾选的根。
     取并集是为了「本地勾选过一个后来又被删掉的根」时不凭空消失。 */
  const shownRoots = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const r of [...allRoots, ...roots]) {
      if (r && !seen.has(r)) {
        seen.add(r);
        out.push(r);
      }
    }
    return out;
  }, [allRoots, roots]);

  const toggleRoot = useCallback((r: string) => {
    setRoots((prev) => (prev.includes(r) ? prev.filter((x) => x !== r) : [...prev, r]));
  }, []);

  const doScan = useCallback(async () => {
    setBusy(true);
    setErr('');
    setSel('');
    setDetail(null);
    try {
      localStorage.setItem(TARGET_LS, target);
      localStorage.setItem(ROOTS_LS, JSON.stringify(roots));
    } catch { /* ignore */ }
    try {
      const r = await apiPost<ScanResp>('/api/cf-consistency/scan', {
        target,
        roots: roots.length ? roots : null,
        with_content: true,
      });
      setScan(r);
      if (r.errors?.length) setErr(r.errors.join('；'));
    } catch (e: any) {
      setErr(e.message || String(e));
      setScan(null);
    } finally {
      setBusy(false);
    }
  }, [target, roots]);

  const loadLedger = useCallback(async () => {
    try {
      const r = await apiGet<{ ok: boolean; entries: LedgerEntry[] }>(
        '/api/cf-consistency/ledger?limit=200'
      );
      setLedger(r.entries || []);
    } catch (e: any) {
      addToast(e.message || String(e), 'error');
    }
  }, [addToast]);

  useEffect(() => {
    if (ledgerOpen) void loadLedger();
  }, [ledgerOpen, loadLedger]);

  /* -------- 列表：合并 entries + local_only -------- */
  const rows = useMemo(() => {
    const out: (Entry | LocalOnly)[] = [];
    if (scan) {
      out.push(...scan.entries);
      out.push(...scan.local_only);
    }
    const kw = q.trim().toLowerCase();
    return out.filter((r) => {
      if (filter === 'only_local') {
        if (r.status !== 'only_local') return false;
      } else if (filter !== 'all' && r.status !== filter) {
        return false;
      }
      if (!kw) return true;
      const hay = `${r.id} ${'description' in r ? r.description : ''} ${'local_path' in r ? r.local_path : ''}`;
      return hay.toLowerCase().includes(kw);
    });
  }, [scan, filter, q]);

  /* 大列表只渲染前 N 条：仅本地动辄 600+ 条，全渲染会让面板明显卡顿 */
  const RENDER_CAP = 400;
  const shown = rows.slice(0, RENDER_CAP);

  const selectRow = useCallback(
    async (r: Entry | LocalOnly) => {
      setSel(r.id);
      setDetail(null);
      if (r.status === 'only_local') return; // 线上没有对应函数，无从 diff
      const req = ++detailReq.current;
      setDetailBusy(true);
      try {
        const d = await apiPost<DiffResp>('/api/cf-consistency/diff', {
          id: r.id,
          local_path: 'local_path' in r ? r.local_path : '',
          target,
        });
        if (req === detailReq.current) setDetail(d);
      } catch (e: any) {
        if (req === detailReq.current) setErr(e.message || String(e));
      } finally {
        if (req === detailReq.current) setDetailBusy(false);
      }
    },
    [target]
  );

  const selected = useMemo(
    () => rows.find((r) => r.id === sel) || null,
    [rows, sel]
  );

  const doPull = useCallback(async () => {
    if (!selected) return;
    if (
      !(await requestConfirm({
        message: t('cfc.pullConfirm', { id: selected.id }),
        danger: true,
      }))
    )
      return;
    setActing(true);
    try {
      const r = await apiPost<{ ok: boolean; local_path: string; backup: string }>(
        '/api/cf-consistency/pull',
        {
          id: selected.id,
          local_path: 'local_path' in selected ? selected.local_path : '',
          target,
          create: !('local_path' in selected && selected.local_path),
          roots: roots.length ? roots : null,
        }
      );
      addToast(t('cfc.pullOk', { path: shortPath(r.local_path) }), 'success');
      await doScan();
      if (ledgerOpen) await loadLedger();
    } catch (e: any) {
      addToast(e.message || String(e), 'error');
    } finally {
      setActing(false);
    }
  }, [selected, target, t, addToast, doScan, ledgerOpen, loadLedger]);

  const doPush = useCallback(async () => {
    if (!selected) return;
    if (
      !(await requestConfirm({
        message: t('cfc.pushConfirm', { id: selected.id }),
        danger: true,
      }))
    )
      return;
    setActing(true);
    try {
      const r = await apiPost<{ ok: boolean; entry_id: string; attempts: number; error: string }>(
        '/api/cf-consistency/push',
        {
          id: selected.id,
          local_path: 'local_path' in selected ? selected.local_path : '',
          target,
          roots: roots.length ? roots : null,
        }
      );
      if (r.ok) {
        addToast(t('cfc.pushOk', { id: selected.id, n: r.attempts }), 'success');
      } else {
        addToast(t('cfc.pushFail', { msg: r.error || '?' }), 'error');
      }
      await doScan();
      if (ledgerOpen) await loadLedger();
    } catch (e: any) {
      addToast(e.message || String(e), 'error');
    } finally {
      setActing(false);
    }
  }, [selected, target, t, addToast, doScan, ledgerOpen, loadLedger]);

  const doRollback = useCallback(
    async (entryId: string) => {
      if (
        !(await requestConfirm({
          message: t('cfc.rollbackConfirm', { id: entryId }),
          danger: true,
        }))
      )
        return;
      setActing(true);
      try {
        const r = await apiPost<{ ok: boolean; cf_id: string; error: string }>(
          '/api/cf-consistency/rollback',
          { entry_id: entryId, target }
        );
        if (r.ok) addToast(t('cfc.rollbackOk', { id: r.cf_id }), 'success');
        else addToast(t('cfc.pushFail', { msg: r.error || '?' }), 'error');
        await doScan();
        await loadLedger();
      } catch (e: any) {
        addToast(e.message || String(e), 'error');
      } finally {
        setActing(false);
      }
    },
    [target, t, addToast, doScan, loadLedger]
  );

  const summary = scan?.summary || {};
  const totalMapped = (summary.in_sync || 0) + (summary.differs || 0) + (summary.dup_local || 0);

  const renderDiff = (text: string) => {
    const lines = text.split('\n');
    return lines.map((l, i) => {
      let cls = 'cfc-diff-line';
      if (l.startsWith('+++') || l.startsWith('---')) cls += ' cfc-diff-file';
      else if (l.startsWith('@@')) cls += ' cfc-diff-hunk';
      else if (l.startsWith('+')) cls += ' cfc-diff-add';
      else if (l.startsWith('-')) cls += ' cfc-diff-del';
      return (
        <div className={cls} key={i}>
          {l || ' '}
        </div>
      );
    });
  };

  return (
    <div className="cfc-wrap">
      {/* ---------- 工具条 ---------- */}
      <div className="cfc-toolbar">
        <label className="cfc-field">
          <span className="cfc-label">
            {t('cfc.gateway')} <em className="cfc-opt">{t('cfc.optional')}</em>
          </span>
          <input
            className="cfc-input"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder={t('cfc.gatewayPlaceholder')}
            spellCheck={false}
            list="cfc-gw-options"
          />
          <datalist id="cfc-gw-options">
            {gwOptions.map((u) => (
              <option key={u} value={u} />
            ))}
          </datalist>
          {/* 留空时把后端实际解析到的网关回显出来，避免"我到底在扫哪台"存疑 */}
          {!target && scan?.target && (
            <span className="cfc-gw-echo">
              {t('cfc.gwUsing', { target: scan.target })}
            </span>
          )}
        </label>

        <div className="cfc-field cfc-field--roots">
          <span className="cfc-label">
            {t('cfc.localRoots')} <em className="cfc-opt">{t('cfc.optional')}</em>
            <span className="cfc-roots-count">
              {t('cfc.rootsSelected', { n: roots.length, total: shownRoots.length })}
            </span>
            {allRoots.length > 0 && roots.length !== allRoots.length && (
              <button
                type="button"
                className="cfc-roots-reset"
                onClick={() => setRoots(allRoots)}
                disabled={busy}
              >
                {t('cfc.rootsReset')}
              </button>
            )}
          </span>
          <div className="cfc-roots">
            {shownRoots.length ? (
              shownRoots.map((r) => {
                const on = roots.includes(r);
                return (
                  <button
                    key={r}
                    type="button"
                    className={`cfc-root-chip${on ? ' on' : ''}`}
                    title={`${r}\n${t('cfc.rootTip')}`}
                    aria-pressed={on}
                    onClick={() => toggleRoot(r)}
                  >
                    <span className="cfc-root-check" aria-hidden="true">
                      {on ? '✓' : ''}
                    </span>
                    {shortPath(r)}
                  </button>
                );
              })
            ) : (
              '—'
            )}
          </div>
        </div>

        <button className="btn btn-primary btn-sm" onClick={doScan} disabled={busy}>
          {busy ? t('cfc.scanning') : t('cfc.scan')}
        </button>
      </div>

      {scan && (
        <div className="cfc-summary">
          <button
            className={`cfc-chip${filter === 'all' ? ' active' : ''}`}
            onClick={() => setFilter('all')}
          >
            {t('cfc.fAll')} <b>{scan.online_count}</b>
          </button>
          {STATUS_ORDER.map((s) => (
            <button
              key={s}
              className={`cfc-chip cfc-chip--${s}${filter === s ? ' active' : ''}`}
              onClick={() => setFilter(s)}
              disabled={!summary[s]}
            >
              {t(`cfc.st.${s}`)} <b>{summary[s] || 0}</b>
            </button>
          ))}
          <button
            className={`cfc-chip cfc-chip--only_local${filter === 'only_local' ? ' active' : ''}`}
            onClick={() => setFilter('only_local')}
          >
            {t('cfc.st.only_local')} <b>{summary.only_local || 0}</b>
          </button>
          <span className="cfc-elapsed">
            {t('cfc.stats', {
              files: scan.local_files,
              ms: scan.elapsed_ms,
              mapped: totalMapped,
            })}
          </span>
        </div>
      )}

      {err && <div className="cfc-error" role="alert">{err}</div>}

      {/* ---------- 主体 ---------- */}
      <div className="cfc-body">
        <div className="cfc-list-col">
          <input
            className="cfc-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t('cfc.searchPlaceholder')}
            spellCheck={false}
          />
          <div className="cfc-list">
            {!scan && !busy && (
              <div className="empty-hint">
                {t('cfc.emptyTitle')}
                <span className="empty-hint-sub">{t('cfc.emptyHint')}</span>
              </div>
            )}
            {scan && !rows.length && <div className="empty-hint">{t('cfc.noMatch')}</div>}
            {shown.map((r) => (
              <button
                key={`${r.status}:${r.id}:${'local_path' in r ? r.local_path : ''}`}
                className={`cfc-item${sel === r.id ? ' active' : ''}`}
                onClick={() => selectRow(r)}
                title={r.id}
              >
                <span className={`cfc-badge cfc-badge--${r.status}`}>
                  {t(`cfc.st.${r.status}`)}
                </span>
                <span className="cfc-item-main">{r.id}</span>
                <span className="cfc-item-sub">
                  {'local_path' in r && r.local_path
                    ? shortPath(r.local_path)
                    : t('cfc.noLocalFile')}
                  {r.status === 'dup_local' && ` · +${(r as Entry).dup_paths.length}`}
                </span>
              </button>
            ))}
            {rows.length > RENDER_CAP && (
              <div className="cfc-more">{t('cfc.more', { n: rows.length - RENDER_CAP })}</div>
            )}
          </div>
        </div>

        {/* ---------- 详情 ---------- */}
        <div className="cfc-detail">
          {!selected && <div className="empty-hint">{t('cfc.pickOne')}</div>}

          {selected && (
            <>
              <div className="cfc-detail-head">
                <div className="cfc-detail-title">
                  <span className={`cfc-badge cfc-badge--${selected.status}`}>
                    {t(`cfc.st.${selected.status}`)}
                  </span>
                  <code>{selected.id}</code>
                </div>
                <div className="cfc-actions">
                  {selected.status !== 'only_local' && (
                    <>
                      <button className="btn btn-sm" onClick={doPull} disabled={acting}>
                        {t('cfc.pull')}
                      </button>
                      <button
                        className="btn btn-sm btn-primary"
                        onClick={doPush}
                        disabled={acting || !('local_path' in selected && selected.local_path)}
                        title={
                          'local_path' in selected && selected.local_path
                            ? undefined
                            : t('cfc.pushNeedLocal')
                        }
                      >
                        {t('cfc.push')}
                      </button>
                    </>
                  )}
                </div>
              </div>

              <div className="cfc-kv">
                {'description' in selected && selected.description && (
                  <div><span>{t('cfc.desc')}</span><em>{selected.description}</em></div>
                )}
                {'local_path' in selected && selected.local_path && (
                  <div>
                    <span>{t('cfc.local')}</span>
                    <em title={selected.local_path}>
                      {selected.local_path}
                      {'local_root' in selected && selected.local_root && (
                        <small> · {shortPath(selected.local_root)}</small>
                      )}
                    </em>
                  </div>
                )}
                {'local_path' in selected && selected.local_path && (
                  <div>
                    <span>{t('cfc.localMeta')}</span>
                    <em>{fmtTime(selected.local_mtime)} · {fmtSize(selected.local_size)}</em>
                  </div>
                )}
                {'online_mtime' in selected && selected.online_mtime && (
                  <div><span>{t('cfc.onlineMeta')}</span><em>{selected.online_mtime}</em></div>
                )}
                {selected.status === 'dup_local' && (
                  <div className="cfc-kv-warn">
                    <span>{t('cfc.dupWarn')}</span>
                    <em>
                      {(selected as Entry).dup_paths.map((p) => (
                        <div key={p}>{p}</div>
                      ))}
                    </em>
                  </div>
                )}
                {'match_by' in selected && selected.match_by === 'entry' && (
                  <div>
                    <span>{t('cfc.matchBy')}</span>
                    <em>{t('cfc.matchByEntry')}</em>
                  </div>
                )}
              </div>

              {detailBusy && <div className="cfc-detail-busy">{t('cfc.diffing')}</div>}

              {detail && !detailBusy && (
                <>
                  <div className="cfc-diff-head">
                    <span>{t('cfc.diffTitle')}</span>
                    <span className="cfc-diff-stat">
                      {t('cfc.diffStat', {
                        a: detail.local.lines,
                        b: detail.online.lines,
                      })}
                    </span>
                  </div>
                  {detail.same ? (
                    <div className="cfc-same">{t('cfc.same')}</div>
                  ) : (
                    <pre className="cfc-diff">{renderDiff(detail.diff)}</pre>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </div>

      {/* ---------- 部署台账 ---------- */}
      <div className="cfc-ledger">
        <button
          className="cfc-ledger-head"
          onClick={() => setLedgerOpen((o) => !o)}
          aria-expanded={ledgerOpen}
        >
          <span className="cfc-ledger-caret" aria-hidden>{ledgerOpen ? '▾' : '▸'}</span>
          {t('cfc.ledger')}
          <span className="cfc-ledger-count">{ledger.length || ''}</span>
          {ledgerRoot && <code className="cfc-ledger-root">{shortPath(ledgerRoot)}</code>}
        </button>
        {ledgerOpen && (
          <div className="cfc-ledger-body">
            {!ledger.length && <div className="empty-hint">{t('cfc.ledgerEmpty')}</div>}
            {!!ledger.length && (
              <table className="cfc-ledger-table">
                <thead>
                  <tr>
                    <th>{t('cfc.colTime')}</th>
                    <th>{t('cfc.colAction')}</th>
                    <th>{t('cfc.colCf')}</th>
                    <th>{t('cfc.colResult')}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {ledger.map((e) => (
                    <tr key={e.entry_id}>
                      <td className="cfc-mono">{e.ts}</td>
                      <td>{t(`cfc.act.${e.action}`)}</td>
                      <td className="cfc-mono">{e.cf_id}</td>
                      <td>
                        <span className={e.ok ? 'cfc-ok' : 'cfc-bad'}>
                          {e.ok ? t('cfc.resOk') : t('cfc.resFail')}
                        </span>
                        {e.attempts ? <small> ×{e.attempts}</small> : null}
                      </td>
                      <td>
                        <button
                          className="btn btn-xs"
                          disabled={acting || !e.has_before}
                          title={e.has_before ? undefined : t('cfc.noArchive')}
                          onClick={() => doRollback(e.entry_id)}
                        >
                          {t('cfc.rollback')}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
