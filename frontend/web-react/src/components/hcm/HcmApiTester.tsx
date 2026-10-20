import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { hcmDirect } from '../../api/hcm/direct';
import { hcmWsProxyUrl } from '../../api/hcm/ws';
import { hcmEnvs, HcmApiError, type HcmEnv } from '../../api/hcm/client';
import { reportLocalApiNetworkError } from '../../api/client';
import { saveBlob } from '../../utils/download';
import { useT } from '../../i18n';
import { reportPanelOutput } from '../../api/aiBridge';
import { JsonTree } from '../JsonTree';
import { CodeView } from '../CodeView';

/**
 * HCM 接口直调面板 —— 设计与功能对齐网关自带调试页
 * （hcm-core templates/api/api_ui.html + api_list.html + static/api/api.js）：
 *
 *   标题（描述：接口名）→ API 文档 → 输入（JSON）→ 提交行（提交 + SQL/Profile 勾选）
 *   → 结果（View 下拉：视图树/文本/代码 + 搜索 + 耗时 + 下载接口日志/性能日志
 *     + 查看明细错误 + 调用堆栈）
 *
 * 接口列表：调平台 Open API `hcm.api.list`（core/service/services.py，返回
 * {list:[{name,description}], count}，支持 filter_str + 分页）拉全量，输入框即
 * 可搜索下拉（名称/描述本地过滤，↑↓ 选择），按网关缓存到 localStorage 24h；
 * 拉取失败时回退到内置常用接口。
 *
 * WS 调试通道（提交行「WS」按钮）：网关 `ws(s)://<host>/ws/<api>`，握手带
 * Cookie token、首帧发**明文业务参数**（无 REST 加密信封），回包
 * `{status: notify|success|error, data}`；notify 为实时进度、success/error
 * 为终态（随后网关关闭，一次性会话）。浏览器不直连网关（跨域 + 密钥），
 * 由后端 `/api/hcm/ws` 双向桥接；仅声明 notify 的 handler 支持（长任务类接口）。
 *
 * 差异说明：
 * - 请求统一走后端 /api/hcm/direct（加解密+签名+token 自动续期），前端零密钥；
 * - WS 通道参数为明文（对齐平台 api.js 的 callHcmOpenWebSocket），无需信封；
 * - 网关返回非 JSON（HTML 错误页/异常页）时，对齐调试页 responseText 的处理：
 *   原文进红色结果块，视图自动切到代码视图（树只对结构化响应有意义）。
 */

/** 内置常用接口兜底（hcm.api.list 不可用 / 权限不足时仍可联想）。doc 文案见 i18n hcm.apiDoc_* */
interface CommonApi {
  name: string;
  docKey: string;
  /** 会写线上数据的危险接口 */
  danger?: boolean;
}

const COMMON_APIS: CommonApi[] = [
  { name: 'hcm.model.list', docKey: 'apiDoc_list' },
  { name: 'hcm.model.info', docKey: 'apiDoc_info' },
  { name: 'hcm.model.view', docKey: 'apiDoc_view' },
  { name: 'hcm.model.count', docKey: 'apiDoc_count' },
  { name: 'hcm.model.update', docKey: 'apiDoc_update', danger: true },
  { name: 'hcm.model.add', docKey: 'apiDoc_add', danger: true },
  { name: 'hcm.model.delete', docKey: 'apiDoc_delete', danger: true },
  { name: 'hcm.model.edit', docKey: 'apiDoc_edit', danger: true },
  { name: 'hcm.model.edit.batch', docKey: 'apiDoc_editBatch', danger: true },
  { name: 'hcm.paas.object.list', docKey: 'apiDoc_paasList' },
  { name: 'dynamic.script.test', docKey: 'apiDoc_dynScriptTest' },
];

interface ApiOption {
  name: string;
  description: string;
  danger?: boolean;
}

/** 下拉最多渲染条数（全量可能数千，过滤后再截断保证流畅） */
const DD_MAX_ITEMS = 80;
/** hcm.api.list 单页条数（服务端直接切片，500 一次拉） */
const LIST_PAGE_SIZE = 5000;
const LIST_CONCURRENCY = 4; // 网关对单 token 并发有限流，8 并发实测会拒掉一半
const LIST_MAX_PAGES = 20; // 单页 5000 → 覆盖 10 万接口上限，防止异常 count 拖死请求
/** 接口列表缓存有效期 */
const LIST_CACHE_TTL = 24 * 3600 * 1000;

/** 响应预览截断长度（超出仍可复制完整文本） */
const PREVIEW_LIMIT = 200_000;

interface SendResult {
  text: string;
  truncated: boolean;
  gateway: string;
  logIndex: string;
  profileIndex: string;
  /** 网关 srv_end - srv_begin（毫秒） */
  durationMs?: number;
  /** sql_debug=1 时网关返回的 SQL 语句（带 [耗时ms] 前缀） */
  sqlTrace?: string[];
  /** 网关业务错误：结果块红色 + traceback + 明细错误链接 */
  isError: boolean;
  errorCode?: string;
  /** 错误摘要（errmsg）：raw 原文（HTML 错误页）优先展示时，摘要行保证可读性 */
  errmsg?: string;
  traceback?: string;
}

/** WS 调试会话状态（对齐调试页 WS 按钮：连接→发参数→notify 进度→终态） */
interface WsSession {
  phase: 'connecting' | 'running' | 'done' | 'error';
  /** notify 进度消息（pong 心跳已过滤） */
  messages: any[];
  /** 终态回包 */
  final?: { status: string; data: any };
  error?: string;
}

export function HcmApiTester() {
  const { t } = useT();

  const [apiName, setApiName] = useState('hcm.model.list');
  const [paramsText, setParamsText] = useState('{\n  "model": ""\n}');
  const [target, setTarget] = useState('');
  const [envs, setEnvs] = useState<HcmEnv[]>([]);
  const [sqlDebug, setSqlDebug] = useState(false);
  const [profileDebug, setProfileDebug] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [result, setResult] = useState<SendResult | null>(null);
  const [dlBusy, setDlBusy] = useState('');
  // 结果区视图（对齐调试页 View 下拉）：视图（object 树，默认）/ 文本 / 代码
  const [view, setView] = useState<'tree' | 'text' | 'code'>('tree');
  const [viewOpen, setViewOpen] = useState(false);
  const viewBoxRef = useRef<HTMLDivElement>(null);
  const [respSearch, setRespSearch] = useState('');
  // WS 调试会话（null = 未启动）
  const [ws, setWs] = useState<WsSession | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // AI 数据桥：接口调试的请求+响应同步给 /api/ai/output/hcmapi（供 AI 拉取）
  useEffect(() => {
    if (!result) return;
    reportPanelOutput('hcmapi', {
      text: `API: ${apiName}\nGateway: ${result.gateway}\nStatus: ${result.isError ? 'ERROR' : 'OK'}\n\n${result.text}`,
      meta: { api: apiName, error: result.isError, duration_ms: result.durationMs },
    });
  }, [result, apiName]);

  // —— 接口列表（hcm.api.list）——
  const [apiOptions, setApiOptions] = useState<ApiOption[]>([]);
  const [apiListState, setApiListState] = useState<'loading' | 'ready' | 'error'>('loading');
  const [ddOpen, setDdOpen] = useState(false);
  const [ddHl, setDdHl] = useState(-1);
  const nameBoxRef = useRef<HTMLDivElement>(null);

  // 网关候选：后端从 cf_accounts / hcm_whitelist 汇总的环境列表
  useEffect(() => {
    let alive = true;
    hcmEnvs()
      .then((list) => { if (alive) setEnvs(list); })
      .catch(() => { /* 候选拉取失败不阻塞：仍可用默认网关 */ });
    return () => { alive = false; };
  }, []);

  // —— 拉取全量接口列表（翻页 + localStorage 缓存）——
  // 序号防竞态：切网关后，旧网关的慢失败（如默认 502）不得覆盖新网关的结果
  const fetchSeq = useRef(0);
  const fetchApiList = useCallback(async (gw: string, force = false) => {
    const seq = ++fetchSeq.current;
    const stale = () => seq !== fetchSeq.current;
    // v2：带 count 校验的缓存格式（作废旧版无 count 的不完整缓存）
    const cacheKey = `hcmApiList:v2:${gw || 'default'}`;
    if (!force) {
      try {
        const cached = JSON.parse(localStorage.getItem(cacheKey) || 'null');
        if (cached && Date.now() - (cached.ts || 0) < LIST_CACHE_TTL
            && Array.isArray(cached.list) && cached.list.length
            // 旧版本/异常中断可能缓存了不完整列表（count 不足则重新拉全量）
            && !(cached.count > 0 && cached.list.length < cached.count)) {
          if (stale()) return;
          setApiOptions(cached.list);
          setApiListState('ready');
          return;
        }
      } catch { /* 缓存损坏则重新拉取 */ }
    }
    setApiListState('loading');
    try {
      // 实测 page_size=5000 单页 ~3s（平台接口总量可达 4 万+）。首页拿 count，
      // 剩余页分块并发（网关对单 token 高并发会拒掉部分请求，故每块 4 页），不足的页再串行重试一轮。
      const first = await hcmDirect('', 'hcm.api.list',
        { filter_str: '', page_index: 1, page_size: LIST_PAGE_SIZE }, '', gw);
      const firstList = (first.data?.list || []) as ApiOption[];
      const count = Number(first.data?.count ?? 0);
      if (stale()) return;
      if (!firstList.length) throw new Error('empty api list');
      let all: ApiOption[] = [...firstList];
      const totalPages = Math.min(
        count > 0 ? Math.ceil(count / LIST_PAGE_SIZE) : 1,
        LIST_MAX_PAGES,
      );
      const fetchPage = async (page: number): Promise<ApiOption[]> => {
        try {
          const r = await hcmDirect('', 'hcm.api.list',
            { filter_str: '', page_index: page, page_size: LIST_PAGE_SIZE }, '', gw);
          return (r.data?.list || []) as ApiOption[];
        } catch { return []; }
      };
      if (totalPages > 1) {
        const restPages = Array.from({ length: totalPages - 1 }, (_, i) => i + 2);
        const parts: ApiOption[][] = [];
        for (let i = 0; i < restPages.length; i += LIST_CONCURRENCY) {
          parts.push(...await Promise.all(
            restPages.slice(i, i + LIST_CONCURRENCY).map(fetchPage),
          ));
          if (stale()) return;
        }
        for (const part of parts) all.push(...part);
        // 串行重试一轮拿空的页（最后一页长度可少于 LIST_PAGE_SIZE）
        const emptyPages = Array.from({ length: totalPages - 1 }, (_, i) => i + 2)
          .filter((p) => {
            const idx = p - 2;
            return !parts[idx] || parts[idx].length === 0;
          });
        for (const p of emptyPages) {
          const retry = await fetchPage(p);
          if (retry.length) all.push(...retry);
        }
      }
      // 按 name 去重（重试可能带来重叠）
      const seen = new Set<string>();
      all = all.filter((o) => (o?.name && !seen.has(o.name) ? (seen.add(o.name), true) : false));
      if (stale()) return;
      if (!all.length) throw new Error('empty api list');
      setApiOptions(all);
      setApiListState('ready');
      // 仅在拿全时写缓存（count 不足的部分结果不缓存，否则 24h 内一直缺斤短两）；
      // 4 万+ 接口序列化可能超 localStorage 配额，失败则跳过缓存
      const complete = !(count > 0) || all.length >= count;
      if (complete) {
        try { localStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), count, list: all })); } catch { /* 忽略 */ }
      }
    } catch {
      // 权限不足（hcm.api.list 需 sys-manager 等）/ 接口不可用：回退内置常用接口
      if (stale()) return;
      setApiOptions([]);
      setApiListState('error');
    }
  }, []);

  // 网关变化即预取（缓存命中则瞬时）
  useEffect(() => { void fetchApiList(target); }, [target, fetchApiList]);

  // 点击面板外关闭下拉
  useEffect(() => {
    if (!ddOpen) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (nameBoxRef.current && !nameBoxRef.current.contains(e.target as Node)) {
        setDdOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [ddOpen]);

  // 点击面板外关闭结果区视图下拉
  useEffect(() => {
    if (!viewOpen) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (viewBoxRef.current && !viewBoxRef.current.contains(e.target as Node)) {
        setViewOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocMouseDown);
    return () => document.removeEventListener('mousedown', onDocMouseDown);
  }, [viewOpen]);

  // 新响应到达 → 按内容自动选视图（对齐调试页：结构化响应走 object 树，
  // HTML 错误页 / 纯文本走代码视图——树对非 JSON 无意义）。
  // 仅在**新内容**时切换；同一响应内用户手动切视图不被覆盖。
  const lastAutoViewText = useRef('');
  useEffect(() => {
    const wsFinal = ws?.final;
    const text = wsFinal
      ? (typeof wsFinal.data === 'string'
          ? wsFinal.data
          : JSON.stringify(wsFinal.data ?? null, null, 2))
      : (result?.text ?? '');
    if (!text || text === lastAutoViewText.current) return;
    lastAutoViewText.current = text;
    let parsed: unknown;
    try { parsed = JSON.parse(text); } catch { parsed = undefined; }
    setView(parsed !== null && typeof parsed === 'object' ? 'tree' : 'code');
  }, [result, ws]);

  // 下拉数据源：远端列表，失败回退内置常用接口
  const options: ApiOption[] = useMemo(() => (
    apiListState === 'error'
      ? COMMON_APIS.map((a) => ({ name: a.name, description: t(`hcm.${a.docKey}Body`), danger: a.danger }))
      : apiOptions
  ), [apiListState, apiOptions, t]);

  const ddFiltered = useMemo(() => {
    const q = apiName.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) =>
      o.name.toLowerCase().includes(q) || (o.description || '').toLowerCase().includes(q));
  }, [options, apiName]);

  // 命中接口 → 显示「{描述}：{名称}」标题 + API 文档块（对齐网关调试页标题/文档区）
  const matchedInfo = useMemo(() => {
    const name = apiName.trim();
    const common = COMMON_APIS.find((a) => a.name === name);
    if (common) {
      return { name, danger: common.danger, desc: t(`hcm.${common.docKey}Body`), hasDesc: true };
    }
    const remote = options.find((o) => o.name === name);
    if (remote) return { name, danger: false, desc: remote.description || '', hasDesc: !!remote.description };
    return null;
  }, [apiName, options, t]);

  const formatParams = useCallback(() => {
    try {
      const obj = JSON.parse(paramsText);
      setParamsText(JSON.stringify(obj, null, 2));
      setErr('');
    } catch (e: any) {
      setErr(t('hcm.payloadParseFailed', { msg: e?.message || String(e) }));
    }
  }, [paramsText, t]);

  const pickApi = useCallback((name: string) => {
    setApiName(name);
    setDdOpen(false);
    setDdHl(-1);
  }, []);

  const send = useCallback(async () => {
    if (busy) return;
    let params: Record<string, any>;
    try {
      params = JSON.parse(paramsText);
      setErr('');
    } catch (e: any) {
      setErr(t('hcm.payloadParseFailed', { msg: e?.message || String(e) }));
      return;
    }
    if (!apiName.trim()) {
      setErr(t('hcm.apiNeedName'));
      return;
    }
    setBusy(true);
    setErr('');
    setDdOpen(false);
    // 结果区展示以「最新一次请求」为准：清掉上一轮 WS 终态（wsStart 对称清 result）。
    // 仅清终态会话；WS 仍在途时保留其 notify 进度面板（罕见并发现场）。
    setWs((s) => (s && (s.phase === 'done' || s.phase === 'error') ? null : s));
    try {
      const { data, gateway, meta } = await hcmDirect('', apiName.trim(), params, '', target, {
        sqlDebug,
        profileDebug,
      });
      const text = JSON.stringify(data, null, 2);
      setResult({
        text,
        truncated: text.length > PREVIEW_LIMIT,
        gateway: gateway || '',
        logIndex: String(meta?.log_index ?? ''),
        profileIndex: String(meta?.profile_index ?? ''),
        sqlTrace: Array.isArray(meta?.sql_trace) ? meta.sql_trace : undefined,
        durationMs: typeof meta?.duration_ms === 'number' ? meta.duration_ms : undefined,
        isError: false,
      });
    } catch (e: any) {
      if (e instanceof HcmApiError && e.payload) {
        // 网关业务错误：对齐网关调试页 error 分支——红色结果块 + traceback + 明细错误 + 日志下载
        const p = e.payload;
        // 网关非 JSON 错误页（HTML）：error.raw 是字符串原文，直接展示（代码视图高亮）；
        // JSON 错误响应则序列化保留结构（树视图可展开）
        const rawText = p.raw !== undefined
          ? (typeof p.raw === 'string' ? p.raw : JSON.stringify(p.raw, null, 2))
          : '';
        setResult({
          text: rawText || p.errmsg || e.message,
          truncated: rawText.length > PREVIEW_LIMIT,
          gateway: target,
          logIndex: String(p.log_index ?? ''),
          profileIndex: String(p.profile_index ?? ''),
          sqlTrace: Array.isArray(p.sql_trace) ? p.sql_trace : undefined,
          isError: true,
          errorCode: p.error_code != null && p.error_code !== ''
            ? String(p.error_code)
            : (p.errcode != null ? String(p.errcode) : undefined),
          errmsg: p.errmsg || '',
          traceback: p.traceback || '',
        });
      } else {
        const msg = e instanceof HcmApiError ? e.message : (e?.message || String(e));
        setErr(msg);
        setResult(null);
      }
    } finally {
      setBusy(false);
    }
  }, [busy, paramsText, apiName, target, sqlDebug, profileDebug, t]);

  // —— WS 调试通道（对齐调试页 WS 按钮：流式代理 + notify 进度 + 终态）——
  const wsStop = useCallback(() => {
    const sock = wsRef.current;
    if (sock) {
      try { sock.close(1000, 'client stop'); } catch { /* 已关闭则忽略 */ }
      wsRef.current = null;
    }
    setWs((s) => (s && s.phase !== 'done' ? { ...s, phase: 'done' } : s));
  }, []);

  const wsStart = useCallback(() => {
    if (wsRef.current) return; // 已在会话中
    let params: Record<string, any>;
    try {
      params = JSON.parse(paramsText);
    } catch (e: any) {
      setErr(t('hcm.payloadParseFailed', { msg: e?.message || String(e) }));
      return;
    }
    if (!apiName.trim()) {
      setErr(t('hcm.apiNeedName'));
      return;
    }
    setErr('');
    setResult(null);
    setWs({ phase: 'connecting', messages: [] });

    let sock: WebSocket;
    try {
      sock = new WebSocket(hcmWsProxyUrl(apiName.trim(), target, '', { sqlDebug, profileDebug }));
    } catch (e: any) {
      setWs({ phase: 'error', messages: [], error: e?.message || String(e) });
      return;
    }
    wsRef.current = sock;

    sock.onopen = () => {
      // 首帧：明文业务参数（平台 api.js 同款语义）
      sock.send(JSON.stringify(params));
      setWs((s) => (s ? { ...s, phase: 'running' } : s));
    };
    sock.onmessage = (ev) => {
      let msg: any;
      try { msg = JSON.parse(ev.data as string); } catch {
        // 非 JSON 帧（网关异常页/裸文本）：对齐后端 ws_call 语义——按 error
        // 终态处理并保留原文（HTML 进代码视图），不再静默丢弃。
        setWs((s) => (s ? { ...s, phase: 'done', error: undefined, final: { status: 'error', data: ev.data } } : s));
        reportPanelOutput('hcmapi', {
          text: `API(ws): ${apiName}\nGateway: ${target || '(default)'}\nStatus: error (non-JSON frame)\n\n${ev.data}`,
          meta: { api: apiName, mode: 'ws', error: true },
        });
        try { sock.close(1000, 'terminal'); } catch { /* 忽略 */ }
        return;
      }
      const status = String(msg?.status || '');
      if (status === 'notify') {
        if (msg?.data === 'pong') return; // 心跳
        setWs((s) => (s ? { ...s, messages: [...s.messages, msg?.data] } : s));
        return;
      }
      if (status === 'success' || status === 'error') {
        // 终态覆盖此前的连接层错误（onclose 可能先于终态帧到达过一次）
        setWs((s) => (s ? { ...s, phase: 'done', error: undefined, final: { status, data: msg?.data } } : s));
        // AI 数据桥：WS 终态同样同步给 /api/ai/output/hcmapi
        reportPanelOutput('hcmapi', {
          text: `API(ws): ${apiName}\nGateway: ${target || '(default)'}\nStatus: ${status}\n\n${JSON.stringify(msg?.data, null, 2)}`,
          meta: { api: apiName, mode: 'ws', error: status === 'error' },
        });
        try { sock.close(1000, 'terminal'); } catch { /* 忽略 */ }
      }
    };
    sock.onerror = () => {
      // 已拿到终态（含非 JSON 帧终态）：连接随后关闭是正常后续，不得覆盖
      setWs((s) => (s && !s.final ? { ...s, phase: 'error', error: t('hcm.apiWsFailed') } : s));
    };
    sock.onclose = () => {
      wsRef.current = null;
      setWs((s) => {
        // 终态已到达（success/error 帧，或非 JSON 帧终态）：网关/后端随后关连接
        // 属协议既定行为（一次性会话），保持 done + final，不弹「连接已关闭」
        if (!s || s.final || s.phase === 'done') return s;
        return { ...s, phase: 'error', error: s.error || t('hcm.apiWsClosed') };
      });
    };
  }, [paramsText, apiName, target, t]);

  // 卸载时关闭在途 WS（切 tab / 关闭面板不留半开连接）
  useEffect(() => () => {
    try { wsRef.current?.close(1000, 'unmount'); } catch { /* 忽略 */ }
    wsRef.current = null;
  }, []);

  const onNameKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (ddOpen && ddFiltered.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setDdHl((h) => Math.min(h + 1, ddFiltered.length - 1)); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setDdHl((h) => Math.max(h - 1, 0)); return; }
      if (e.key === 'Enter' && ddHl >= 0) { e.preventDefault(); pickApi(ddFiltered[ddHl].name); return; }
    }
    if (e.key === 'Escape') { setDdOpen(false); return; }
    if (e.key === 'Enter') { void send(); }
  }, [ddOpen, ddFiltered, ddHl, pickApi, send]);

  // 下载网关临时文件（接口日志 / SQL Profile）：token 留空 = 后端用预设代取
  const downloadTemp = useCallback(async (index: string, kind: 'log' | 'profile') => {
    if (!index || dlBusy) return;
    setDlBusy(kind);
    try {
      let res: Response;
      try {
        res = await fetch('/api/hcm/temp-download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ index, name: '', token: '', target }),
        });
      } catch (e) {
        reportLocalApiNetworkError('/api/hcm/temp-download', e);
        throw e;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        const detail = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
        throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
      }
      const blob = await res.blob();
      const fname = index.includes('.') ? index : `${index}.log`;
      saveBlob(blob, fname);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setDlBusy('');
    }
  }, [dlBusy, target]);

  const copyResult = useCallback(async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.text);
    } catch { /* 剪贴板不可用时静默 */ }
  }, [result]);

  // 「查看明细错误」：网关的错误词典页（api.js error 分支同款链接）
  const errorDetailUrl = result?.errorCode && result.gateway
    ? `${result.gateway.replace(/\/+$/, '')}/server/static/auth/html/error_message.html?error_code=${encodeURIComponent(result.errorCode)}`
    : '';

  return (
    <div className="hcm-api">
      {/* ---- 标题行：接口名（可搜索下拉）+ 网关 ---- */}
      <div className="hcm-api-form">
        <div className="hcm-api-row">
          <div className="hcm-api-field hcm-api-field--name" ref={nameBoxRef}>
            <span className="hcm-api-label">
              {t('hcm.apiNameLabel')}
              {apiListState === 'ready' && (
                <span className="hcm-api-count">{t('hcm.apiListTotal', { n: options.length })}</span>
              )}
            </span>
            <input
              className="input hcm-mono"
              value={apiName}
              onChange={(e) => { setApiName(e.target.value); setDdOpen(true); setDdHl(-1); }}
              onFocus={() => { setDdOpen(true); setDdHl(-1); }}
              onKeyDown={onNameKeyDown}
              placeholder="hcm.model.update"
              spellCheck={false}
              autoComplete="off"
              role="combobox"
              aria-expanded={ddOpen}
              aria-autocomplete="list"
            />
            {ddOpen && (
              <div className="hcm-api-dd" role="listbox">
                {apiListState === 'loading' && (
                  <div className="hcm-api-dd-meta">{t('hcm.apiListLoading')}</div>
                )}
                {apiListState === 'error' && (
                  <div
                    className="hcm-api-dd-meta hcm-api-dd-meta--warn"
                    onMouseDown={(e) => { e.preventDefault(); void fetchApiList(target, true); }}
                    title={t('hcm.apiListFailed')}
                  >
                    {t('hcm.apiListFailed')}
                  </div>
                )}
                {ddFiltered.length === 0 && apiListState !== 'loading' && (
                  <div className="hcm-api-dd-meta">{t('hcm.apiListNoMatch')}</div>
                )}
                {ddFiltered.slice(0, DD_MAX_ITEMS).map((o, i) => (
                  <div
                    key={o.name}
                    role="option"
                    aria-selected={i === ddHl}
                    className={`hcm-api-dd-item${i === ddHl ? ' hcm-api-dd-item--active' : ''}`}
                    onMouseDown={(e) => { e.preventDefault(); pickApi(o.name); }}
                    onMouseEnter={() => setDdHl(i)}
                  >
                    <code className="hcm-api-dd-name">{o.name}</code>
                    {o.description && <span className="hcm-api-dd-desc">{o.description}</span>}
                  </div>
                ))}
                {ddFiltered.length > DD_MAX_ITEMS && (
                  <div className="hcm-api-dd-meta">
                    {t('hcm.apiListTotal', { n: ddFiltered.length })}
                  </div>
                )}
              </div>
            )}
          </div>
          <label className="hcm-api-field hcm-api-field--gw">
            <span className="hcm-api-label">{t('hcm.apiGateway')}</span>
            <select
              className="sel"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              title={t('hcm.apiGatewayHint')}
            >
              <option value="">{t('hcm.apiGatewayDefault')}</option>
              {envs.map((env) => (
                <option key={env.server_url} value={env.server_url}>
                  {env.name} · {env.server_url}{env.domain ? ` · ${env.domain}` : ''}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* ---- 标题 + API 文档（对齐网关调试页「{描述}：{名称}」+ 文档块）---- */}
        {matchedInfo && (
          <>
            <div className="hcm-api-title">
              {matchedInfo.hasDesc ? matchedInfo.desc : matchedInfo.name}
              {'：'}
              <code>{matchedInfo.name}</code>
            </div>
            {matchedInfo.hasDesc && (
              <div className={`hcm-api-doc${matchedInfo.danger ? ' hcm-api-doc--danger' : ''}`}>
                <span className="hcm-api-doc-tag">{t('hcm.apiDoc')}</span>
                {matchedInfo.desc}
                {matchedInfo.danger && (
                  <span className="hcm-api-doc-warn"> ⚠ {t('hcm.apiDangerTip')}</span>
                )}
              </div>
            )}
          </>
        )}

        {/* ---- 输入区（JSON）---- */}
        <div className="hcm-api-row">
          <div className="hcm-api-field hcm-api-field--params">
            <span className="hcm-api-label">
              {t('hcm.apiParams')}
              <button type="button" className="btn btn-sm hcm-api-format" onClick={formatParams}>
                {t('hcm.apiFormat')}
              </button>
            </span>
            <textarea
              className="input hcm-mono hcm-api-params"
              value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
              spellCheck={false}
              placeholder='{ "model": "XXX", "filter_dict": {} }'
            />
          </div>
        </div>

        {/* ---- 提交行（对齐参考页：按钮在左，SQL/Profile 勾选在右）---- */}
        <div className="hcm-api-row hcm-api-submit-row">
          <button className="btn btn-primary btn-sm" onClick={() => void send()} disabled={busy}>
            {busy ? t('hcm.loading') : t('hcm.apiSend')}
          </button>
          {ws && ws.phase !== 'done' && ws.phase !== 'error' ? (
            <button className="btn btn-sm hcm-api-ws-btn" onClick={wsStop}>
              {t('hcm.apiWsStop')}
            </button>
          ) : (
            <button
              className="btn btn-sm hcm-api-ws-btn"
              onClick={wsStart}
              title={t('hcm.apiWsHint')}
            >
              WS
            </button>
          )}
          {busy && <span className="hcm-api-busy" aria-label="loading" />}
          {ws && (ws.phase === 'connecting' || ws.phase === 'running') && (
            <span className="hcm-api-ws-state" aria-label="ws running">
              {ws.phase === 'connecting' ? t('hcm.apiWsConnecting') : t('hcm.apiWsRunning')}
            </span>
          )}
          <span className="spacer" />
          <label className="chk hcm-api-chk" title={t('hcm.apiSqlDebugHint')}>
            <input type="checkbox" checked={sqlDebug} onChange={(e) => setSqlDebug(e.target.checked)} />
            {t('hcm.apiSqlDebug')}
          </label>
          <label className="chk hcm-api-chk" title={t('hcm.apiProfileDebugHint')}>
            <input type="checkbox" checked={profileDebug} onChange={(e) => setProfileDebug(e.target.checked)} />
            {t('hcm.apiProfileDebug')}
          </label>
          <span className="hcm-api-token-hint">{t('hcm.apiTokenHint')}</span>
        </div>

        {err && <div className="hcm-api-err">{err}</div>}
        {ws && ws.phase === 'error' && ws.error && (
          <div className="hcm-api-err">{ws.error}</div>
        )}
        {ws && ws.messages.length > 0 && (
          <details className="hcm-api-ws-msgs">
            <summary>{t('hcm.apiWsNotify', { n: ws.messages.length })}</summary>
            <div className="hcm-api-ws-msg-list">
              {ws.messages.map((m, i) => (
                <div key={i} className="hcm-api-ws-msg">
                  {typeof m === 'string' ? m : JSON.stringify(m)}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>

      {/* ---- 结果区（REST 结果 / WS 终态共用；树/文本视图 + 搜索）---- */}
      {(() => {
        const wsFinal = ws?.final;
        const display: SendResult | null = wsFinal
          ? {
              // WS 终态 data 为字符串（非 JSON 帧原文，如 HTML 错误页）时直接展示
              text: typeof wsFinal.data === 'string'
                ? wsFinal.data
                : JSON.stringify(wsFinal.data ?? null, null, 2),
              truncated: false,
              gateway: target,
              logIndex: '',
              profileIndex: '',
              isError: wsFinal.status !== 'success',
            }
          : result;
        if (!display) return null;
        return (
          <div className={`hcm-api-resp${display.isError ? ' hcm-api-resp--error' : ''}`}>
            <div className="hcm-api-resp-head">
              <span className="hcm-api-label">{t('hcm.apiResponse')}</span>
              {/* View 下拉（对齐调试页：视图 / 文本 / 代码） */}
              <div className="hcm-api-view" ref={viewBoxRef}>
                <button
                  type="button"
                  className="hcm-api-view-btn"
                  aria-haspopup="menu"
                  aria-expanded={viewOpen}
                  onClick={() => setViewOpen((v) => !v)}
                  title={t('hcm.apiViewLabel')}
                >
                  {view === 'tree'
                    ? t('hcm.apiViewView')
                    : view === 'text'
                      ? t('hcm.apiViewText')
                      : t('hcm.apiViewCode')}
                  <span className="hcm-api-view-caret" aria-hidden="true">▾</span>
                </button>
                {viewOpen && (
                  <div className="hcm-api-view-dd" role="menu">
                    {([
                      ['tree', t('hcm.apiViewView')],
                      ['text', t('hcm.apiViewText')],
                      ['code', t('hcm.apiViewCode')],
                    ] as const).map(([v, label]) => (
                      <button
                        key={v}
                        type="button"
                        role="menuitem"
                        aria-checked={view === v}
                        className={`hcm-api-view-dd-item${view === v ? ' active' : ''}`}
                        onClick={() => { setView(v); setViewOpen(false); }}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <input
                className="hcm-api-search"
                type="search"
                value={respSearch}
                placeholder={t('hcm.apiSearch')}
                onChange={(e) => setRespSearch(e.target.value)}
                spellCheck={false}
              />
              {typeof display.durationMs === 'number' && (
                <span
                  className={`hcm-api-duration${display.isError ? ' hcm-api-duration--err' : ''}`}
                  title={`srv_end - srv_begin = ${display.durationMs}ms`}
                >
                  {(display.durationMs / 1000).toFixed(2)}s
                </span>
              )}
              {display.gateway && (
                <code className="hcm-api-gw" title={t('hcm.apiGatewayHint')}>
                  {t('hcm.apiViaGw', { target: display.gateway })}
                </code>
              )}
              <span className="spacer" />
              {display.profileIndex && (
                <button className="btn btn-sm hcm-link-btn" disabled={!!dlBusy}
                        onClick={() => void downloadTemp(display.profileIndex, 'profile')}>
                  {dlBusy === 'profile' ? t('hcm.downloading') : t('hcm.downloadProfile')}
                </button>
              )}
              {display.logIndex && (
                <button className="btn btn-sm hcm-link-btn" disabled={!!dlBusy}
                        onClick={() => void downloadTemp(display.logIndex, 'log')}>
                  {dlBusy === 'log' ? t('hcm.downloading') : t('hcm.downloadLog')}
                </button>
              )}
              {errorDetailUrl && (
                <a
                  className="btn btn-sm hcm-link-btn hcm-api-errlink"
                  href={errorDetailUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {t('hcm.apiErrorDetail')}
                </a>
              )}
              <button className="btn btn-sm" onClick={() => void copyResult()}>
                {t('hcm.apiCopy')}
              </button>
            </div>
            {view === 'tree' ? (
              <JsonTree data={safeJsonParse(display.text)} search={respSearch} />
            ) : view === 'code' ? (
              <CodeView
                text={display.truncated ? display.text.slice(0, PREVIEW_LIMIT) + '\n…' : display.text}
                search={respSearch}
              />
            ) : (
              <pre className="hcm-json hcm-api-json">
                {display.truncated ? display.text.slice(0, PREVIEW_LIMIT) + '\n…' : display.text}
                {display.truncated && (
                  <span className="hcm-api-truncated">
                    {'\n' + t('hcm.apiTruncated', { n: PREVIEW_LIMIT })}
                  </span>
                )}
              </pre>
            )}
            {display.isError && display.errmsg && (
              <div className="hcm-api-errmsg">{display.errmsg}</div>
            )}
            {display.isError && display.traceback && (
              <div className="hcm-api-traceback">
                <span className="hcm-api-traceback-tag">{t('hcm.apiTraceback')}</span>
                {display.traceback}
              </div>
            )}
            {/* SQL 日志（sql_debug=1 时网关在响应里带回 sql_trace） */}
            {display.sqlTrace && display.sqlTrace.length > 0 && (
              <details className="hcm-api-sqltrace">
                <summary>
                  {t('hcm.apiSqlTrace', { n: display.sqlTrace.length })}
                </summary>
                <div className="hcm-api-sqltrace-body">
                  {display.sqlTrace.map((s, i) => {
                    const m = /^\[(\d+(?:\.\d+)?)\]\s*([\s\S]*)$/.exec(s);
                    return (
                      <div key={i} className="hcm-api-sqltrace-item">
                        {m ? (
                          <>
                            <span className="hcm-api-sqltrace-ms" title={t('hcm.apiSqlTraceMs')}>
                              [{m[1]}ms]
                            </span>
                            <span className="hcm-api-sqltrace-sql">{m[2]}</span>
                          </>
                        ) : (
                          <span className="hcm-api-sqltrace-sql">{s}</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </details>
            )}
          </div>
        );
      })()}
    </div>
  );
}

/** 结果文本 → 树数据；解析失败时退回原文包装，保证树视图不白屏。 */
function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { _raw: text };
  }
}
