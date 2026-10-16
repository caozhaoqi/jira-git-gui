import { useCallback, useEffect, useState } from 'react';
import { hcmDirect } from '../../api/hcm/direct';
import { hcmEnvs, HcmApiError, type HcmEnv } from '../../api/hcm/client';
import { reportLocalApiNetworkError } from '../../api/client';
import { saveBlob } from '../../utils/download';
import { useT } from '../../i18n';

/**
 * HCM 接口直调面板：输入接口名 + JSON 参数即可对网关发起任意 hcm.* 请求，
 * 等价于网关自带的「API 文档 → 输入 → 提交」调试页（截图参照 10.22.78.121/api/hcm.model.update）。
 *
 * - 请求统一走后端 /api/hcm/direct（加解密+签名+token 自动续期），前端零密钥；
 * - token 留空 = 后端预设 token（失效时按 cf_accounts 自动重登一次）；
 * - 「输出 SQL/Profile 日志」对应网关的 sql_debug / profile_debug 查询参数；
 * - 网关在 debug 模式下会在响应里带 log_index / profile_index，提供一键下载。
 */

/** 常用接口候选（datalist 联想；仍可输入任意接口名） */
const COMMON_APIS = [
  'hcm.model.list',
  'hcm.model.info',
  'hcm.model.view',
  'hcm.model.count',
  'hcm.model.update',
  'hcm.model.add',
  'hcm.model.delete',
  'hcm.model.edit',
  'hcm.model.edit.batch',
  'hcm.paas.object.list',
];

/** 响应预览截断长度（超出仍可复制完整文本） */
const PREVIEW_LIMIT = 200_000;

interface SendResult {
  text: string;
  truncated: boolean;
  gateway: string;
  logIndex: string;
  profileIndex: string;
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
      const { data, gateway } = await hcmDirect('', apiName.trim(), params, '', target, {
        sqlDebug,
        profileDebug,
      });
      const text = JSON.stringify(data, null, 2);
      const d = (data && typeof data === 'object' && !Array.isArray(data) ? data : {}) as Record<string, any>;
      setResult({
        text,
        truncated: text.length > PREVIEW_LIMIT,
        gateway: gateway || '',
        logIndex: String(d.log_index ?? ''),
        profileIndex: String(d.profile_index ?? ''),
      });
    } catch (e: any) {
      const msg = e instanceof HcmApiError ? e.message : (e?.message || String(e));
      setErr(msg);
      setResult(null);
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

  return (
    <div className="hcm-api">
      {/* ---- 请求构造区 ---- */}
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
              {COMMON_APIS.map((a) => <option key={a} value={a} />)}
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

        <div className="hcm-api-row">
          <label className="chk hcm-api-chk" title={t('hcm.apiSqlDebugHint')}>
            <input type="checkbox" checked={sqlDebug} onChange={(e) => setSqlDebug(e.target.checked)} />
            {t('hcm.apiSqlDebug')}
          </label>
          <label className="chk hcm-api-chk" title={t('hcm.apiProfileDebugHint')}>
            <input type="checkbox" checked={profileDebug} onChange={(e) => setProfileDebug(e.target.checked)} />
            {t('hcm.apiProfileDebug')}
          </label>
          <span className="spacer" />
          <span className="hcm-api-token-hint">{t('hcm.apiTokenHint')}</span>
          <button className="btn btn-primary btn-sm" onClick={() => void send()} disabled={busy}>
            {busy ? t('hcm.loading') : t('hcm.apiSend')}
          </button>
        </div>

        {err && <div className="hcm-api-err">{err}</div>}
      </div>

      {/* ---- 响应区 ---- */}
      {result && (
        <div className="hcm-api-resp">
          <div className="hcm-api-resp-head">
            <span className="hcm-api-label">{t('hcm.apiResponse')}</span>
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
        </div>
      )}
    </div>
  );
}
