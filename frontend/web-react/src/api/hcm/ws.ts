import { reportLocalApiNetworkError } from '../client';

/**
 * HCM 网关 WS 调试通道（对齐平台调试页「WS」按钮）。
 *
 * 协议（现网实测确认）：网关 `ws(s)://<host>/ws/<api>`，握手带 Cookie token，
 * 首帧发**明文业务参数**（无 REST 的加密信封），回包 `{status, data}`：
 * notify = 进度（可多条），success / error = 终态。只有声明 notify 的
 * handler 支持 WS（长任务类接口），否则回
 * "xxx() got an unexpected keyword argument 'notify'"。
 *
 * 本文件封装后端两个入口：
 * - `hcmWsCall`：一次性调用（POST /api/hcm/ws-call），收齐终态即返回；
 * - `hcmWsProxyUrl`：流式代理地址（WS /api/hcm/ws），浏览器直连本地后端，
 *   后端双向桥接网关，notify 进度实时推送（调试页同款体验）。
 */
const WS_CALL_ENDPOINT = '/api/hcm/ws-call';

export interface HcmWsCallResult {
  ok: boolean;
  status?: string;
  data?: any;
  /** notify 进度消息列表（pong 心跳已由后端过滤） */
  messages?: any[];
  gateway?: string;
  duration_ms?: number;
  error?: string;
}

/** 一次性 WS 调试调用（连接 → 发参数 → 收包至终态 → 关闭）。 */
export async function hcmWsCall(
  apiName: string,
  params: Record<string, any>,
  target = '',
  token = '',
  recvTimeout = 300,
): Promise<HcmWsCallResult> {
  let res: Response;
  try {
    res = await fetch(WS_CALL_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        api_name: apiName,
        params,
        target: target.trim(),
        token: token.trim(),
        recv_timeout: recvTimeout,
      }),
    });
  } catch (e) {
    reportLocalApiNetworkError(WS_CALL_ENDPOINT, e);
    throw e;
  }
  const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
  if (!res.ok) {
    const detail =
      typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body);
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return body as HcmWsCallResult;
}

/** 流式 WS 代理地址（浏览器 WebSocket 直连本地后端）。 */
export function hcmWsProxyUrl(apiName: string, target = '', token = ''): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const q = new URLSearchParams({ api: apiName.trim() });
  if (target.trim()) q.set('target', target.trim());
  if (token.trim()) q.set('token', token.trim());
  return `${proto}//${location.host}/api/hcm/ws?${q.toString()}`;
}
