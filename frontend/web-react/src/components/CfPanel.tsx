import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { apiGet, apiPost } from '../api/client';
import { sse } from '../api/events';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { readClipboardText, writeClipboardText } from '../utils/clipboard';
import { openBuiltinBrowser, hcmCookiesForTarget } from '../utils/browser';
import { logRowType, logRowTime, buildHcmLogUrl } from '../utils/logFields';
import type { CfAccount, CfLogsRow, SSECFLogUpdate } from '../api/types';

const CF_CFG_KEY = 'jgg-cf-cfg';

/** 时间范围预设：'all' = 不过滤，其余为相对当前时间，'custom' = 手动起止。 */
type TimePreset = 'all' | '1h' | '6h' | '24h' | '7d' | 'custom';

interface CfCfg {
  server_url: string;
  username: string;
  password: string;
  token: string;
  proxy: string;
  log_type: string;
  record_model: string;
  page_size: number;
  page_index: number;
  time_preset: TimePreset;
  time_start: string; // datetime-local 值（本地时间，形如 2026-09-15T18:00）
  time_end: string;
}

interface CfLastResult {
  server_url: string;
  log_type: string;
  record_model: string;
  auth_method: string;
  page_index: number;
  page_size: number;
  total: number;
  rows: CfLogsRow[];
  raw: any;
  localPage: number;
}

// 时间范围预设（下拉）。labelKey 指向 i18n 文案。
const TIME_PRESETS: { key: TimePreset; labelKey: string }[] = [
  { key: 'all', labelKey: 'cf.timeAll' },
  { key: '1h', labelKey: 'cf.time1h' },
  { key: '6h', labelKey: 'cf.time6h' },
  { key: '24h', labelKey: 'cf.time24h' },
  { key: '7d', labelKey: 'cf.time7d' },
  { key: 'custom', labelKey: 'cf.timeCustom' },
];

const HOUR_MS = 3600_000;
const DAY_MS = 24 * HOUR_MS;

/** 把日志时间字符串解析为毫秒时间戳；无法解析返回 NaN。兼容 "2026-09-15 10:00:00"、ISO、unix 秒/毫秒。 */
function parseLogTime(s: string): number {
  const raw = (s || '').trim();
  if (!raw) return NaN;
  if (/^\d+$/.test(raw)) {
    const n = Number(raw);
    if (n >= 1e12) return n; // 毫秒
    if (n >= 1e9) return n * 1000; // 秒
    return NaN;
  }
  let t = new Date(raw.replace(' ', 'T')).getTime();
  if (isNaN(t)) t = new Date(raw).getTime();
  return t;
}

/** 相对预设 → [startMs, endMs]；'all'/'custom' 返回 null。 */
function presetWindowMs(preset: TimePreset): [number, number] | null {
  const now = Date.now();
  switch (preset) {
    case '1h': return [now - HOUR_MS, now];
    case '6h': return [now - 6 * HOUR_MS, now];
    case '24h': return [now - DAY_MS, now];
    case '7d': return [now - 7 * DAY_MS, now];
    default: return null;
  }
}

/** 当前生效的时间窗口 [startMs, endMs]；preset='all' 返回 null（不过滤）。 */
function effectiveWindowMs(preset: TimePreset, startS: string, endS: string): [number, number] | null {
  if (preset === 'all') return null;
  if (preset === 'custom') {
    const s = startS ? new Date(startS).getTime() : NaN;
    const e = endS ? new Date(endS).getTime() : NaN;
    return [isNaN(s) ? -Infinity : s, isNaN(e) ? Infinity : e];
  }
  return presetWindowMs(preset);
}

/** 毫秒时间戳 → datetime-local 值（本地时区）。 */
function toLocalInput(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 时间字段兜底统一走 utils/logFields（dynamic_log 的 create_* / SyncOuterRecord 的 update_*）。 */
function cfTime(row: CfLogsRow): string {
  return logRowTime(row);
}
function cfContent(row: CfLogsRow): string {
  const c = row.content ?? row.message ?? row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c) : String(c);
}
function cfContentFull(row: CfLogsRow): string {
  const c = row.content ?? row.message ?? row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c, null, 2) : String(c);
}
/**
 * 类型字段兜底统一走 utils/logFields：
 * 记录自身字段（log_type / name / title …）→ 查询时填的 log_type 过滤值 → '(未知)'。
 */
function cfLogType(row: CfLogsRow, fallback: string): string {
  return logRowType(row, fallback);
}

/** 实时刷新的行去重 key：id 优先，缺 id 用「时间+内容」哈希兜底（与后端 _row_key 同口径）。 */
function cfRowKey(row: CfLogsRow): string {
  const rid = row.id ?? row._id;
  if (rid != null) return 'id:' + String(rid);
  const basis = JSON.stringify([row.create_time, row.update_time, row.content, row.log_type, row.name]);
  let h = 0;
  for (let i = 0; i < basis.length; i++) h = (h * 31 + basis.charCodeAt(i)) | 0;
  return 'h:' + String(h);
}

async function openCloudFunctionLogs(serverUrl: string, logType: string, recordModel = 'dynamic_log', token = ''): Promise<void> {
  try {
    const base = (serverUrl || '').trim();
    if (!base) return;
    // 打开 HCM 云函数日志界面（#/common_model_list），按记录模型 + 类型字段过滤
    const target = buildHcmLogUrl(base, recordModel || 'dynamic_log',
      (logType && logType !== '(未知)') ? logType : undefined);
    // 注入与目标网关绑定的 token（同网关才生效）：本账号 token → 后端现刷新 → 全局兜底
    const cookies = await hcmCookiesForTarget(target, { token, server: serverUrl });
    // 优先在内置浏览器打开（应用内嵌窗口，可注入 HCM token cookie 自动登录），失败再回退系统浏览器
    if (await openBuiltinBrowser(target, cookies)) return;
    const external = (window as any).electronAPI?.openExternal;
    if (typeof external === 'function') {
      await external(target);
    } else {
      window.open(target, '_blank', 'noopener,noreferrer');
    }
  } catch {
    return;
  }
}

// —— 全局搜索高亮：定位匹配区间 + 渲染 <mark> ——
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

function highlightNodes(
  text: string,
  matches: Array<[number, number]>,
  ordStart: number,
  activeMatch: number
): ReactNode {
  if (!matches.length) return text;
  const nodes: ReactNode[] = [];
  let last = 0;
  matches.forEach(([s, e], i) => {
    if (s > last) nodes.push(text.slice(last, s));
    const ord = ordStart + i;
    const cls = ord === activeMatch ? 'cf-hl active' : 'cf-hl';
    nodes.push(
      <mark key={i} className={cls}>
        {text.slice(s, e)}
      </mark>
    );
    last = e;
  });
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function CfPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const { t } = useT();

  const [accounts, setAccounts] = useState<CfAccount[]>([]);
  const [env, setEnv] = useState('');
  const [cfg, setCfg] = useState<CfCfg>({
    server_url: '',
    username: '',
    password: '',
    token: '',
    proxy: '',
    log_type: '',
    record_model: 'dynamic_log',
    page_size: 200,
    page_index: 1,
    time_preset: 'all',
    time_start: '',
    time_end: '',
  });
  const [captcha, setCaptcha] = useState<{ captcha_id: string; image_code_index: string; image: string }>({
    captcha_id: '',
    image_code_index: '',
    image: '',
  });
  const [imageCode, setImageCode] = useState('');
  const [result, setResult] = useState<CfLastResult | null>(null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [search, setSearch] = useState('');
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [filterOn, setFilterOn] = useState(true);
  const [activeMatch, setActiveMatch] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [status, setStatus] = useState<{ text: string; cls: string }>({ text: '', cls: '' });
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [cfgOpen, setCfgOpen] = useState(false);
  const [tokenMap, setTokenMap] = useState<Record<string, any>>({});
  const [autoLogin, setAutoLogin] = useState<{ running: boolean; msg: string }>({ running: false, msg: '' });
  const [streaming, setStreaming] = useState(false);        // 实时刷新是否开启
  const [streamInterval, setStreamInterval] = useState(5);  // 轮询间隔（秒）

  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;
  const resultRef = useRef(result);
  resultRef.current = result;
  const streamingRef = useRef(false);
  streamingRef.current = streaming;

  const setBusy = (k: string, v: boolean) =>
    setLoading((m) => ({ ...m, [k]: v }));

  const saveCfg = useCallback((override?: Partial<CfCfg>) => {
    try {
      localStorage.setItem(CF_CFG_KEY, JSON.stringify({ ...cfgRef.current, ...(override || {}) }));
    } catch {
      /* ignore */
    }
  }, []);

  const loadCfg = useCallback(() => {
    try {
      const raw = localStorage.getItem(CF_CFG_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw) as Partial<CfCfg>;
      setCfg((c) => {
        const next = { ...c, ...parsed };
        // 相对预设（最近 X）每次重算起止时间，避免沿用上次持久化的过期绝对时间
        const p = next.time_preset;
        if (p && p !== 'all' && p !== 'custom') {
          const w = presetWindowMs(p);
          if (w) { next.time_start = toLocalInput(w[0]); next.time_end = toLocalInput(w[1]); }
        }
        return next;
      });
    } catch {
      /* ignore */
    }
  }, []);

  const loadAccounts = useCallback(async () => {
    try {
      const d = await apiGet<{ accounts?: CfAccount[] }>('/api/cf/accounts');
      const list = Array.isArray(d.accounts) ? d.accounts : [];
      setAccounts(list);
      if (list.length === 0) {
        pushLog('[CF] 本地 cf_accounts.local.json 未读到任何账号，下拉框只有「自定义」可选', 'warning');
      } else {
        pushLog(`[CF] 已从本地配置加载 ${list.length} 个账号：${list.map((a) => a.name).join('、')}`);
      }
    } catch {
      setAccounts([]);
    }
  }, [pushLog]);

  const loadTokens = useCallback(async (): Promise<Record<string, any>> => {
    try {
      const d = await apiGet<{ tokens?: Array<{
        server_url: string; has_token: boolean; token_masked: string;
        need_captcha: boolean; last_error: string; name: string;
        ts: string; stale: boolean;
      }> }>('/api/cf/tokens');
      const map: Record<string, any> = {};
      (d.tokens || []).forEach((tk) => { map[tk.server_url] = tk; });
      setTokenMap(map);
      return map;
    } catch {
      setTokenMap({});
      return {};
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadAccounts();
      loadCfg();
      await loadTokens();
    })();
  }, [loadAccounts, loadCfg, loadTokens]);

  // 实时刷新：订阅后端 SSE 推送（cf_log_update），把新增日志合并进当前结果。
  // 合并策略：行 key 去重 → 新行前插 → 上限 5000 条；时间过滤视图由 view memo 自动生效。
  useEffect(() => {
    const off = sse.on('cf_log_update', (d: SSECFLogUpdate) => {
      if (d.stopped) {
        setStreaming(false);
        setStatus({ text: d.error ? `实时刷新已停止：${d.error}` : '实时刷新已停止', cls: d.error ? 'error' : '' });
        return;
      }
      if (!streamingRef.current) return;
      const r = resultRef.current;
      if (!r) return;
      const incoming = Array.isArray(d.rows) ? d.rows : [];
      if (!incoming.length) {
        if (d.error) setStatus({ text: `实时刷新拉取失败：${d.error}（将继续重试）`, cls: 'warning' });
        return;
      }
      const seen = new Set(r.rows.map(cfRowKey));
      const fresh = incoming.filter((row) => !seen.has(cfRowKey(row)));
      if (!fresh.length) return; // 无新日志，不打扰
      setResult({
        ...r,
        rows: [...fresh, ...r.rows].slice(0, 5000),
        total: d.total ?? r.total,
        localPage: 1,
      });
      const latest = d.latest_time || cfTime(fresh[0]);
      setStatus({ text: `实时刷新：新增 ${fresh.length} 条（最新 ${latest}）`, cls: 'success' });
    });
    return () => off();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchEnv = (key: string) => {
    setEnv(key);
    // 切环境时停掉实时刷新：流绑定的是旧 server_url，继续跑只会拉旧环境日志
    if (streamingRef.current) void stopStream();
    // 清空上一个环境的登录态残留（token/验证码），避免用旧环境的 token 去查询新环境
    const clearLoginState = () => {
      setImageCode('');
      setCaptcha({ captcha_id: '', image_code_index: '', image: '' });
    };
    if (!key || key === 'custom') {
      setCfg((c) => ({ ...c, token: '' }));
      clearLoginState();
      saveCfg({ token: '' });
      return;
    }
    const acc = accounts.find((a) => a.name === key);
    if (!acc) return;
    setCfg((c) => ({
      ...c,
      server_url: acc.server_url || '',
      username: acc.username || '',
      password: acc.password || '',
      token: '', // 切换环境清空 token，查询时由后端按新 server_url 复用自动获取的缓存 cookie
    }));
    clearLoginState();
    saveCfg({
      server_url: acc.server_url || '',
      username: acc.username || '',
      password: acc.password || '',
      token: '',
    });
    addToast(`已切换到「${acc.name}」环境，账号密码已预填`, 'info');
  };

  const fetchCaptcha = async () => {
    const serverUrl = cfg.server_url.trim();
    const proxy = cfg.proxy.trim();
    if (!serverUrl) {
      addToast(t('cf.serverUrlFirst'), 'warn');
      return;
    }
    setBusy('captcha', true);
    try {
      const res = await apiPost<{ captcha_id?: string; image_code_index?: string; image?: string }>(
        '/api/cf/captcha',
        { server_url: serverUrl, proxy }
      );
      setCaptcha({
        captcha_id: res.captcha_id || '',
        image_code_index: res.image_code_index || '',
        image: res.image || '',
      });
      setImageCode('');
      setStatus({ text: '', cls: '' });
    } catch (e: any) {
      setCaptcha({ captcha_id: '', image_code_index: '', image: '' });
      setStatus({ text: `获取验证码失败：${e.message}`, cls: 'error' });
    } finally {
      setBusy('captcha', false);
    }
  };

  const login = async () => {
    const serverUrl = cfg.server_url.trim();
    const mobile = cfg.username.trim();
    const password = cfg.password.trim();
    const proxy = cfg.proxy.trim();
    if (!serverUrl || !mobile || !password) {
      setStatus({ text: t('cf.fillServerMobilePwd'), cls: 'error' });
      return;
    }
    if (imageCode && !captcha.captcha_id) {
      setStatus({ text: t('cf.refreshCaptchaFirst'), cls: 'error' });
      return;
    }
    setBusy('login', true);
    setStatus({ text: t('cf.loggingIn'), cls: '' });
    try {
      const res = await apiPost<{
        token?: string;
        ok?: boolean;
        message?: string;
        need_img_valid?: boolean;
      }>('/api/cf/login', {
        server_url: serverUrl,
        mobile,
        password,
        proxy,
        image_code: imageCode,
        image_code_index: captcha.image_code_index,
        captcha_id: captcha.captcha_id,
      });
      if (res.token) {
        setCfg((c) => ({ ...c, token: res.token || '' }));
        setCaptcha({ captcha_id: '', image_code_index: '', image: '' });
        setImageCode('');
        saveCfg({ token: res.token || '' });
        setStatus({ text: t('cf.loginSuccess'), cls: 'success' });
        return;
      }
      if (res && res.ok === false) {
        if (res.need_img_valid) {
          setStatus({ text: `${res.message || '登录失败'}（需要图片验证码，请输入后重新登录）`, cls: 'error' });
          try { await fetchCaptcha(); } catch { /* ignore */ }
        } else {
          setStatus({ text: `登录失败：${res.message || ''}`, cls: 'error' });
        }
        return;
      }
      throw new Error('未获取到 token');
    } catch (ex: any) {
      setStatus({ text: `登录失败：${ex.message}`, cls: 'error' });
      if (captcha.captcha_id) {
        try { await fetchCaptcha(); } catch { /* ignore */ }
      }
    } finally {
      setBusy('login', false);
    }
  };

  const autoGetTokens = async () => {
    setAutoLogin({ running: true, msg: t('cf.autoGetting') });
    try {
      const res = await apiPost<{ total?: number; success?: number; results?: any[] }>(
        '/api/cf/auto-login',
        { proxy: cfg.proxy.trim() }
      );
      const map = await loadTokens();
      const su = cfg.server_url.trim();
      if (su && map[su]?.has_token) {
        addToast(`已自动获取「${map[su].name}」的 Token`, 'success');
      }
      setStatus({
        text: `自动获取完成：${res.success ?? 0}/${res.total ?? 0} 个账号成功（需验证码的请手动登录）`,
        cls: (res.success ?? 0) > 0 ? 'success' : 'warning',
      });
    } catch (e: any) {
      setStatus({ text: `自动获取失败：${e.message}`, cls: 'error' });
    } finally {
      setAutoLogin({ running: false, msg: '' });
    }
  };

  // windowStartMs：时间过滤生效时的窗口起始（ms）。页 1 最新，一旦翻到比窗口更旧的页即可停，
  // 避免为「最近 1 小时」这类过滤把历史全量都拉下来。
  const ensureAllLogs = useCallback(async (base: CfLastResult, windowStartMs = -Infinity) => {
    const total = base.total || 0;
    if (total === 0 || base.rows.length >= total) return;
    const token = cfgRef.current.token;
    const proxy = cfgRef.current.proxy.trim();
    const fetchSize = Math.max(base.page_size || 200, 1000);
    let nextPage = Math.floor(base.rows.length / fetchSize) + 1;
    if (nextPage < 2) nextPage = 2;
    try {
      while (base.rows.length < total && nextPage <= 500) {
        setStatus({ text: `正在加载全部日志用于排序…（${base.rows.length}/${total}）`, cls: '' });
        const res = await apiPost<any>('/api/cf/logs', {
          server_url: base.server_url,
          token,
          log_type: base.log_type,
          record_model: base.record_model,
          page_index: nextPage,
          page_size: fetchSize,
          proxy,
        });
        const payload = res.data || res.result || res;
        const pageRows =
          payload.list || payload.data || payload.items || res.list || res.data || [];
        if (!pageRows.length) break;
        base.rows = base.rows.concat(pageRows);
        setResult({ ...base });
        // 时间过滤生效时：本页最后（最旧）一行若已早于窗口起始，后续页只会更旧 → 停。
        if (isFinite(windowStartMs)) {
          const oldest = parseLogTime(cfTime(base.rows[base.rows.length - 1]));
          if (!isNaN(oldest) && oldest < windowStartMs) break;
        }
        nextPage += 1;
      }
    } catch (e: any) {
      setStatus({ text: `加载全部日志失败：${e.message}（已对当前已加载 ${base.rows.length} 条排序）`, cls: 'error' });
    }
  }, []);

  // —— 时间范围过滤：预设下拉 / 手动起止（手动改动即切到「自定义」）——
  const onPresetChange = (p: TimePreset) => {
    let start = cfg.time_start;
    let end = cfg.time_end;
    if (p !== 'all' && p !== 'custom') {
      const w = presetWindowMs(p);
      if (w) { start = toLocalInput(w[0]); end = toLocalInput(w[1]); }
    } else if (p === 'custom' && !start && !end) {
      const w = presetWindowMs('1h');
      if (w) { start = toLocalInput(w[0]); end = toLocalInput(w[1]); }
    }
    const patch = { time_preset: p, time_start: start, time_end: end };
    setCfg((c) => ({ ...c, ...patch }));
    saveCfg(patch);
    if (resultRef.current) setResult({ ...resultRef.current, localPage: 1 });
  };

  const onTimeInput = (which: 'start' | 'end', v: string) => {
    const patch = which === 'start'
      ? { time_start: v, time_preset: 'custom' as TimePreset }
      : { time_end: v, time_preset: 'custom' as TimePreset };
    setCfg((c) => ({ ...c, ...patch }));
    saveCfg(patch);
    if (resultRef.current) setResult({ ...resultRef.current, localPage: 1 });
  };

  const queryLogs = async () => {
    const serverUrl = cfg.server_url.trim();
    const token = cfg.token.trim();
    const logType = cfg.log_type.trim();
    const recordModel = cfg.record_model.trim() || 'dynamic_log';
    const pageSize = cfg.page_size || 200;
    const pageIndex = cfg.page_index || 1;
    const proxy = cfg.proxy.trim();
    const cached = tokenMap[cfg.server_url.trim()];
    if (!token && !(cached && cached.has_token)) {
      setStatus({ text: t('cf.tokenFirst'), cls: 'error' });
      return;
    }
    setBusy('query', true);
    setStatus({ text: proxy ? `正在查询（代理：${proxy}）…` : '正在查询（直连）…', cls: '' });
    try {
      const res = await apiPost<any>('/api/cf/logs', {
        server_url: serverUrl,
        token,
        log_type: logType,
        record_model: recordModel,
        page_index: pageIndex,
        page_size: pageSize,
        proxy,
      });
      const payload = res.data || res.result || res;
      const rows =
        payload.list || payload.data || payload.items || res.list || res.data || [];
      const total =
        payload.total ?? payload.count ?? payload.row_count ?? res.total ?? rows.length;

      const base: CfLastResult = {
        server_url: serverUrl,
        log_type: logType,
        record_model: recordModel,
        auth_method: res.method || '',
        page_index: pageIndex,
        page_size: pageSize,
        total,
        rows,
        raw: res,
        localPage: 1,
      };
      setResult(base);
      setExpanded(null);
      setSearch('');
      setStatus({ text: `查询成功，共 ${total} 条`, cls: 'success' });
      const win = effectiveWindowMs(cfg.time_preset, cfg.time_start, cfg.time_end);
      try {
        await ensureAllLogs(base, win ? win[0] : -Infinity);
      } catch {
        /* 拉取失败时降级：对已加载的数据排序并提示 */
      }
    } catch (ex: any) {
      const msg = ex?.message || String(ex);
      // P2-⑧：手填 Token 失败时给出明确引导。识别会话失效 / 格式异常类错误，
      // 引导用户改用「自动获取」缓存 cookie 或重新登录，而非只报红。
      if (cfg.token.trim()) {
        const sessionLike = /token 可能已失效|未登录|登录过期|未授权|unauthorized|格式异常|疑似 HTML|验证码片段/i.test(msg);
        if (sessionLike) {
          setStatus({ text: `查询失败：${msg}\n${t('cf.tokenHandFillGuide')}`, cls: 'error' });
          setResult(null);
          return;
        }
      }
      setStatus({ text: `查询失败：${msg}`, cls: 'error' });
      setResult(null);
    } finally {
      setBusy('query', false);
    }
  };

  // —— 实时刷新：后端按间隔轮询最新页，新日志经 SSE（cf_log_update）推送合并 ——
  const stopStream = useCallback(async () => {
    try {
      await apiPost('/api/cf/logs/stream', { action: 'stop' });
    } catch {
      /* ignore */
    }
    setStreaming(false);
  }, []);

  const toggleStream = async () => {
    if (streamingRef.current) {
      setBusy('stream', true);
      await stopStream();
      setStatus({ text: '已停止实时刷新', cls: '' });
      setBusy('stream', false);
      return;
    }
    const serverUrl = cfg.server_url.trim();
    if (!serverUrl) {
      setStatus({ text: t('cf.serverUrlFirst'), cls: 'error' });
      return;
    }
    const cached = tokenMap[serverUrl];
    if (!cfg.token.trim() && !(cached && cached.has_token)) {
      setStatus({ text: t('cf.tokenFirst'), cls: 'error' });
      return;
    }
    setBusy('stream', true);
    try {
      // 先查一次基线：流首轮只做静默种子，之后只推送新增日志
      await queryLogs();
      await apiPost('/api/cf/logs/stream', {
        action: 'start',
        server_url: serverUrl,
        token: cfg.token.trim(),
        proxy: cfg.proxy.trim(),
        log_type: cfg.log_type.trim(),
        record_model: cfg.record_model.trim() || 'dynamic_log',
        page_size: Math.min(cfg.page_size || 100, 300),
        interval: streamInterval,
      });
      setStreaming(true);
      setStatus({ text: `实时刷新已开启（每 ${streamInterval} 秒检查新日志）`, cls: 'success' });
    } catch (e: any) {
      setStatus({ text: `开启实时刷新失败：${e?.message || e}`, cls: 'error' });
    } finally {
      setBusy('stream', false);
    }
  };

  // 导出「当前过滤视图」（时间窗口 + 关键词），导出后自动把文件路径复制到剪贴板（可粘贴给 AI）。
  const exportLogs = async () => {
    const r = resultRef.current;
    if (!r || !r.rows || !r.rows.length) {
      setStatus({ text: t('cf.exportNeedData'), cls: 'error' });
      return;
    }
    const win = effectiveWindowMs(cfg.time_preset, cfg.time_start, cfg.time_end);
    // 导出「当前过滤视图」的结果（排序 + 时间窗口 + 搜索关键字），与界面所见一致：
    // 先按当前排序方向排好所有已加载行，再依次按时间窗口、搜索关键字过滤。
    const allRows = r.rows.slice();
    allRows.sort((a, b) => {
      const ta = cfTime(a), tb = cfTime(b);
      const cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });
    const q = search.trim();
    let rowsToExport = allRows;
    if (win || q) {
      const needle = caseSensitive ? q : q.toLowerCase();
      rowsToExport = allRows.filter((row) => {
        if (win) {
          const tv = parseLogTime(cfTime(row));
          if (isNaN(tv) || tv < win[0] || tv > win[1]) return false;
        }
        if (q) {
          const content = caseSensitive ? cfContent(row) : cfContent(row).toLowerCase();
          const time = caseSensitive ? cfTime(row) : cfTime(row).toLowerCase();
          const type = caseSensitive ? cfLogType(row, r.log_type) : cfLogType(row, r.log_type).toLowerCase();
          if (!(content.includes(needle) || time.includes(needle) || type.includes(needle))) return false;
        }
        return true;
      });
    }
    // 把生效的时间窗口（本地时间字符串）带给后端，写进导出文件元数据并按它命名文件。
    const timeStart = win && isFinite(win[0]) ? toLocalInput(win[0]) : '';
    const timeEnd = win && isFinite(win[1]) ? toLocalInput(win[1]) : '';
    setBusy('export', true);
    try {
      const res = await apiPost<{ path?: string; count?: number }>('/api/cf/logs/export', {
        server_url: r.server_url,
        log_type: r.log_type,
        record_model: r.record_model,
        auth_method: r.auth_method,
        page_index: r.page_index,
        page_size: r.page_size,
        total: rowsToExport.length,
        rows: rowsToExport,
        raw: r.raw,
        keyword: q,
        filtered: !!q || !!win,
        time_start: timeStart,
        time_end: timeEnd,
      });
      if (res.path) {
        let copied = false;
        try {
          await writeClipboardText(res.path);
          copied = true;
        } catch {
          /* ignore */
        }
        setStatus({
          text: copied
            ? `已导出 ${res.count} 条，文件路径已复制到剪贴板：${res.path}`
            : `已导出 ${res.count} 条 → ${res.path}（复制到剪贴板失败，请手动复制路径）`,
          cls: 'success',
        });
        pushLog(`CF 日志导出路径: ${res.path}`);
      } else {
        throw new Error('未返回文件路径');
      }
    } catch (e: any) {
      setStatus({ text: `导出失败：${e.message}`, cls: 'error' });
    } finally {
      setBusy('export', false);
    }
  };

  const clipboardSave = async () => {
    let text = '';
    try {
      text = await readClipboardText();
    } catch (e: any) {
      setStatus({ text: `读取剪贴板失败：${e.message}（请先复制文本，并点击本窗口使其获得焦点，再重试）`, cls: 'error' });
      return;
    }
    if (!text || !text.trim()) {
      setStatus({ text: '剪贴板内容为空，请先复制一些文本再点击', cls: 'error' });
      return;
    }
    setBusy('clipboard', true);
    setStatus({ text: '正在保存剪贴板内容到文件…', cls: '' });
    try {
      const res = await apiPost<{ path?: string; size?: number }>('/api/cf/clipboard-save', { text });
      if (res.path) {
        let copied = false;
        try {
          await writeClipboardText(res.path);
          copied = true;
        } catch {
          /* ignore */
        }
        setStatus({
          text: `已保存剪贴板内容（${res.size} 字符）→ ${res.path}${copied ? '（路径已复制到剪贴板）' : ''}`,
          cls: 'success',
        });
        pushLog(`剪贴板转文件成功：${res.path}`);
      } else {
        throw new Error('未返回文件路径');
      }
    } catch (ex: any) {
      setStatus({ text: `保存失败：${ex.message}`, cls: 'error' });
    } finally {
      setBusy('clipboard', false);
    }
  };

  const toggleSort = async () => {
    const next = sortDir === 'asc' ? 'desc' : 'asc';
    setSortDir(next);
    setBusy('sort', true);
    try {
      const win = effectiveWindowMs(cfg.time_preset, cfg.time_start, cfg.time_end);
      if (resultRef.current) await ensureAllLogs(resultRef.current, win ? win[0] : -Infinity);
    } finally {
      setBusy('sort', false);
    }
  };

  // 排序 + 客户端实时过滤 + 本地分页 + 匹配计数
  const view = useMemo(() => {
    if (!result) return { rows: [] as CfLogsRow[], isFull: false, all: 0, total: 0, totalPages: 1, localPage: 1, matchTotal: 0 };
    const all = result.rows.slice();
    const isFull = (result.total || 0) > 0 && result.rows.length >= result.total;
    all.sort((a, b) => {
      const ta = cfTime(a);
      const tb = cfTime(b);
      const cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });
    const q = search.trim();
    const needle = caseSensitive ? q : q.toLowerCase();
    // 时间窗口过滤（先于关键词过滤）：把结果收窄到「最近 X」或自定义起止。
    const win = effectiveWindowMs(cfg.time_preset, cfg.time_start, cfg.time_end);
    let filtered = all;
    if (win) {
      filtered = filtered.filter((r) => {
        const tv = parseLogTime(cfTime(r));
        return !isNaN(tv) && tv >= win[0] && tv <= win[1];
      });
    }
    if (q && filterOn) {
      filtered = filtered.filter((r) => {
        const content = caseSensitive ? cfContent(r) : cfContent(r).toLowerCase();
        const time = caseSensitive ? cfTime(r) : cfTime(r).toLowerCase();
        const type = caseSensitive ? cfLogType(r, result.log_type) : cfLogType(r, result.log_type).toLowerCase();
        return content.includes(needle) || time.includes(needle) || type.includes(needle);
      });
    }
    let matchTotal = 0;
    let matchRows = 0;
    if (q) {
      const lt = result.log_type;
      for (const r of filtered) {
        const c =
          findMatches(cfContent(r), q, caseSensitive).length +
          findMatches(cfTime(r), q, caseSensitive).length +
          findMatches(cfLogType(r, lt), q, caseSensitive).length;
        if (c > 0) matchRows += 1;
        matchTotal += c;
      }
    }
    const pageSize = result.page_size || 200;
    let display = filtered;
    let localPage = result.localPage || 1;
    let totalPages = 1;
    if (isFull && filtered.length > pageSize) {
      totalPages = Math.ceil(filtered.length / pageSize);
      if (localPage > totalPages) localPage = totalPages;
      display = filtered.slice((localPage - 1) * pageSize, localPage * pageSize);
    }
    return { rows: display, isFull, all: filtered.length, total: result.total, totalPages, localPage, matchTotal, matchRows };
  }, [result, sortDir, search, caseSensitive, filterOn, cfg.time_preset, cfg.time_start, cfg.time_end]);

  const goLocalPage = (p: number) => {
    if (result) setResult({ ...result, localPage: p });
  };

  // 选中匹配：上一个 / 下一个（循环），自动翻到匹配所在本地页
  const gotoMatch = useCallback((dir: number) => {
    const total = view.matchTotal || 0;
    if (total === 0) return;
    let next = activeMatch + dir;
    if (next < 1) next = total;
    if (next > total) next = 1;
    if (result) {
      const q2 = search.trim();
      let ord = 0;
      let targetDisplay = -1;
      for (let i = 0; i < view.rows.length; i++) {
        const r = view.rows[i];
        const cnt = q2
          ? findMatches(cfContent(r), q2, caseSensitive).length +
            findMatches(cfTime(r), q2, caseSensitive).length +
            findMatches(cfLogType(r, result.log_type), q2, caseSensitive).length
          : 0;
        if (next > ord && next <= ord + cnt) {
          targetDisplay = i;
          break;
        }
        ord += cnt;
      }
      if (targetDisplay >= 0 && view.totalPages > 1) {
        const pageSize = result.page_size || 200;
        const page = Math.floor(targetDisplay / pageSize) + 1;
        if (page !== result.localPage) setResult({ ...result, localPage: page });
      }
    }
    setActiveMatch(next);
  }, [view.matchTotal, view.rows, view.totalPages, activeMatch, result, search, caseSensitive]);

  // 搜索词变化时，重置/收敛选中匹配
  useEffect(() => {
    const total = view.matchTotal || 0;
    if (!search.trim() || total === 0) {
      if (activeMatch !== 0) setActiveMatch(0);
    } else if (activeMatch < 1 || activeMatch > total) {
      setActiveMatch(1);
    }
  }, [search, view.matchTotal, activeMatch]);

  // 选中匹配变化时，滚动到对应行（若跨页已在上一步切页）
  useEffect(() => {
    if (!activeMatch || !result) return;
    const q2 = search.trim();
    if (!q2) return;
    let ord = 0;
    let target = -1;
    for (let i = 0; i < view.rows.length; i++) {
      const r = view.rows[i];
      const cnt =
        findMatches(cfContent(r), q2, caseSensitive).length +
        findMatches(cfTime(r), q2, caseSensitive).length +
        findMatches(cfLogType(r, result.log_type), q2, caseSensitive).length;
      if (activeMatch > ord && activeMatch <= ord + cnt) {
        target = i;
        break;
      }
      ord += cnt;
    }
    if (target >= 0) {
      const el = document.getElementById(`cf-row-${target}`);
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, [activeMatch, view, search, caseSensitive, result]);

  // 当前搜索词（组件作用域，供结果表渲染高亮使用）
  const q = search.trim();
  let rowOrd = 0;

  return (
    <div className="cf-panel">
      {/* ===== 配置卡片：标题 + 环境切换 + 可折叠配置体 ===== */}
      <div className="card-soft cf-cfg-card">
        <div className="panel-header">
          <h2 className="section-title">{t('cf.title')}</h2>
          <div className="cf-env-switcher">
            <select
              className="sel"
              value={env}
              onChange={(e) => switchEnv(e.target.value)}
            >
              <option value="">{t('cf.selectEnv')}</option>
              {accounts.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
              <option value="custom">{t('cf.custom')}</option>
            </select>
          </div>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setCfgOpen((v) => !v)}
            title={t('cf.toggleConfig')}
          >
            {t('cf.config')} {cfgOpen ? '▾' : '▸'}
          </button>
          <button
            className="btn btn-sm btn-primary"
            onClick={() => void openCloudFunctionLogs(cfg.server_url.trim(), cfg.log_type.trim(), cfg.record_model.trim() || 'dynamic_log', cfg.token.trim())}
            disabled={!cfg.server_url.trim()}
            title={t('cf.openLog')}
          >
            ↗ {t('cf.openLogShort')}
          </button>
        </div>

        {cfgOpen && (
          <div className="cf-cfg-body">
            <div className="cf-cfg-row">
              <div className="cf-cfg-field">
                <label>{t('cf.serverUrl')}</label>
                <input
                  className="input"
                  placeholder={t('cf.serverUrlPlaceholder')}
                  value={cfg.server_url}
                  onChange={(e) => setCfg({ ...cfg, server_url: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
            </div>
            <div className="cf-cfg-row">
              <div className="cf-cfg-field">
                <label>{t('cf.mobile')}</label>
                <input
                  className="input"
                  placeholder={t('cf.mobilePlaceholder')}
                  value={cfg.username}
                  onChange={(e) => setCfg({ ...cfg, username: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
              <div className="cf-cfg-field">
                <label>{t('cf.password')}</label>
                <input
                  className="input"
                  type="password"
                  placeholder={t('cf.passwordPlaceholder')}
                  value={cfg.password}
                  onChange={(e) => setCfg({ ...cfg, password: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
              <div className="cf-cfg-field cf-cfg-field--token">
                <label>{t('cf.token')}</label>
                <input
                  className="input"
                  placeholder={t('cf.tokenPlaceholder')}
                  value={cfg.token}
                  onChange={(e) => setCfg({ ...cfg, token: e.target.value })}
                  onBlur={() => saveCfg()}
                />
                {(() => {
                  const tk = tokenMap[cfg.server_url.trim()];
                  if (!tk) return null;
                  const ageText = (() => {
                    if (!tk.ts) return '';
                    const diffMs = Date.now() - new Date(tk.ts.replace(/-/g, '/')).getTime();
                    if (isNaN(diffMs) || diffMs < 0) return '';
                    const h = Math.floor(diffMs / 3600000);
                    if (h < 1) return `（${Math.max(1, Math.floor(diffMs / 60000))} 分钟前）`;
                    if (h < 24) return `（${h} 小时前）`;
                    return `（${Math.floor(h / 24)} 天前）`;
                  })();
                  const RetryBtn = (
                    <button
                      className="btn btn-ghost btn-xs cf-token-retry"
                      onClick={autoGetTokens}
                      disabled={autoLogin.running}
                      title={t('cf.autoGetTokenHint')}
                    >
                      🔄 {t('cf.retry')}
                    </button>
                  );
                  if (tk.has_token && tk.stale)
                    return (
                      <span className="cf-token-hint warn">
                        ✓ {t('cf.tokenCached')}{ageText} {t('cf.tokenStale')} {RetryBtn}
                      </span>
                    );
                  if (tk.has_token)
                    return <span className="cf-token-hint ok">✓ {t('cf.tokenCached')}{ageText}</span>;
                  if (tk.need_captcha)
                    return <span className="cf-token-hint warn">{t('cf.tokenNeedCaptcha')}</span>;
                  if (tk.last_error)
                    return (
                      <span className="cf-token-hint err">
                        {t('cf.tokenLoginFailed')}：{tk.last_error} {RetryBtn}
                      </span>
                    );
                  return null;
                })()}
              </div>
            </div>
            <div className="cf-cfg-row">
              <div className="cf-cfg-field cf-cfg-field--full">
                <label>{t('cf.proxy')}</label>
                <input
                  className="input"
                  placeholder="http://127.0.0.1:7890"
                  value={cfg.proxy}
                  onChange={(e) => setCfg({ ...cfg, proxy: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
            </div>
            <div className="cf-cfg-row cf-captcha-row">
              <div className="cf-cfg-field cf-cfg-field--captcha">
                <label>{t('cf.captcha')}</label>
                <div className="cf-captcha-img-wrap">
                  {captcha.image ? (
                    <img
                      className="cf-captcha-img"
                      src={captcha.image}
                      alt={t('cf.captcha')}
                      title={t('cf.refresh')}
                      onClick={fetchCaptcha}
                    />
                  ) : (
                    <div
                      className="cf-captcha-img empty"
                      onClick={fetchCaptcha}
                      title={t('cf.getCaptcha')}
                    >
                      {t('cf.getCaptcha')}
                    </div>
                  )}
                  <button
                    className="btn btn-ghost btn-xs"
                    onClick={fetchCaptcha}
                    disabled={loading.captcha}
                  >
                    🔄 {t('cf.refresh')}
                  </button>
                </div>
              </div>
              <div className="cf-cfg-field">
                <label>{t('cf.captchaInput')}</label>
                <input
                  className="input"
                  placeholder={t('cf.captchaInputPlaceholder')}
                  maxLength={8}
                  autoComplete="off"
                  value={imageCode}
                  onChange={(e) => setImageCode(e.target.value)}
                />
              </div>
            </div>
            <div className="cf-cfg-actions">
              <button
                className="btn btn-sm btn-primary"
                onClick={login}
                disabled={loading.login}
              >
                {loading.login ? t('cf.loggingInShort') : t('cf.loginToken')}
              </button>
              <button
                className="btn btn-sm"
                onClick={autoGetTokens}
                disabled={autoLogin.running}
                title={t('cf.autoGetTokenHint')}
              >
                {autoLogin.running ? `⏳ ${t('cf.autoGetting')}` : `🔑 ${t('cf.autoGetToken')}`}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ===== 查询卡片 ===== */}
      <div className="card-soft cf-query-card">
        <div className="cf-query-row">
          <div className="cf-cfg-field cf-cfg-field--main">
            <label>{t('cf.logType')}</label>
            <input
              className="input"
              placeholder="salary_seal_delay_payment_vvv1"
              value={cfg.log_type}
              onChange={(e) => setCfg({ ...cfg, log_type: e.target.value })}
              onBlur={() => saveCfg()}
              onKeyDown={(e) => e.key === 'Enter' && queryLogs()}
            />
          </div>
          <div className="cf-cfg-field cf-cfg-field--main">
            <label>{t('cf.recordModel')}</label>
            <input
              className="input"
              list="cf-record-models"
              placeholder="dynamic_log"
              value={cfg.record_model}
              onChange={(e) => setCfg({ ...cfg, record_model: e.target.value })}
              onBlur={() => saveCfg()}
              onKeyDown={(e) => e.key === 'Enter' && queryLogs()}
            />
            <datalist id="cf-record-models">
              <option value="dynamic_log" />
              <option value="SyncOuterRecord" />
              <option value="async_task_log" />
              <option value="operation_log" />
              <option value="api_log" />
            </datalist>
          </div>
          <div className="cf-cfg-field cf-cfg-field--w100">
            <label>{t('cf.pageSize')}</label>
            <input
              className="input input-sm"
              type="number"
              min={1}
              max={12000}
              value={cfg.page_size}
              onChange={(e) => setCfg({ ...cfg, page_size: parseInt(e.target.value) || 200 })}
              onBlur={() => saveCfg()}
            />
          </div>
          <div className="cf-cfg-field cf-cfg-field--w80">
            <label>{t('cf.pageIndex')}</label>
            <input
              className="input input-sm"
              type="number"
              min={1}
              value={cfg.page_index}
              onChange={(e) => setCfg({ ...cfg, page_index: parseInt(e.target.value) || 1 })}
              onBlur={() => saveCfg()}
            />
          </div>
          <button
            className="btn btn-primary cf-query-btn"
            onClick={queryLogs}
            disabled={loading.query}
          >
            {loading.query ? t('cf.querying') : t('cf.query')}
          </button>
          <button
            className="btn cf-query-btn"
            onClick={exportLogs}
            disabled={loading.export || !result?.rows.length}
          >
            {t('cf.export')}
          </button>
          <button
            className="btn btn-ghost cf-query-btn"
            onClick={clipboardSave}
            disabled={loading.clipboard}
          >
            📋 {t('cf.clipboardToFile')}
          </button>
          {/* 实时刷新：后端轮询最新页 + SSE 推送，开启后新日志自动进列表 */}
          <select
            className="sel cf-live-interval"
            value={streamInterval}
            onChange={(e) => setStreamInterval(parseInt(e.target.value) || 5)}
            title={t('cf.liveHint')}
          >
            <option value={5}>5s</option>
            <option value={10}>10s</option>
            <option value={30}>30s</option>
            <option value={60}>60s</option>
          </select>
          <button
            className={'btn cf-query-btn' + (streaming ? ' btn-live-on' : ' btn-ghost')}
            onClick={toggleStream}
            disabled={loading.stream}
            title={t('cf.liveHint')}
          >
            {streaming ? `⏹ ${t('cf.liveStop')}` : `▶ ${t('cf.liveRefresh')}`}
          </button>
        </div>

        {/* ===== 时间范围过滤：预设「最近 1 小时 / 1 天…」+ 手动起止（改动起止即切自定义）。
               过滤实时作用于结果；上方的「导出」按钮会导出当前时间段的日志并复制文件路径给 AI ===== */}
        <div className="cf-query-row cf-time-row">
          <div className="cf-cfg-field cf-cfg-field--w120">
            <label>{t('cf.timeRange')}</label>
            <select
              className="sel"
              value={cfg.time_preset}
              onChange={(e) => onPresetChange(e.target.value as TimePreset)}
            >
              {TIME_PRESETS.map((p) => (
                <option key={p.key} value={p.key}>{t(p.labelKey)}</option>
              ))}
            </select>
          </div>
          <div className="cf-cfg-field cf-cfg-field--time">
            <label>{t('cf.timeFrom')}</label>
            <input
              className="input input-sm"
              type="datetime-local"
              value={cfg.time_start}
              onChange={(e) => onTimeInput('start', e.target.value)}
            />
          </div>
          <div className="cf-cfg-field cf-cfg-field--time">
            <label>{t('cf.timeTo')}</label>
            <input
              className="input input-sm"
              type="datetime-local"
              value={cfg.time_end}
              onChange={(e) => onTimeInput('end', e.target.value)}
            />
          </div>
          <span className="cf-time-hint">{t('cf.timeHint')}</span>
        </div>

        {status.text && <div className={`cf-query-status ${status.cls}`}>{status.text}</div>}
      </div>

      {/* ===== 日志搜索 / 过滤工具栏 ===== */}
      {result && (
        <div className="cf-search-bar card-soft">
          <input
            className="input cf-search-input"
            placeholder={t('cf.searchPlaceholder')}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setActiveMatch(e.target.value.trim() ? 1 : 0);
              if (resultRef.current) setResult({ ...resultRef.current, localPage: 1 });
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                gotoMatch(e.shiftKey ? -1 : 1);
              }
            }}
          />
          <label className="cf-search-case" title={t('cf.caseSensitive')}>
            <input
              type="checkbox"
              checked={caseSensitive}
              onChange={(e) => setCaseSensitive(e.target.checked)}
            />{' '}
            {t('cf.caseSensitive')}
          </label>
          <label className="cf-search-case" title={t('cf.filterToggleHint')}>
            <input
              type="checkbox"
              checked={filterOn}
              onChange={(e) => {
                setFilterOn(e.target.checked);
                if (resultRef.current) setResult({ ...resultRef.current, localPage: 1 });
              }}
            />{' '}
            {t('cf.filterToggle')}
          </label>
          <button
            className="btn btn-sm btn-ghost cf-btn-sort-time"
            onClick={toggleSort}
            disabled={loading.sort}
          >
            {t('cf.time')} {sortDir === 'asc' ? '↑' : '↓'}
          </button>
          <span className="cf-search-count">
            {search
              ? filterOn
                ? `匹配 ${view.all} / ${view.isFull ? '全部' : '本页'} ${result.rows.length}`
                : `高亮 ${view.matchTotal} 处 · ${view.matchRows} 行（未过滤）`
              : view.isFull
              ? `共 ${view.all} 条`
              : `本页 ${view.all} / 共 ${result.total} 条`}
          </span>
          {search && view.matchTotal > 0 && (
            <span className="cf-match-nav">
              <button
                className="btn btn-xs btn-ghost"
                onClick={() => gotoMatch(-1)}
                title={t('cf.prevMatch')}
              >
                ↑
              </button>
              <span className="cf-match-pos">
                {activeMatch} / {view.matchTotal}
              </span>
              <button
                className="btn btn-xs btn-ghost"
                onClick={() => gotoMatch(1)}
                title={t('cf.nextMatch')}
              >
                ↓
              </button>
            </span>
          )}
        </div>
      )}

      {!result && <div className="empty-hint">{t('cf.startHint')}</div>}

      {result && view.rows.length === 0 && (
        <div className="empty-hint">{t('cf.noMatch')}</div>
      )}
      {result && search && !filterOn && view.matchTotal === 0 && view.rows.length > 0 && (
        <div className="empty-hint">{t('cf.noMatch')}</div>
      )}

      {/* ===== 日志结果表 ===== */}
      {result && view.rows.length > 0 && (
        <div className="cf-results">
          <div className="cf-result-meta">
            <span className="cf-result-count">
              {search
                ? filterOn
                  ? `匹配 ${view.all} 条`
                  : `高亮 ${view.matchTotal} 处 · ${view.matchRows} 行（未过滤）`
                : view.isFull
                ? `共 ${view.all} 条`
                : `本页 ${view.all} / 共 ${result.total} 条`}
            </span>
            {view.isFull && <span>{t('cf.fullLoaded')}</span>}
          </div>
          <div className="table-scroll">
            <table className="cf-log-table">
              <thead>
                <tr>
                  <th style={{ width: 48 }}>#</th>
                  <th style={{ width: 150 }}>{t('cf.colType')}</th>
                  <th style={{ width: 170 }}>{t('cf.colTime')}</th>
                  <th>{t('cf.colContent')}</th>
                </tr>
              </thead>
              <tbody>
                {view.rows.map((row, i) => {
                  const createTime = cfTime(row);
                  const content = cfContent(row);
                  const contentFull = cfContentFull(row);
                  const logTypeVal = cfLogType(row, result.log_type);
                  const globalIdx = result.rows.length - view.rows.length + i;
                  const mContent = q ? findMatches(content, q, caseSensitive) : [];
                  const mTime = q ? findMatches(createTime, q, caseSensitive) : [];
                  const mType = q ? findMatches(logTypeVal, q, caseSensitive) : [];
                  const mFull = q ? findMatches(contentFull, q, caseSensitive) : [];
                  const rowCount = mContent.length + mTime.length + mType.length;
                  const rowOrdStart = rowOrd;
                  rowOrd += rowCount;
                  return (
                    <FragmentRow
                      key={globalIdx}
                      displayIndex={i}
                      idx={i + 1}
                      type={logTypeVal}
                      time={createTime}
                      content={content}
                      contentFull={contentFull}
                      mContent={mContent}
                      mTime={mTime}
                      mType={mType}
                      mFull={mFull}
                      rowOrdStart={rowOrdStart}
                      activeMatch={activeMatch}
                      rowId={
                        row.id != null
                          ? String(row.id)
                          : row._id != null
                          ? String(row._id)
                          : ''
                      }
                      serverUrl={result.server_url}
                      openLogLabel={t('cf.openLog')}
                      expanded={expanded === globalIdx}
                      onToggle={() => setExpanded(expanded === globalIdx ? null : globalIdx)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
          {view.isFull && view.totalPages > 1 && (
            <div className="cf-pagination">
              {view.localPage > 1 && (
                <button className="btn btn-sm" onClick={() => goLocalPage(view.localPage - 1)}>
                  {t('cf.prevPage')}
                </button>
              )}
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                {view.localPage} / {view.totalPages}（本地，第 1 页为最新）
              </span>
              {view.localPage < view.totalPages && (
                <button className="btn btn-sm" onClick={() => goLocalPage(view.localPage + 1)}>
                  {t('cf.nextPage')}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FragmentRow(props: {
  displayIndex: number;
  idx: number;
  type: string;
  time: string;
  content: string;
  contentFull: string;
  mContent: Array<[number, number]>;
  mTime: Array<[number, number]>;
  mType: Array<[number, number]>;
  mFull: Array<[number, number]>;
  rowOrdStart: number;
  activeMatch: number;
  rowId: string;
  serverUrl: string;
  openLogLabel: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const {
    displayIndex,
    idx,
    type,
    time,
    content,
    contentFull,
    mContent,
    mTime,
    mType,
    mFull,
    rowOrdStart,
    activeMatch,
    rowId,
    serverUrl,
    openLogLabel,
    expanded,
    onToggle,
  } = props;
  return (
    <>
      <tr id={`cf-row-${displayIndex}`} className="cf-log-row" onClick={onToggle} style={{ cursor: 'pointer' }}>
        <td>{idx}</td>
        <td className="cf-log-type" title={type}>
          {highlightNodes(type, mType, rowOrdStart + mContent.length + mTime.length, activeMatch)}
          <button
            type="button"
            className="cf-open-log"
            title={openLogLabel}
            aria-label={openLogLabel}
            onClick={(e) => {
              e.stopPropagation();
              void openCloudFunctionLogs(serverUrl, type);
            }}
          >
            ↗
          </button>
        </td>
        <td className="cf-log-time">
          {highlightNodes(time, mTime, rowOrdStart + mContent.length, activeMatch)}
        </td>
        <td className="cf-log-content">{highlightNodes(content, mContent, rowOrdStart, activeMatch)}</td>
      </tr>
      {expanded && (
        <tr className="cf-log-detail-row">
          <td colSpan={4}>
            <div className="cf-log-meta">
              类型：{type} ｜ ID：{rowId} ｜ 时间：{time}
            </div>
            <div className="cf-log-content-full">
              {highlightNodes(contentFull, mFull, rowOrdStart, activeMatch)}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
