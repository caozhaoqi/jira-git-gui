import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { hcmDirect } from '../../api/hcm/direct';
import { hcmEnvs, HcmApiError, type HcmEnv } from '../../api/hcm/client';
import { reportLocalApiNetworkError } from '../../api/client';
import { saveBlob } from '../../utils/download';
import { useT } from '../../i18n';

/**
 * HCM 接口直调面板 —— 设计与功能对齐网关自带调试页
 * （hcm-core templates/api/api_ui.html + api_list.html + static/api/api.js）：
 *
 *   标题（描述：接口名）→ API 文档 → 输入（JSON）→ 提交行（提交 + SQL/Profile 勾选）
 *   → 结果（JSON + 耗时 + 下载接口日志/性能日志 + 查看明细错误 + 调用堆栈）
 *
 * 接口列表：调平台 Open API `hcm.api.list`（core/service/services.py，返回
 * {list:[{name,description}], count}，支持 filter_str + 分页）拉全量，输入框即
 * 可搜索下拉（名称/描述本地过滤，↑↓ 选择），按网关缓存到 localStorage 24h；
 * 拉取失败时回退到内置常用接口。
 *
 * 差异说明：
 * - 请求统一走后端 /api/hcm/direct（加解密+签名+token 自动续期），前端零密钥；
 * - 网关调试页的「WS」按钮依赖网关 WebSocket 通道，本地后端无 WS 代理，不提供。
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
  /** 网关业务错误：结果块红色 + traceback + 明细错误链接 */
  isError: boolean;
  errorCode?: string;
  traceback?: string;
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
        durationMs: typeof meta?.duration_ms === 'number' ? meta.duration_ms : undefined,
        isError: false,
      });
    } catch (e: any) {
      if (e instanceof HcmApiError && e.payload) {
        // 网关业务错误：对齐网关调试页 error 分支——红色结果块 + traceback + 明细错误 + 日志下载
        const p = e.payload;
        const rawText = p.raw !== undefined ? JSON.stringify(p.raw, null, 2) : '';
        setResult({
          text: rawText || p.errmsg || e.message,
          truncated: rawText.length > PREVIEW_LIMIT,
          gateway: target,
          logIndex: String(p.log_index ?? ''),
          profileIndex: String(p.profile_index ?? ''),
          isError: true,
          errorCode: p.error_code != null && p.error_code !== ''
            ? String(p.error_code)
            : (p.errcode != null ? String(p.errcode) : undefined),
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
          {busy && <span className="hcm-api-busy" aria-label="loading" />}
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
      </div>

      {/* ---- 结果区 ---- */}
      {result && (
        <div className={`hcm-api-resp${result.isError ? ' hcm-api-resp--error' : ''}`}>
          <div className="hcm-api-resp-head">
            <span className="hcm-api-label">{t('hcm.apiResponse')}</span>
            {typeof result.durationMs === 'number' && (
              <span
                className={`hcm-api-duration${result.isError ? ' hcm-api-duration--err' : ''}`}
                title={`srv_end - srv_begin = ${result.durationMs}ms`}
              >
                {(result.durationMs / 1000).toFixed(2)}s
              </span>
            )}
            {result.gateway && (
              <code className="hcm-api-gw" title={t('hcm.apiGatewayHint')}>
                {t('hcm.apiViaGw', { target: result.gateway })}
              </code>
            )}
            <span className="spacer" />
            {result.profileIndex && (
              <button className="btn btn-sm hcm-link-btn" disabled={!!dlBusy}
                      onClick={() => void downloadTemp(result.profileIndex, 'profile')}>
                {dlBusy === 'profile' ? t('hcm.downloading') : t('hcm.downloadProfile')}
              </button>
            )}
            {result.logIndex && (
              <button className="btn btn-sm hcm-link-btn" disabled={!!dlBusy}
                      onClick={() => void downloadTemp(result.logIndex, 'log')}>
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
          <pre className="hcm-json hcm-api-json">
            {result.truncated ? result.text.slice(0, PREVIEW_LIMIT) + '\n…' : result.text}
            {result.truncated && (
              <span className="hcm-api-truncated">
                {'\n' + t('hcm.apiTruncated', { n: PREVIEW_LIMIT })}
              </span>
            )}
          </pre>
          {result.isError && result.traceback && (
            <div className="hcm-api-traceback">
              <span className="hcm-api-traceback-tag">{t('hcm.apiTraceback')}</span>
              {result.traceback}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
