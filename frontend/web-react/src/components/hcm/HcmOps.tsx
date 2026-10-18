import { useCallback, useEffect, useMemo, useState } from 'react';
import { hcmDirect } from '../../api/hcm/direct';
import { hcmEnvs, type HcmEnv } from '../../api/hcm/client';
import { useT } from '../../i18n';

/**
 * HCM 运维面板 —— 功能取自 hcm-cloud-vue（hcm-core）运维页面，走 /api/hcm/direct 零后端改动：
 *
 *  1. Redis Key   ← 网关 /server/redis 页（templates/redis/redis_list.html + static/redis/redis.js）
 *     · redis.keys.get      精确 key 查值（string 类型返回 value，其它类型显示「list对象」）
 *     · cache.possible.key  模糊联想模型缓存 key（页面「查找key」模式）
 *     · redis.key.del       删 key（参数是数组 {key:["xxx"]}）
 *     · 需要 sys-manager / hcmsysmanager 等管理角色
 *  2. 线上错误     ← core/manage/monitor/services_error.py
 *     · monitor.error.get_error_list   {time_type, curpage, perpage, begin_date?, end_date?}
 *     · monitor.error.get_error_count  同参，返回总数
 *  3. 参数解密     ← 网关 /api/api_decrypt_param（admin/apis/handlers.py，TransParamDecryptUtil）
 *     · 把抓包/日志里的 hcm_param + hcm_transfer_strategy 解回明文 JSON
 *
 * 网关选择与接口调试面板同约定：空 = 后端默认 proxy_target，可切换环境列表其它部署。
 */

type OpsTab = 'redis' | 'errors' | 'decrypt';

const TIME_TYPES = ['today', 'yesterday', 'week', 'month', 'custom'] as const;
type TimeType = (typeof TIME_TYPES)[number];

const ERR_PAGE_SIZE = 20;
const VALUE_PREVIEW = 400;

export function HcmOps() {
  const { t } = useT();
  const [tab, setTab] = useState<OpsTab>('redis');
  const [target, setTarget] = useState('');
  const [envs, setEnvs] = useState<HcmEnv[]>([]);

  useEffect(() => {
    let alive = true;
    hcmEnvs()
      .then((list) => { if (alive) setEnvs(list); })
      .catch(() => { /* 候选拉取失败不阻塞 */ });
    return () => { alive = false; };
  }, []);

  const tabs: { key: OpsTab; label: string; icon: string }[] = useMemo(() => ([
    { key: 'redis', label: t('hcm.opsTabRedis'), icon: '🧱' },
    { key: 'errors', label: t('hcm.opsTabErrors'), icon: '🚨' },
    { key: 'decrypt', label: t('hcm.opsTabDecrypt'), icon: '🔓' },
  ]), [t]);

  return (
    <div className="hcm-ops">
      <div className="hcm-ops-head">
        <div className="hcm-ops-tabs" role="tablist">
          {tabs.map((x) => (
            <button
              key={x.key}
              type="button"
              role="tab"
              aria-selected={tab === x.key}
              className={`hcm-ops-tab${tab === x.key ? ' active' : ''}`}
              onClick={() => setTab(x.key)}
            >
              <span aria-hidden="true">{x.icon}</span>
              {x.label}
            </button>
          ))}
        </div>
        <label className="hcm-ops-gw">
          <span className="hcm-api-label">{t('hcm.apiGateway')}</span>
          <select
            className="input"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            title={t('hcm.apiGatewayHint')}
          >
            <option value="">{t('hcm.apiGatewayDefault')}</option>
            {envs.map((env) => (
              <option key={env.server_url} value={env.server_url}>
                {env.name} · {env.server_url}
              </option>
            ))}
          </select>
        </label>
      </div>

      {tab === 'redis' && <RedisPanel target={target} />}
      {tab === 'errors' && <ErrorPanel target={target} />}
      {tab === 'decrypt' && <DecryptPanel target={target} />}
    </div>
  );
}

/* ================= 错误提示条（面板内共用） ================= */

function ErrBar({ msg }: { msg: string }) {
  if (!msg) return null;
  return <div className="hcm-ops-err" role="alert">{msg}</div>;
}

/* ================= ① Redis Key ================= */

interface RedisItem { key: string; value: string }

function RedisPanel({ target }: { target: string }) {
  const { t } = useT();
  const [search, setSearch] = useState('');
  const [possible, setPossible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [items, setItems] = useState<RedisItem[] | null>(null);
  const [searched, setSearched] = useState(false);
  const [detail, setDetail] = useState<RedisItem | null>(null);
  const [delTarget, setDelTarget] = useState<string | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  const [history, setHistory] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem('hcmRedisSearchHistory') || '[]'); } catch { return []; }
  });

  const pushHistory = useCallback((k: string) => {
    setHistory((prev) => {
      const next = [k, ...prev.filter((x) => x !== k)].slice(0, 10);
      try { localStorage.setItem('hcmRedisSearchHistory', JSON.stringify(next)); } catch { /* 忽略 */ }
      return next;
    });
  }, []);

  const doSearch = useCallback(async (raw?: string) => {
    const key = (raw ?? search).trim();
    if (!key) { setErr(t('hcm.opsRedisNeedKey')); return; }
    setErr('');
    setBusy(true);
    setSearched(true);
    pushHistory(key);
    try {
      let list: RedisItem[] = [];
      if (possible) {
        // cache.possible.key：联想模型缓存 key（返回 string[]，点选后可再精确查询）
        const r = await hcmDirect('', 'cache.possible.key', { key }, '', target);
        list = ((r.data as string[]) || []).map((k) => ({ key: k, value: k }));
      } else {
        // redis.keys.get：精确 key 查值（string 返回 value；其它类型值固定为「list对象」）
        const r = await hcmDirect('', 'redis.keys.get', { key }, '', target);
        list = ((r.data as { list?: RedisItem[] })?.list || []);
      }
      setItems(list);
    } catch (e: any) {
      setItems(null);
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [search, possible, target, t, pushHistory]);

  const doDelete = useCallback(async () => {
    if (!delTarget) return;
    setDelBusy(true);
    setErr('');
    try {
      // 注意：网关实现 del_key(self, key) 取 key[0]，参数必须是数组
      await hcmDirect('', 'redis.key.del', { key: [delTarget] }, '', target);
      setItems((prev) => (prev ? prev.filter((x) => x.key !== delTarget) : prev));
      setDelTarget(null);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setDelBusy(false);
    }
  }, [delTarget, target]);

  const historyBox = history.length > 0 && !searched;

  return (
    <div className="hcm-ops-body">
      <div className="hcm-ops-form">
        <input
          className="input hcm-mono hcm-ops-keyinput"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void doSearch(); }}
          placeholder={t('hcm.opsRedisSearchPlaceholder')}
          spellCheck={false}
          autoComplete="off"
        />
        <label className="chk" title={t('hcm.opsRedisPossibleHint')}>
          <input type="checkbox" checked={possible} onChange={(e) => setPossible(e.target.checked)} />
          {t('hcm.opsRedisPossible')}
        </label>
        <button className="btn btn-primary btn-sm" onClick={() => void doSearch()} disabled={busy}>
          {busy ? t('hcm.loading') : t('hcm.opsRedisSearch')}
        </button>
      </div>
      <ErrBar msg={err} />

      {historyBox && (
        <div className="hcm-ops-history">
          <div className="hcm-ops-history-title">{t('hcm.opsRedisHistory')}</div>
          {history.map((k) => (
            <button key={k} type="button" className="hcm-ops-history-item hcm-mono" onClick={() => { setSearch(k); void doSearch(k); }}>
              {k}
            </button>
          ))}
        </div>
      )}

      {searched && items !== null && (
        <div className="hcm-ops-count">{t('hcm.opsRedisFound', { n: items.length })}</div>
      )}
      {searched && items !== null && items.length === 0 && (
        <div className="hcm-ops-empty">{t('hcm.opsRedisEmpty')}</div>
      )}
      {items && items.length > 0 && (
        <div className="hcm-ops-redislist">
          {items.map((it) => (
            <div key={it.key} className="hcm-ops-redisitem">
              <button
                type="button"
                className="hcm-ops-redismain"
                onClick={() => setDetail(it)}
                title={t('hcm.opsRedisValueTip')}
              >
                <div className="hcm-ops-rediskey hcm-mono">{it.key}</div>
                <div className="hcm-ops-redisvalue hcm-mono">
                  {it.value.length > VALUE_PREVIEW ? `${it.value.slice(0, VALUE_PREVIEW)}…` : it.value}
                </div>
              </button>
              <button
                type="button"
                className="btn btn-sm hcm-ops-reddel"
                onClick={() => setDelTarget(it.key)}
                disabled={delBusy}
              >
                {t('hcm.opsRedisDelete')}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* value 详情弹层 */}
      {detail && (
        <div className="hcm-ops-mask" onClick={() => setDetail(null)}>
          <div className="hcm-ops-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="hcm-ops-dialog-head">
              <code className="hcm-mono">{detail.key}</code>
              <button type="button" className="hcm-ops-dialog-close" onClick={() => setDetail(null)} aria-label="close">×</button>
            </div>
            <pre className="hcm-ops-dialog-body hcm-mono">{detail.value}</pre>
          </div>
        </div>
      )}

      {/* 删除确认弹层（对齐参考页 .confirm） */}
      {delTarget && (
        <div className="hcm-ops-mask" onClick={() => setDelTarget(null)}>
          <div className="hcm-ops-dialog hcm-ops-dialog--confirm" onClick={(e) => e.stopPropagation()}>
            <div className="hcm-ops-dialog-msg">{t('hcm.opsRedisDelConfirm', { key: delTarget })}</div>
            <div className="hcm-ops-dialog-btns">
              <button type="button" className="btn btn-sm" onClick={() => setDelTarget(null)}>
                {t('hcm.opsCancel')}
              </button>
              <button type="button" className="btn btn-sm btn-danger" onClick={() => void doDelete()} disabled={delBusy}>
                {delBusy ? t('hcm.loading') : t('hcm.opsRedisDelete')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ================= ② 线上错误 ================= */

interface ErrorRow {
  error_code: string;
  error_info: string;
  comp_name?: string;
  emp_name?: string;
  error_time: string;
}

function ErrorPanel({ target }: { target: string }) {
  const { t } = useT();
  const [timeType, setTimeType] = useState<TimeType>('today');
  const [beginDate, setBeginDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<ErrorRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async (pg: number) => {
    setErr('');
    setBusy(true);
    try {
      const params: Record<string, unknown> = { time_type: timeType, curpage: pg, perpage: ERR_PAGE_SIZE };
      if (timeType === 'custom') {
        if (!beginDate || !endDate) { setErr(t('hcm.opsErrNeedRange')); return; }
        params.begin_date = beginDate;
        params.end_date = endDate;
      }
      // 并行拿列表 + 总数（count 失败不阻塞列表展示）
      const [listR, countR] = await Promise.all([
        hcmDirect('', 'monitor.error.get_error_list', params, '', target),
        hcmDirect('', 'monitor.error.get_error_count', {
          time_type: timeType,
          ...(timeType === 'custom' ? { begin_date: beginDate, end_date: endDate } : {}),
        }, '', target).catch(() => null),
      ]);
      setRows((listR.data as ErrorRow[]) || []);
      const c = countR?.data;
      setTotal(typeof c === 'number' ? c : Number((c as { count?: number })?.count ?? 0));
      setPage(pg);
      setExpanded(null);
    } catch (e: any) {
      setRows(null);
      setTotal(0);
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [timeType, beginDate, endDate, target, t]);

  const totalPages = Math.max(1, Math.ceil(total / ERR_PAGE_SIZE));

  return (
    <div className="hcm-ops-body">
      <div className="hcm-ops-form">
        <select
          className="input"
          value={timeType}
          onChange={(e) => { setTimeType(e.target.value as TimeType); setRows(null); setTotal(0); }}
        >
          {TIME_TYPES.map((k) => (
            <option key={k} value={k}>{t(`hcm.opsErrTime_${k}`)}</option>
          ))}
        </select>
        {timeType === 'custom' && (
          <>
            <input type="date" className="input" value={beginDate} onChange={(e) => setBeginDate(e.target.value)} />
            <span className="hcm-ops-datesep">→</span>
            <input type="date" className="input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </>
        )}
        <button className="btn btn-primary btn-sm" onClick={() => void load(1)} disabled={busy}>
          {busy ? t('hcm.loading') : t('hcm.opsErrQuery')}
        </button>
        {total > 0 && <span className="hcm-ops-count">{t('hcm.opsErrCount', { n: total })}</span>}
      </div>
      <ErrBar msg={err} />

      {rows !== null && rows.length === 0 && (
        <div className="hcm-ops-empty">{t('hcm.opsErrEmpty')}</div>
      )}
      {rows && rows.length > 0 && (
        <>
          <div className="hcm-ops-errlist">
            {rows.map((r, i) => {
              const rowKey = `${r.error_time}#${i}`;
              const open = expanded === rowKey;
              return (
                <div key={rowKey} className={`hcm-ops-erritem${open ? ' open' : ''}`}>
                  <button
                    type="button"
                    className="hcm-ops-errmain"
                    onClick={() => setExpanded(open ? null : rowKey)}
                    title={t('hcm.opsErrExpand')}
                  >
                    <code className="hcm-ops-errcode hcm-mono">{r.error_code}</code>
                    <span className="hcm-ops-errinfo">
                      {r.error_info.length > 160 && !open ? `${r.error_info.slice(0, 160)}…` : r.error_info}
                    </span>
                    <span className="hcm-ops-errmeta">
                      {[r.comp_name, r.emp_name].filter(Boolean).join(' / ')}
                      {r.comp_name || r.emp_name ? ' · ' : ''}
                      {r.error_time}
                    </span>
                  </button>
                </div>
              );
            })}
          </div>
          <div className="hcm-ops-pager">
            <button className="btn btn-sm" disabled={busy || page <= 1} onClick={() => void load(page - 1)}>
              {t('hcm.prevPage')}
            </button>
            <span className="hcm-ops-pager-info">{t('hcm.opsErrPage', { a: page, b: totalPages })}</span>
            <button className="btn btn-sm" disabled={busy || page >= totalPages} onClick={() => void load(page + 1)}>
              {t('hcm.nextPage')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

/* ================= ③ 参数解密 ================= */

function DecryptPanel({ target }: { target: string }) {
  const { t } = useT();
  const [paramText, setParamText] = useState('');
  const [strategy, setStrategy] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [result, setResult] = useState<string | null>(null);

  const run = useCallback(async () => {
    const hp = paramText.trim();
    if (!hp) { setErr(t('hcm.opsDecNeedParam')); return; }
    setErr('');
    setBusy(true);
    setResult(null);
    try {
      // 网关 /api/api_decrypt_param：外层信封加密后，handler 收到
      // {hcm_transfer_strategy, hcm_param} 并对内层 hcm_param 再解一次
      const r = await hcmDirect('', 'api_decrypt_param', {
        hcm_param: hp,
        hcm_transfer_strategy: strategy.trim() || 'ha',
      }, '', target);
      const d = r.data as { result?: unknown } | unknown;
      const out = (d as { result?: unknown })?.result !== undefined ? (d as { result?: unknown }).result : d;
      setResult(typeof out === 'string' ? out : JSON.stringify(out, null, 2));
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [paramText, strategy, target, t]);

  return (
    <div className="hcm-ops-body">
      <div className="hcm-ops-form hcm-ops-form--top">
        <label className="hcm-ops-field">
          <span className="hcm-api-label">{t('hcm.opsDecStrategy')}</span>
          <input
            className="input hcm-mono"
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            placeholder={t('hcm.opsDecStrategyPlaceholder')}
            spellCheck={false}
          />
        </label>
      </div>
      <label className="hcm-ops-field">
        <span className="hcm-api-label">{t('hcm.opsDecParam')}</span>
        <textarea
          className="input hcm-mono hcm-ops-decinput"
          value={paramText}
          onChange={(e) => setParamText(e.target.value)}
          placeholder={t('hcm.opsDecParamPlaceholder')}
          spellCheck={false}
        />
      </label>
      <span className="hcm-ops-hint">{t('hcm.opsDecHint')}</span>
      <div className="hcm-ops-form">
        <button className="btn btn-primary btn-sm" onClick={() => void run()} disabled={busy}>
          {busy ? t('hcm.loading') : t('hcm.opsDecRun')}
        </button>
      </div>
      <ErrBar msg={err} />
      {result !== null && (
        <div className="hcm-ops-decresult">
          <div className="hcm-api-label">{t('hcm.opsDecResult')}</div>
          <pre className="hcm-ops-dialog-body hcm-mono">{result}</pre>
        </div>
      )}
    </div>
  );
}
