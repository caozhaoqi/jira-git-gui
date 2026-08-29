import { HcmApiError } from './client';

/**
 * 后端同源代理直连 HCM 网关（与 HcmModelDetail.directCall 同形态）。
 * 请求体 { api_name, params, model, token, target? } → 成功返回 { data, gateway }。
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
}

export async function hcmDirect<T = any>(
  token: string,
  apiName: string,
  params: Record<string, any>,
  model = '',
  target = ''
): Promise<HcmDirectResult<T>> {
  const res = await fetch(DIRECT_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_name: apiName, params, model, token: token.trim(), target: target.trim() }),
  });
  const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
  if (!res.ok) {
    const detail =
      typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
    throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
  }
  return { data: (data?.data ?? data) as T, gateway: data?.gateway };
}
