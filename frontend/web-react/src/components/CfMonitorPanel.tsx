/**
 * 云函数运行状态监控面板。
 *
 * 把「按云函数聚合日志 → 一块健康度看板」做成 UI，数据全部来自后端
 * ``/api/cf/monitor``（复用 cf_query_logs 的鉴权/重登/模型降级链路，
 * 与命令行工具 tools/cf_monitor.py 共用同一套聚合规则）。
 *
 * 面板职责：
 * 1. 选环境（后端给的可监控列表，标注 token 是否可用）；
 * 2. 一键/定时抓取一轮，渲染「云函数 → 总数/错误/错误率/健康度」表格；
 * 3. 点开某行看错误码、阶段、最近错误详情；
 * 4. 空状态要教人做事（无环境/未登录/取数失败三种情况分别给可执行建议）。
 *
 * 设计取向（见 .impeccable.md）：状态可辨优于装饰；颜色只表达
 * 「出错/告警/正常」，选中用主色弱底而不是橙色。
 */
import { Fragment, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet, apiPost } from '../api/client';
import { useT } from '../i18n';
import { TabIcon } from './icons';
import { EmptyState } from './EmptyState';
import { PanelActiveContext } from '../utils/panelActive';

/** 单个云函数的聚合状态（与后端 api.cf.cf_monitor.aggregate 的字段一一对应）。 */
type FuncStatus = {
  name: string;
  total: number;
  error: number;
  warn: number;
  info: number;
  error_ratio: number;
  health: 'OK' | 'WARN' | 'ERROR' | 'UNKNOWN';
  last_time: string;
  last_message: string;
  last_error: string;
  last_error_full?: string;
  last_error_time: string;
  top_errcodes: { code: string; count: number }[];
  top_stages: { stage: string; count: number }[];
  error_samples?: { time: string; short: string; full: string }[];
};

type MonitorResp = {
  ok: boolean;
  server_url: string;
  at?: string;
  record_model?: string;
  models_tried?: string[];
  rows_fetched?: number;
  /** 过滤前实际抓到的行数（与 rows_fetched 的差就是被时间窗口剔除的）。 */
  rows_queried?: number;
  platform_total?: number;
  time_range?: TimeRange;
  summary?: {
    functions: number;
    total_rows: number;
    errors: number;
    warnings: number;
    error_functions: number;
    warn_functions: number;
    ok_functions: number;
  };
  functions: FuncStatus[];
  error?: string;
};

type ServerInfo = {
  name: string;
  server_url: string;
  domain: string;
  has_token: boolean;
  has_credentials: boolean;
  last_error: string;
};

/** 后端 /api/cf/monitor/models 返回的一条记录模型。 */
type RecordModelInfo = {
  name: string;
  label: string;
  /** 该模型下「只看某函数」用的字段名（dynamic_log→log_type，其余→name）。 */
  type_field: string;
};

/** 后端 time_range 字段（成功/失败路径都会给全，前端不用判存在性）。 */
type TimeRange = {
  start: string;
  end: string;
  /** 本轮采样实际覆盖到的时间跨度（过滤前的原始行集）。 */
  sampled_from: string;
  sampled_to: string;
  /** 采样条数中落在窗口外被剔除的条数。 */
  out_of_window: number;
  /** 翻页没翻完 + 窗口起点比采样最早一行还早 → 采样不完整。 */
  truncated: boolean;
};

/** 抓取规模：单页 200 条会被高频函数独占（UI 给 3 档，默认「标准」=1000×3）。 */
const SCALES = [
  { key: 'quick', pageSize: 1000, pages: 1 },
  { key: 'normal', pageSize: 1000, pages: 3 },
  { key: 'deep', pageSize: 1000, pages: 6 },
] as const;
type ScaleKey = (typeof SCALES)[number]['key'];

const REFRESH_OPTS = [0, 15, 30, 60, 300];
const PREFS_KEY = 'jgg-cf-monitor';

function loadPrefs(): {
  server: string; scale: ScaleKey; refreshSec: number;
  recordModel: string; timeStart: string; timeEnd: string;
} {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      return {
        server: typeof p.server === 'string' ? p.server : '',
        scale: SCALES.some((s) => s.key === p.scale) ? p.scale : 'normal',
        refreshSec: REFRESH_OPTS.includes(p.refreshSec) ? p.refreshSec : 0,
        recordModel: typeof p.recordModel === 'string' ? p.recordModel : 'auto',
        timeStart: typeof p.timeStart === 'string' ? p.timeStart : '',
        timeEnd: typeof p.timeEnd === 'string' ? p.timeEnd : '',
      };
    }
  } catch { /* ignore */ }
  return { server: '', scale: 'normal', refreshSec: 0, recordModel: 'auto', timeStart: '', timeEnd: '' };
}

function pct(ratio: number): string {
  return `${(ratio * 100).toFixed(ratio > 0 && ratio < 0.001 ? 2 : 1)}%`;
}

export function CfMonitorPanel() {
  const { t } = useT();
  const pushLog = useAppStore((s) => s.pushLog);
  const active = useContext(PanelActiveContext);

  const prefs = useMemo(loadPrefs, []);
  const [servers, setServers] = useState<ServerInfo[] | null>(null);
  // 环境列表本身是否加载失败（与「加载成功但为空」是两种空态，必须区分：
  // 前者多半是后端起的是旧版本 / 请求失败，后者是配置里确实没有环境）
  const [serversError, setServersError] = useState('');
  // 记录环境列表请求的 HTTP 状态：404 说明后端是旧版本（没有本接口），
  // 这和「网络失败 / 后端没起来」是两回事，提示要分开给。
  const [serversStatus, setServersStatus] = useState(0);
  const [server, setServer] = useState(prefs.server);
  const [scale, setScale] = useState<ScaleKey>(prefs.scale);
  const [refreshSec, setRefreshSec] = useState(prefs.refreshSec);
  // 记录模型：'auto'（默认）按 dynamic_log → SyncOuterRecord → SysLogRecord 探测。
  // 选具体模型后**不**降级 —— 用户明确指定了，猜另一个模型只会把错误藏起来。
  const [recordModel, setRecordModel] = useState(prefs.recordModel);
  const [models, setModels] = useState<RecordModelInfo[]>([]);
  // 时间窗口（datetime-local 值，本地时区）。起止都空 = 不限。
  const [timeStart, setTimeStart] = useState(prefs.timeStart);
  const [timeEnd, setTimeEnd] = useState(prefs.timeEnd);
  const [data, setData] = useState<MonitorResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<string>('');
  const [onlyProblems, setOnlyProblems] = useState(false);
  // 展开的是第几条错误样本（-1 = 都收起）；切换函数时重置
  const [openSample, setOpenSample] = useState(-1);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  // 偏好持久化
  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        server, scale, refreshSec, recordModel, timeStart, timeEnd,
      }));
    } catch { /* ignore */ }
  }, [server, scale, refreshSec, recordModel, timeStart, timeEnd]);

  // 记录模型清单：加载失败不影响主流程（下拉退化成只有「自动」）
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await apiGet<{ ok: boolean; models: RecordModelInfo[] }>(
          '/api/cf/monitor/models',
        );
        if (alive) setModels(r.models || []);
      } catch {
        if (alive) setModels([]);
      }
    })();
    return () => { alive = false; };
  }, []);

  const loadServers = useCallback(async (): Promise<ServerInfo[]> => {
    try {
      const r = await apiGet<{ ok: boolean; servers: ServerInfo[]; error?: string }>(
        '/api/cf/monitor/servers',
      );
      const list = r.servers || [];
      if (alive.current) {
        setServers(list);
        setServersError(r.ok === false ? (r.error || t('cfmon.emptyLoadFailHint')) : '');
        setServersStatus(0);
        // 首次自动选「最可能直接能用」的环境：有 token 且上次没报错优先，
        // 其次有 token 的，最后才退到第一个（避免一进面板就选中注定失败的项）
        setServer((prev) => {
          if (prev && list.some((s) => s.server_url === prev)) return prev;
          const usable =
            list.find((s) => s.has_token && !s.last_error) ||
            list.find((s) => s.has_token) ||
            list[0];
          return usable ? usable.server_url : '';
        });
      }
      return list;
    } catch (ex) {
      if (alive.current) {
        setServers([]);
        setServersError(ex instanceof Error ? ex.message : String(ex));
        const st = (ex as { status?: number })?.status;
        setServersStatus(typeof st === 'number' ? st : 0);
      }
      return [];
    }
  }, []);

  useEffect(() => { loadServers(); }, [loadServers]);

  const run = useCallback(async (silent = false) => {
    const target = (server || '').trim();
    if (!target) {
      if (!silent) setError(t('cfmon.errNoServer'));
      return;
    }
    if (!silent) setBusy(true);
    setError('');
    const sc = SCALES.find((s) => s.key === scale) || SCALES[1];
    try {
      const r = await apiPost<MonitorResp>('/api/cf/monitor', {
        server_url: target,
        page_size: sc.pageSize,
        pages: sc.pages,
        record_model: (recordModel || 'auto').trim() || 'auto',
        time_start: (timeStart || '').trim(),
        time_end: (timeEnd || '').trim(),
      });
      if (!alive.current) return;
      setData(r);
      if (!r.ok) {
        setError(r.error || t('cfmon.errFetch'));
        if (!silent) pushLog(`[云函数监控] 取数失败：${r.error || ''}`, 'warning');
      }
    } catch (ex) {
      if (!alive.current) return;
      const msg = ex instanceof Error ? ex.message : String(ex);
      setError(msg);
      if (!silent) pushLog(`[云函数监控] ${msg}`, 'error');
    } finally {
      if (alive.current && !silent) setBusy(false);
    }
  }, [server, scale, t, pushLog, recordModel, timeStart, timeEnd]);

  // 首次进入面板（且已选好环境）自动跑一轮，避免用户对着空白页。
  // 注意：环境列表还没加载完 / 加载失败 / 列表为空 / 当前环境没 token 时都不跑 ——
  // 否则会额外弹一条「请先选择环境」的错误，和空态提示叠在一起（实测踩过）。
  const autoRan = useRef(false);
  useEffect(() => {
    if (autoRan.current || !active) return;
    if (servers === null || serversError) return;      // 列表未就绪或加载失败
    if (servers.length === 0) return;                   // 确实没有环境
    if (!server) return;
    const cur = servers.find((s) => s.server_url === server);
    if (!cur || !cur.has_token) return;                 // 没登录先别跑，省得报错刷屏
    autoRan.current = true;
    run();
  }, [server, servers, serversError, active, run]);

  // 定时刷新（面板隐藏时暂停，回到前台立即补一轮）
  useEffect(() => {
    if (!refreshSec || !active) return;
    const timer = window.setInterval(() => { run(true); }, refreshSec * 1000);
    return () => window.clearInterval(timer);
  }, [refreshSec, active, run]);

  // 切到前台且已有数据时，补一轮（隐藏期间暂停了）
  const prevActive = useRef(active);
  useEffect(() => {
    if (active && !prevActive.current && refreshSec && server) run(true);
    prevActive.current = active;
  }, [active, refreshSec, server, run]);

  const funcs = data?.functions || [];
  const shown = onlyProblems
    ? funcs.filter((f) => f.error > 0)
    : funcs;
  const sum = data?.summary;

  const curServer = servers?.find((s) => s.server_url === server);

  // ── 时间窗口 / 记录模型的展示派生值 ──
  const tr = data?.time_range;
  const curModel = models.find((m) => m.name === recordModel);
  const typeField = curModel?.type_field || 'log_type';
  /** 当前选的是「自动」还是具体模型（供工具条 select 旁的状态提示用）。 */
  const isAutoModel = recordModel === 'auto';
  /** 时间窗口的人类可读形式（供 tooltip 与提示条）。 */
  const winText = [
    (timeStart || '').trim() || t('cfmon.timeAll'),
    '~',
    (timeEnd || '').trim() || t('cfmon.timeAll'),
  ].join(' ');
  const hasWindow = Boolean((timeStart || '').trim() || (timeEnd || '').trim());
  // 起止写反的判定（后端会自动交换，这里给个提示，别让用户以为是数据丢了）
  const swapped = Boolean((timeStart || '').trim() && (timeEnd || '').trim()
    && timeStart > timeEnd);

  return (
    <div className="panel cfmon-panel">
      <div className="panel-toolbar cfmon-toolbar">
        <select
          className="sel"
          value={server}
          onChange={(e) => { setServer(e.target.value); setData(null); setError(''); }}
          title={t('cfmon.envTitle')}
          aria-label={t('cfmon.envTitle')}
        >
          {servers === null && <option value="">{t('common.loading')}</option>}
          {servers !== null && servers.length === 0 && <option value="">{t('cfmon.noEnv')}</option>}
          {servers?.map((s) => (
            <option key={s.server_url} value={s.server_url}>
              {s.has_token ? '● ' : '○ '}{s.name}
              {s.domain && s.domain !== s.server_url ? ` · ${s.domain}` : ''}
              {s.last_error ? ` ⚠ ${s.last_error.slice(0, 30)}` : ''}
            </option>
          ))}
        </select>

        <div className="cfmon-seg" role="group" aria-label={t('cfmon.scaleLabel.normal')}>
          {SCALES.map((s) => (
            <button
              key={s.key}
              type="button"
              className={`cfmon-seg-btn${scale === s.key ? ' active' : ''}`}
              onClick={() => setScale(s.key)}
              title={t(`cfmon.scale.${s.key}`)}
            >
              {t(`cfmon.scaleLabel.${s.key}`)}
            </button>
          ))}
        </div>

        {/* 日志模型：默认「自动探测」。选具体模型后不降级 —— 用户明确指定了，
            猜另一个模型只会把「该租户没有这个模型」的真实错误藏起来。 */}
        <select
          className="sel cfmon-model"
          value={recordModel}
          onChange={(e) => { setRecordModel(e.target.value); setData(null); }}
          title={recordModel === 'auto' ? t('cfmon.modelAutoHint')
            : t('cfmon.modelTypeFieldHint', { field: typeField })}
          aria-label={t('cfmon.modelLabel')}
        >
          <option value="auto">{t('cfmon.modelAuto')}</option>
          {models.map((m) => (
            <option key={m.name} value={m.name}>{m.name}</option>
          ))}
        </select>

        {/* 时间窗口：起止都是 datetime-local（本地时区，与「云函数日志」面板一致）。
            都空 = 不限。窗口在**取回之后**过滤 —— 见 api/cf/cf_monitor.py 模块 docstring
            里 SyncOuterRecord.parse_filter_dict 会把 update_time 二次包裹成 {gte:{...}} 的坑。 */}
        <div className="cfmon-time" role="group" aria-label={t('cfmon.timeLabel')}>
          <span className="cfmon-time-label">{t('cfmon.timeFrom')}</span>
          <input
            className="cfmon-time-input"
            type="datetime-local"
            value={timeStart}
            onChange={(e) => { setTimeStart(e.target.value); setData(null); }}
            title={t('cfmon.timeHint')}
            aria-label={t('cfmon.timeFrom')}
          />
          <span className="cfmon-time-label">{t('cfmon.timeTo')}</span>
          <input
            className="cfmon-time-input"
            type="datetime-local"
            value={timeEnd}
            onChange={(e) => { setTimeEnd(e.target.value); setData(null); }}
            title={t('cfmon.timeHint')}
            aria-label={t('cfmon.timeTo')}
          />
          {hasWindow && (
            <button
              type="button"
              className="btn btn-ghost btn-sm cfmon-time-clear"
              onClick={() => { setTimeStart(''); setTimeEnd(''); setData(null); }}
              title={t('cfmon.timeAll')}
            >
              ✕
            </button>
          )}
        </div>

        <button className="btn btn-primary btn-sm" onClick={() => run()} disabled={busy || !server}>
          {busy ? t('cfmon.fetching') : t('cfmon.fetch')}
        </button>

        <label className="cfmon-toggle">
          <input
            type="checkbox"
            checked={onlyProblems}
            onChange={(e) => setOnlyProblems(e.target.checked)}
          />
          {t('cfmon.onlyProblems')}
        </label>

        <select
          className="sel"
          value={refreshSec}
          onChange={(e) => setRefreshSec(Number(e.target.value))}
          title={t('cfmon.autoRefresh')}
          aria-label={t('cfmon.autoRefresh')}
        >
          {REFRESH_OPTS.map((n) => (
            <option key={n} value={n}>
              {n === 0 ? t('cfmon.refreshOff') : t('cfmon.refreshEvery', { n })}
            </option>
          ))}
        </select>

        <span className="spacer" />
        {data?.at && <span className="cfmon-at">{t('cfmon.at', { t: data.at })}</span>}
      </div>

      {/* 环境未登录提示（可执行建议，不是干瘪报错） */}
      {curServer && !curServer.has_token && !data && (
        <div className="cfmon-note">
          <TabIcon name="alert" size={14} />
          <span>{t('cfmon.noToken')}</span>
          {curServer.last_error && <code className="cfmon-note-detail">{curServer.last_error}</code>}
        </div>
      )}

      {/* 起止写反：后端会自动交换，但要让用户知道（否则会以为数据被吞了） */}
      {swapped && (
        <div className="cfmon-note" role="status">
          <TabIcon name="alert" size={14} />
          <span>{t('cfmon.timeRangeInvalid')}</span>
        </div>
      )}

      {/* 采样没覆盖到窗口起点：这是「安静给出 0 个错误」的主要来源，必须显式提示 */}
      {data?.ok && tr?.truncated && (
        <div className="cfmon-note" role="status">
          <TabIcon name="alert" size={14} />
          <span>
            <b>{t('cfmon.truncatedTitle')}</b>
            {' — '}
            {t('cfmon.truncatedHint', {
              sampled: tr.sampled_from || '-', start: tr.start || '-',
            })}
          </span>
        </div>
      )}

      {/* 时间窗口命中情况：让「命中 N / 采样 M」可见，用户才知道窗口确实生效了 */}
      {data?.ok && hasWindow && tr && (tr.out_of_window > 0) && (
        <div className="cfmon-note" role="status">
          <TabIcon name="chart" size={14} />
          <span>
            {t('cfmon.timeRangeApplied', {
              range: winText, kept: data.rows_fetched ?? 0, queried: tr.out_of_window + (data.rows_fetched ?? 0),
            })}
            {' · '}
            {t('cfmon.outOfWindowHint', { dropped: tr.out_of_window, queried: tr.out_of_window + (data.rows_fetched ?? 0) })}
          </span>
        </div>
      )}

      {/* 错误条：只在已有数据时作为「本轮刷新失败」的提示条；
          还没数据时由下方空态承担说明职责，避免同一句话出现两次 */}
      {error && data?.ok && (
        <div className="cfmon-error" role="alert">
          <TabIcon name="alert" size={14} />
          <span>{error}</span>
        </div>
      )}

      {/* 总览条：只有拿到数据才显示，避免空态同时出现两套信息 */}
      {data?.ok && sum && (
        <div className="cfmon-summary">
          <div className="cfmon-kpi">
            <span className="cfmon-kpi-num">{sum.functions}</span>
            <span className="cfmon-kpi-label">{t('cfmon.kpi.functions')}</span>
          </div>
          <div className={`cfmon-kpi cfmon-kpi--error${sum.error_functions ? ' is-bad' : ''}`}>
            <span className="cfmon-kpi-num">{sum.error_functions}</span>
            <span className="cfmon-kpi-label">{t('cfmon.kpi.errorFuncs')}</span>
          </div>
          <div className="cfmon-kpi">
            <span className="cfmon-kpi-num">{sum.errors}</span>
            <span className="cfmon-kpi-label">{t('cfmon.kpi.errors')}</span>
          </div>
          <div className="cfmon-kpi">
            <span className="cfmon-kpi-num">{sum.ok_functions}</span>
            <span className="cfmon-kpi-label">{t('cfmon.kpi.ok')}</span>
          </div>
          <span className="spacer" />
          <span className="cfmon-meta">
            {t('cfmon.meta', {
              rows: data.rows_fetched ?? 0,
              total: data.platform_total ?? 0,
              model: data.record_model || '-',
            })}
            {/* 自动探测时把实际解析出的模型显式说出来 —— 否则用户不知道自己
                看的到底是 dynamic_log 还是 SyncOuterRecord，无从判断口径 */}
            {isAutoModel && data.record_model && (
              <span className="cfmon-meta-model"> ({data.record_model})</span>
            )}
            {hasWindow && <> · {t('cfmon.timeWinShort', { range: winText })}</>}
          </span>
        </div>
      )}

      <div className="panel-body cfmon-body">
        {/* 空态分三种：无环境 / 未登录 / 已抓但无数据 */}
        {/* 环境列表加载失败（多半是后端版本旧 / 请求失败），与「确实没有环境」分开 */}

        {serversError && (
          <EmptyState
            icon={<TabIcon name="alert" size={28} />}
            title={t('cfmon.emptyLoadFailTitle')}
            hint={
              serversStatus === 404 ||
              /\b404\b|Not Found/i.test(serversError)
                ? t('cfmon.emptyLoadFailOldBackend')
                : t('cfmon.emptyLoadFailHint')
            }
            actions={
              <button className="btn btn-sm" onClick={() => { loadServers(); }}>
                {t('cfmon.reloadServers')}
              </button>
            }
          />
        )}

        {servers !== null && servers.length === 0 && !serversError && (
          <EmptyState
            icon={<TabIcon name="cf" size={28} />}
            title={t('cfmon.emptyNoEnvTitle')}
            hint={t('cfmon.emptyNoEnvHint')}
          />
        )}

        {!data && !error && servers && servers.length > 0 && (
          <EmptyState
            icon={<TabIcon name="chart" size={28} />}
            title={t('cfmon.emptyIdleTitle')}
            hint={t('cfmon.emptyIdleHint')}
            actions={
              <button className="btn btn-primary btn-sm" onClick={() => run()} disabled={busy || !server}>
                {busy ? t('cfmon.fetching') : t('cfmon.fetch')}
              </button>
            }
          />
        )}

        {data?.ok && shown.length === 0 && (
          <EmptyState
            icon={<TabIcon name="check" size={28} />}
            title={onlyProblems ? t('cfmon.emptyAllGood') : t('cfmon.emptyNoRowsTitle')}
            hint={onlyProblems ? t('cfmon.emptyAllGoodHint') : t('cfmon.emptyNoRowsHint')}
          />
        )}

        {/* 取数失败且没有任何数据：给可执行建议，而不是留一屏空白 */}
        {error && !serversError && servers && servers.length > 0 && !data?.ok && (
          <EmptyState
            icon={<TabIcon name="alert" size={28} />}
            title={t('cfmon.emptyFailTitle')}
            hint={t('cfmon.emptyFailHint')}
            actions={
              <button className="btn btn-primary btn-sm" onClick={() => run()} disabled={busy}>
                {busy ? t('cfmon.fetching') : t('cfmon.fetch')}
              </button>
            }
          />
        )}

        {shown.length > 0 && (
          <table className="cfmon-table">
            <thead>
              <tr>
                <th className="cfmon-col-name">{t('cfmon.col.function')}</th>
                <th className="cfmon-col-num">{t('cfmon.col.total')}</th>
                <th className="cfmon-col-num">{t('cfmon.col.error')}</th>
                <th className="cfmon-col-rate">{t('cfmon.col.rate')}</th>
                <th className="cfmon-col-health">{t('cfmon.col.health')}</th>
                <th className="cfmon-col-time">{t('cfmon.col.last')}</th>
                <th className="cfmon-col-msg">{t('cfmon.col.message')}</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((f) => {
                const open = expanded === f.name;
                const hasDetail = f.error > 0;
                // 错误样本：优先用后端去重后的列表；老后端只给 last_error 时兜底成一条
                const samples = f.error_samples?.length
                  ? f.error_samples
                  : (f.last_error || f.last_error_full)
                    ? [{ time: f.last_error_time, short: f.last_error, full: f.last_error_full || f.last_error }]
                    : [];
                return (
                  <Fragment key={f.name}>
                    <tr
                      className={`cfmon-row cfmon-row--${f.health.toLowerCase()}${open ? ' is-open' : ''}${hasDetail ? ' is-clickable' : ''}`}
                      onClick={() => {
                        if (!hasDetail) return;
                        setExpanded(open ? '' : f.name);
                        setOpenSample(-1);
                      }}
                      title={hasDetail ? t('cfmon.clickDetail') : undefined}
                    >
                      <td className="cfmon-col-name">
                        {hasDetail && (
                          <span className={`cfmon-caret${open ? ' is-open' : ''}`} aria-hidden="true">▸</span>
                        )}
                        <span className="cfmon-fn">{f.name}</span>
                      </td>
                      <td className="cfmon-col-num">{f.total}</td>
                      <td className="cfmon-col-num cfmon-err-cell">{f.error || <span className="cfmon-zero">0</span>}</td>
                      <td className="cfmon-col-rate">{pct(f.error_ratio)}</td>
                      <td className="cfmon-col-health">
                        <span className={`cfmon-health cfmon-health--${f.health.toLowerCase()}`}>
                          {t(`cfmon.health.${f.health}`)}
                        </span>
                      </td>
                      <td className="cfmon-col-time">{f.last_time || '-'}</td>
                      <td className="cfmon-col-msg" title={f.last_message}>{f.last_message || '-'}</td>
                    </tr>
                    {open && (
                      <tr className="cfmon-detail-row">
                        <td colSpan={7}>
                          <div className="cfmon-detail">
                            <div className="cfmon-detail-item">
                              <span className="cfmon-detail-label">{t('cfmon.detail.codes')}</span>
                              <span className="cfmon-detail-value">
                                {f.top_errcodes.length === 0 ? '-' : f.top_errcodes.map((c) => (
                                  <span key={c.code} className="cfmon-chip cfmon-chip--err">
                                    {c.code}<b>×{c.count}</b>
                                  </span>
                                ))}
                              </span>
                            </div>
                            <div className="cfmon-detail-item">
                              <span className="cfmon-detail-label">{t('cfmon.detail.stages')}</span>
                              <span className="cfmon-detail-value">
                                {f.top_stages.length === 0 ? '-' : f.top_stages.map((s) => (
                                  <span key={s.stage} className="cfmon-chip">
                                    {s.stage}<b>×{s.count}</b>
                                  </span>
                                ))}
                              </span>
                            </div>
                            <div className="cfmon-detail-item cfmon-detail-item--full">
                              <span className="cfmon-detail-label">
                                {t('cfmon.detail.samples', { n: (samples.length || 1) })}
                              </span>
                              <div className="cfmon-samples">
                                {samples.length === 0 && (
                                  <pre className="cfmon-detail-error">{(f.last_error_full || f.last_error || '-').trim()}</pre>
                                )}
                                {samples.map((smp, i) => (
                                  <div key={`${smp.time}-${i}`} className="cfmon-sample">
                                    <button
                                      type="button"
                                      className={`cfmon-sample-head${openSample === i ? ' is-open' : ''}`}
                                      onClick={() => setOpenSample(openSample === i ? -1 : i)}
                                      aria-expanded={openSample === i}
                                    >
                                      <span className="cfmon-caret" aria-hidden="true">▸</span>
                                      <span className="cfmon-sample-time">{smp.time || '-'}</span>
                                      <span className="cfmon-sample-short">{smp.short || '-'}</span>
                                    </button>
                                    {openSample === i && (
                                      <pre className="cfmon-detail-error">{(smp.full || smp.short || '-').trim()}</pre>
                                    )}
                                  </div>
                                ))}
                              </div>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
