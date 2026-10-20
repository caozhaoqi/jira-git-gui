import { HcmApiError, type HcmDirectErrorInfo } from './client';
import { reportLocalApiNetworkError } from '../client';

/**
 * 后端同源代理直连 HCM 网关（与 HcmModelDetail.directCall 同形态）。
 * 请求体 { api_name, params, model, token, target? } → 成功 { ok:true, data, meta, gateway }。
 * 业务错误（网关 errcode / HTTP>=400 且响应为 JSON）→ { ok:false, data:<原始错误响应>, error, meta, gateway }，
 * 这里统一抛 HcmApiError（payload 携带 traceback / error_code / log_index 等结构化信息），
 * 对齐网关自带调试页（hcm-core templates/api/api_ui.html + api.js）的错误分支。
 *
 * 与 client.ts 的 hcmCall 区别：这里走本地后端 /api/hcm/direct（后端做加解密+签名），
 * 前端零密钥、零跨域，适合在工具内随取随用（如错误定位查对象当前数据）。
 *
 * target 可选：覆盖后端配置的 proxy_target，指向其它可达的 HCM 网关
 * （当默认 proxy_target 不可达、返回 502 时，可临时切到离线/可达部署）。
 */
const DIRECT_ENDPOINT = '/api/hcm/direct';

export interface HcmDirectResult<T = any> {
  data: T;
  gateway?: string;
  /** 网关 debug 元信息：srv_begin/srv_end/duration_ms/log_index/profile_index/sql_trace */
  meta?: Record<string, any>;
}

export async function hcmDirect<T = any>(
  token: string,
  apiName: string,
  params: Record<string, any>,
  model = '',
  target = '',
  opts: { sqlDebug?: boolean; profileDebug?: boolean } = {},
): Promise<HcmDirectResult<T>> {
  let res: Response;
  try {
    res = await fetch(DIRECT_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        api_name: apiName,
        params,
        model,
        token: token.trim(),
        target: target.trim(),
        sql_debug: !!opts.sqlDebug,
        profile_debug: !!opts.profileDebug,
      }),
    });
  } catch (e) {
    // 本地后端不可达：点亮全局网络横幅（HTTP 4xx/5xx 不算，走下方正常错误分支）
    reportLocalApiNetworkError(DIRECT_ENDPOINT, e);
    throw e;
  }
  const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
  if (!res.ok) {
    const detail =
      typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body);
    throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
  }
  // 后端把网关业务错误结构化为 { ok:false, data:<原始响应>, error, meta }（HTTP 200）
  if (body && body.ok === false) {
    const err = (body.error || {}) as HcmDirectErrorInfo;
    const meta = (body.meta || {}) as Record<string, any>;
    throw new HcmApiError(err.errmsg || `HCM 网关错误 ${err.errcode ?? ''}`, 200, undefined, {
      ...err,
      // 错误响应里同样可能带接口日志/性能日志索引、SQL 日志与耗时（参考网关调试页错误分支）
      log_index: meta.log_index,
      profile_index: meta.profile_index,
      sql_trace: meta.sql_trace,
      duration_ms: meta.duration_ms,
      raw: body.data,
    });
  }
  return {
    data: (body?.data ?? body) as T,
    gateway: body?.gateway,
    meta: body?.meta,
  };
}
