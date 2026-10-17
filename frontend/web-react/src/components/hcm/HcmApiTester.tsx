import { useCallback, useEffect, useMemo, useState } from 'react';
import { hcmDirect } from '../../api/hcm/direct';
import { hcmEnvs, HcmApiError, type HcmEnv } from '../../api/hcm/client';
import { reportLocalApiNetworkError } from '../../api/client';
import { saveBlob } from '../../utils/download';
import { useT } from '../../i18n';

/**
 * HCM 接口直调面板 —— 设计与功能对齐网关自带调试页
 * （hcm-core templates/api/api_ui.html + static/api/api.js）：
 *
 *   标题（描述：接口名）→ API 文档 → 输入（JSON）→ 提交行（提交 + SQL/Profile 勾选）
 *   → 结果（JSON + 耗时 + 下载接口日志/性能日志 + 查看明细错误 + 调用堆栈）
 *
 * 差异说明：
 * - 请求统一走后端 /api/hcm/direct（加解密+签名+token 自动续期），前端零密钥；
 * - 网关调试页的「WS」按钮依赖网关 WebSocket 通道，本地后端无 WS 代理，不提供；
 * - 错误响应原样展示（红色结果块），traceback / error_code / log_index 均来自网关响应，
 *   与网关调试页 error 分支行为一致。
 */

/** 常用接口候选（datalist 联想；仍可输入任意接口名）。doc 文案见 i18n hcm.apiDoc_* */
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
];

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

  // 网关候选：后端从 cf_accounts / hcm_whitelist 汇总的环境列表
  useEffect(() => {
    let alive = true;
    hcmEnvs()
      .then((list) => { if (alive) setEnvs(list); })
      .catch(() => { /* 候选拉取失败不阻塞：仍可用默认网关 */ });
    return () => { alive = false; };
  }, []);

  // 命中常用接口 → 显示「{描述}：{名称}」标题 + API 文档块（对齐网关调试页标题/文档区）
  const matchedApi = useMemo(
    () => COMMON_APIS.find((a) => a.name === apiName.trim()) || null,
    [apiName],
  );

  const formatParams = useCallback(() => {
    try {
      const obj = JSON.parse(paramsText);
      setParamsText(JSON.stringify(obj, null, 2));
      setErr('');
    } catch (e: any) {
      setErr(t('hcm.payloadParseFailed', { msg: e?.message || String(e) }));
    }
  }, [paramsText, t]);

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
      {/* ---- 标题行：接口名 + 网关 ---- */}
      <div className="hcm-api-form">
        <div className="hcm-api-row">
          <label className="hcm-api-field hcm-api-field--name">
            <span className="hcm-api-label">{t('hcm.apiNameLabel')}</span>
            <input
              className="input hcm-mono"
              value={apiName}
              onChange={(e) => setApiName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void send(); }}
              list="hcm-api-names"
              placeholder="hcm.model.update"
              spellCheck={false}
            />
            <datalist id="hcm-api-names">
              {COMMON_APIS.map((a) => <option key={a.name} value={a.name} />)}
            </datalist>
          </label>
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
        {matchedApi && (
          <>
            <div className="hcm-api-title">
              {t(`hcm.${matchedApi.docKey}`)}
              {'：'}
              <code>{matchedApi.name}</code>
            </div>
            <div className={`hcm-api-doc${matchedApi.danger ? ' hcm-api-doc--danger' : ''}`}>
              <span className="hcm-api-doc-tag">{t('hcm.apiDoc')}</span>
              {t(`hcm.${matchedApi.docKey}Body`)}
              {matchedApi.danger && (
                <span className="hcm-api-doc-warn"> ⚠ {t('hcm.apiDangerTip')}</span>
              )}
            </div>
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
