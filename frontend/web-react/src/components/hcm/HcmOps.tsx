import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { hcmDirect } from '../../api/hcm/direct';
import { hcmEnvs, type HcmEnv } from '../../api/hcm/client';
import { reportLocalApiNetworkError } from '../../api/client';
import { useAppStore } from '../../store/useAppStore';
import { copyText } from '../../utils/clipboard';
import { useT } from '../../i18n';
import { TabIcon } from '../icons';

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
    { key: 'redis', label: t('hcm.opsTabRedis'), icon: 'database' },
    { key: 'errors', label: t('hcm.opsTabErrors'), icon: 'alert' },
    { key: 'decrypt', label: t('hcm.opsTabDecrypt'), icon: 'unlock' },
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
              <span aria-hidden="true"><TabIcon name={x.icon} size={13} /></span>
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

/**
 * 常用 Key 快捷模板（前缀全部取自 hcm-core 源码中的真实缓存 key）。
 * 实测（京投-测试环境）实例化格式 = 源码字面量完整保留 + ":" + 值
 * （如 CompanySetting:company_id:3929 / CompanyThirdBind:company_id:3929 均验证命中）。
 * {cid} 由「公司 ID」输入填充；其余占位（{emp_id}/{phone} 等）回填搜索框后手工补全。
 * 部分 key 在特定环境可能未被实例化（查询为空属正常）。
 */
const COMMON_KEY_PRESETS: { tpl: string; note?: string }[] = [
  { tpl: 'CompanySetting:company_id:{cid}', note: '公司设置' },
  { tpl: 'CompanyThirdBind:company_id:{cid}', note: '第三方绑定' },
  { tpl: 'SystemFunction:company_id:{cid}', note: '功能开关' },
  { tpl: 'SystemEmployeeRoles:company_id:role:', note: '员工角色（末尾补角色路径）' },
  { tpl: 'SystemManager:company_id:manger_type:', note: '系统管理员（末尾补类型）' },
  { tpl: 'SecuritySystem:company_id:{cid}', note: '安全体系' },
  { tpl: 'SsoPlatformConfig:company_id:{cid}', note: 'SSO 配置' },
  { tpl: 'DynamicScriptList:company_id:type', note: '动态脚本（末段补类型）' },
  { tpl: 'ControlDynamicScriptList:company_id:type', note: '控件动态脚本（末段补类型）' },
  { tpl: 'SystemUpdateMessage:company_id:{cid}', note: '系统更新消息' },
  { tpl: 'UserGuide:company_id:{cid}', note: '用户引导' },
  { tpl: 'Employee:{emp_id}', note: '员工缓存（填员工 id）' },
  { tpl: '__valid_code_{phone}', note: '手机验证码（填手机号）' },
];

function RedisPanel({ target }: { target: string }) {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);
  const [search, setSearch] = useState('');
  const [possible, setPossible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [items, setItems] = useState<RedisItem[] | null>(null);
  const [searched, setSearched] = useState(false);
  const [detail, setDetail] = useState<RedisItem | null>(null);
  const [delTarget, setDelTarget] = useState<string | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  /** 上次查询是否为联想模式（联想返回的是模板 key，点选回填而非删除） */
  const [pickMode, setPickMode] = useState(false);
  const [history, setHistory] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem('hcmRedisSearchHistory') || '[]'); } catch { return []; }
  });
  // —— 批量删除 ——
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchProg, setBatchProg] = useState<{ done: number; total: number } | null>(null);
  const [batchTarget, setBatchTarget] = useState<null | 'all'>(null);
  // —— 常用 Key ——
  const [cid, setCid] = useState('');

  // 切网关即清空旧结果：运维场景下把 A 环境的 key/值误当 B 环境是高危误判
  useEffect(() => {
    setItems(null);
    setSearched(false);
    setPickMode(false);
    setErr('');
    setDetail(null);
    setDelTarget(null);
    setSelected(new Set());
    setBatchProg(null);
  }, [target]);

  // 弹层 ESC 关闭（详情 / 单删与批删确认共用）
  useEffect(() => {
    if (!detail && !delTarget && !batchTarget) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setDetail(null); setDelTarget(null); setBatchTarget(null); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [detail, delTarget, batchTarget]);

  // 复制 key/value（项目约定走 utils/clipboard 的 copyText + 全局 toast）
  const copyAndToast = useCallback(async (text: string) => {
    const ok = await copyText(text);
    addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
  }, [addToast, t]);

  const pushHistory = useCallback((k: string) => {
    setHistory((prev) => {
      const next = [k, ...prev.filter((x) => x !== k)].slice(0, 10);
      try { localStorage.setItem('hcmRedisSearchHistory', JSON.stringify(next)); } catch { /* 忽略 */ }
      return next;
    });
  }, []);

  const clearHistory = useCallback(() => {
    setHistory([]);
    try { localStorage.removeItem('hcmRedisSearchHistory'); } catch { /* 忽略 */ }
  }, []);

  // 删除确认弹层出现时聚焦「删除」按钮（Enter 直达，Esc 取消）
  const delBtnRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (delTarget) delBtnRef.current?.focus();
  }, [delTarget]);

  const doSearch = useCallback(async (raw?: string, possibleOverride?: boolean) => {
    const key = (raw ?? search).trim();
    if (!key) { setErr(t('hcm.opsRedisNeedKey')); return; }
    const usePossible = possibleOverride ?? possible;
    setErr('');
    setBusy(true);
    setSearched(true);
    setPickMode(usePossible);
    pushHistory(key);
    try {
      let list: RedisItem[] = [];
      if (usePossible) {
        // cache.possible.key：联想模型缓存 key（返回 string[]，点选后回填搜索框再精确查询）
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

  // 联想模式点选：回填搜索框并自动转精确查询（对齐参考页 viewContent 行为）
  const pickPossibleKey = useCallback((k: string) => {
    setSearch(k);
    setPossible(false);
    setDetail(null);
    void doSearch(k, false);
  }, [doSearch]);

  const doDelete = useCallback(async () => {
    if (!delTarget) return;
    setDelBusy(true);
    setErr('');
    try {
      // 注意：网关实现 del_key(self, key) 取 key[0]，参数必须是数组
      await hcmDirect('', 'redis.key.del', { key: [delTarget] }, '', target);
      setItems((prev) => (prev ? prev.filter((x) => x.key !== delTarget) : prev));
      setDelTarget(null);
      addToast(t('hcm.opsRedisDelDone', { key: delTarget }), 'success');
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setDelBusy(false);
    }
  }, [delTarget, target, addToast, t]);

  // —— 批量删除：逐个调 redis.key.del（网关实现一次只删 key[0]），完成后统一反馈 ——
  const batchDelete = useCallback(async () => {
    if (!batchTarget || selected.size === 0) return;
    const keys = [...selected];
    setBatchBusy(true);
    setBatchProg({ done: 0, total: keys.length });
    const failed: string[] = [];
    let ok = 0;
    for (const k of keys) {
      try {
        await hcmDirect('', 'redis.key.del', { key: [k] }, '', target);
        ok++;
      } catch {
        failed.push(k);
      }
      setBatchProg((p) => ({ done: (p?.done ?? 0) + 1, total: keys.length }));
    }
    setItems((prev) => (prev ? prev.filter((x) => !selected.has(x.key)) : prev));
    setSelected(new Set());
    setBatchTarget(null);
    setBatchProg(null);
    setBatchBusy(false);
    if (failed.length) setErr(t('hcm.opsRedisBatchFailList', { list: failed.join(', ') }));
    addToast(t('hcm.opsRedisBatchDone', { ok, fail: failed.length }), failed.length ? 'warn' : 'success');
  }, [batchTarget, selected, target, addToast, t]);

  const toggleSel = useCallback((k: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  }, []);

  // —— 常用 Key：模板 {cid} 用公司 ID 填充，其余占位留在搜索框手工补 ——
  const applyPreset = useCallback((tpl: string) => {
    const key = tpl.replace(/\{cid\}/g, cid.trim());
    setSearch(key);
    setPossible(false);
    void doSearch(key, false);
  }, [cid, doSearch]);

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
          <button type="button" className="hcm-ops-history-clear" onClick={clearHistory}>
            {t('hcm.opsRedisHistoryClear')}
          </button>
        </div>
      )}

      {/* 常用 Key（前缀取自 hcm-core 源码；{cid} 由公司 ID 输入填充） */}
      <div className="hcm-ops-presets">
        <div className="hcm-ops-history-title">
          {t('hcm.opsRedisCommon')}
          <input
            className="input hcm-mono hcm-ops-cid"
            value={cid}
            onChange={(e) => setCid(e.target.value)}
            placeholder={t('hcm.opsRedisCidPlaceholder')}
            spellCheck={false}
            title={t('hcm.opsRedisCidPlaceholder')}
          />
        </div>
        <div className="hcm-ops-presetlist">
          {COMMON_KEY_PRESETS.map((p) => (
            <button
              key={p.tpl}
              type="button"
              className="hcm-ops-preset hcm-mono"
              onClick={() => applyPreset(p.tpl)}
              title={p.note ? `${p.note} · ${p.tpl}` : p.tpl}
            >
              {p.note || p.tpl}
            </button>
          ))}
        </div>
      </div>

      {searched && items !== null && (
        <div className="hcm-ops-count">
          {!pickMode && items.length > 0 && (
            <label className="chk hcm-ops-selall">
              <input
                type="checkbox"
                checked={selected.size === items.length && items.length > 0}
                onChange={(e) => setSelected(e.target.checked ? new Set(items.map((x) => x.key)) : new Set())}
              />
              {t('hcm.opsRedisSelectAll')}
            </label>
          )}
          {t('hcm.opsRedisFound', { n: items.length })}
        </div>
      )}
      {searched && items !== null && items.length === 0 && (
        <div className="hcm-ops-empty">{t('hcm.opsRedisEmpty')}</div>
      )}
      {items && items.length > 0 && (
        <div className="hcm-ops-redislist">
          {items.map((it) => (
            <div key={it.key} className={`hcm-ops-redisitem${selected.has(it.key) ? ' hcm-ops-redisitem--sel' : ''}`}>
              {!pickMode && (
                <label className="hcm-ops-redissel">
                  <input
                    type="checkbox"
                    checked={selected.has(it.key)}
                    onChange={() => toggleSel(it.key)}
                    aria-label={`${t('hcm.opsRedisSelectAll')}: ${it.key}`}
                  />
                </label>
              )}
              <button
                type="button"
                className="hcm-ops-redismain"
                onClick={() => (pickMode ? pickPossibleKey(it.key) : setDetail(it))}
                title={pickMode ? t('hcm.opsRedisPickTip') : t('hcm.opsRedisValueTip')}
              >
                <div className="hcm-ops-rediskey hcm-mono">{it.key}</div>
                {!pickMode && (
                  <div className="hcm-ops-redisvalue hcm-mono">
                    {it.value
                      ? (it.value.length > VALUE_PREVIEW ? `${it.value.slice(0, VALUE_PREVIEW)}…` : it.value)
                      : <span className="hcm-ops-redisvalue--empty">{t('hcm.opsRedisEmptyValue')}</span>}
                  </div>
                )}
              </button>
              {/* 联想模式返回的是模板 key（Redis 中不存在），不提供删除 */}
              {!pickMode && (
                <button
                  type="button"
                  className="btn btn-sm hcm-ops-reddel"
                  onClick={() => setDelTarget(it.key)}
                  disabled={delBusy}
                >
                  {t('hcm.opsRedisDelete')}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 批量操作条（有勾选时出现） */}
      {selected.size > 0 && !pickMode && (
        <div className="hcm-ops-batchbar">
          <span className="hcm-ops-count">
            {batchProg
              ? t('hcm.opsRedisBatchProg', { a: batchProg.done, b: batchProg.total })
              : t('hcm.opsRedisSelCount', { n: selected.size })}
          </span>
          <button
            type="button"
            className="btn btn-sm btn-danger"
            onClick={() => setBatchTarget('all')}
            disabled={batchBusy}
          >
            {t('hcm.opsRedisBatchDel', { n: selected.size })}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setSelected(new Set())}
            disabled={batchBusy}
          >
            {t('hcm.opsRedisBatchClearSel')}
          </button>
        </div>
      )}

      {/* value 详情弹层 */}
      {detail && (
        <div className="hcm-ops-mask" onClick={() => setDetail(null)}>
          <div className="hcm-ops-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="hcm-ops-dialog-head">
              <code className="hcm-mono">{detail.key}</code>
              <span className="hcm-ops-dialog-actions">
                <button
                  type="button"
                  className="hcm-ops-dialog-close"
                  onClick={() => void copyAndToast(detail.value)}
                  title={t('hcm.opsCopy')}
                  aria-label={t('hcm.opsCopy')}
                >⧉</button>
                <button type="button" className="hcm-ops-dialog-close" onClick={() => setDetail(null)} aria-label="close">×</button>
              </span>
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
              <button
                type="button" ref={delBtnRef}
                className="btn btn-sm btn-danger" onClick={() => void doDelete()} disabled={delBusy}
              >
                {delBusy ? t('hcm.loading') : t('hcm.opsRedisDelete')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 批量删除确认弹层 */}
      {batchTarget && (
        <div className="hcm-ops-mask" onClick={() => setBatchTarget(null)}>
          <div className="hcm-ops-dialog hcm-ops-dialog--confirm" onClick={(e) => e.stopPropagation()}>
            <div className="hcm-ops-dialog-msg">
              {t('hcm.opsRedisBatchConfirm', { n: selected.size })}
              <div className="hcm-ops-batchkeys hcm-mono">
                {[...selected].map((k) => <div key={k}>{k}</div>)}
              </div>
            </div>
            <div className="hcm-ops-dialog-btns">
              <button type="button" className="btn btn-sm" onClick={() => setBatchTarget(null)}>
                {t('hcm.opsCancel')}
              </button>
              <button
                type="button"
                className="btn btn-sm btn-danger"
                onClick={() => void batchDelete()}
                disabled={batchBusy}
              >
                {batchBusy ? t('hcm.loading') : t('hcm.opsRedisBatchDel', { n: selected.size })}
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
  const addToast = useAppStore((s) => s.addToast);
  const [timeType, setTimeType] = useState<TimeType>('today');
  const [beginDate, setBeginDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<ErrorRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  // 切网关清空旧环境数据（避免把 A 环境错误数当成 B 环境）
  useEffect(() => {
    setRows(null);
    setTotal(0);
    setErr('');
    setExpanded(null);
  }, [target]);

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

  // 单条错误一键复制（[错误码] 环境/人 时间 + 完整信息，便于贴给 AI / 工单）
  const copyRow = useCallback(async (r: ErrorRow) => {
    const text = `[${r.error_code}] ${[r.comp_name, r.emp_name].filter(Boolean).join('/')} ${r.error_time}\n${r.error_info}`;
    const ok = await copyText(text);
    addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
  }, [addToast, t]);

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
        {busy && <span className="hcm-api-busy" aria-label="loading" />}
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
                  <button
                    type="button"
                    className="hcm-ops-errcopy"
                    onClick={() => void copyRow(r)}
                    title={t('hcm.opsErrCopy')}
                    aria-label={t('hcm.opsErrCopy')}
                  >⧉</button>
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
  const addToast = useAppStore((s) => s.addToast);
  const [paramText, setParamText] = useState('');
  const [strategy, setStrategy] = useState('');
  const [s3h, setS3h] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [result, setResult] = useState<string | null>(null);

  // 切网关清掉旧解密结果（同一密文在不同环境钥下结果不同）
  useEffect(() => {
    setResult(null);
    setErr('');
  }, [target]);

  const run = useCallback(async () => {
    const hp = paramText.trim();
    if (!hp) { setErr(t('hcm.opsDecNeedParam')); return; }
    setErr('');
    setBusy(true);
    setResult(null);
    try {
      // 专用端点：后端明文直调网关 /api/api_decrypt_param（handler 自身解两层，
      // 不能走 /api/hcm/direct 的信封通道——否则 handler pop 键后 execute 取不到）。
      // s3h 留空时后端按 hcm_param 自动计算签名。
      let res: Response;
      try {
        res = await fetch('/api/hcm/decrypt', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            hcm_param: hp,
            hcm_transfer_strategy: strategy.trim(),
            s3h: s3h.trim(),
            target,
          }),
        });
      } catch (e) {
        reportLocalApiNetworkError('/api/hcm/decrypt', e);
        throw e;
      }
      const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      if (!res.ok) {
        throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${res.status}`);
      }
      if (body.ok === false) {
        throw new Error(body?.error?.errmsg || body?.error?.message || `HTTP ${res.status}`);
      }
      const out = body.data;
      setResult(typeof out === 'string' ? out : JSON.stringify(out, null, 2));
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [paramText, strategy, s3h, target, t]);

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
        <label className="hcm-ops-field">
          <span className="hcm-api-label">{t('hcm.opsDecS3h')}</span>
          <input
            className="input hcm-mono hcm-ops-s3h"
            value={s3h}
            onChange={(e) => setS3h(e.target.value)}
            placeholder={t('hcm.opsDecS3hPlaceholder')}
            spellCheck={false}
            autoComplete="off"
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
          <div className="hcm-api-label">
            {t('hcm.opsDecResult')}
            <button
              type="button"
              className="btn btn-sm hcm-api-format"
              onClick={async () => {
                const ok = await copyText(result);
                addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
              }}
            >
              {t('hcm.opsCopy')}
            </button>
          </div>
          <pre className="hcm-ops-dialog-body hcm-mono">{result}</pre>
        </div>
      )}
    </div>
  );
}
