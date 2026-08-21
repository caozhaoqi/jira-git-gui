// 统一 API 客户端，复刻遗留 web/js/01-core.js 的 api()/apiPost()/apiDelete() 行为：
// - 同源：使用 location.origin（Electron / Web / Tauri 均同源）
// - 仅消费一次 body（先 text 再 parse）
// - 错误分类：timeout / network / auth / server / business

export type ApiErrorType =
  | 'timeout'
  | 'network'
  | 'auth'
  | 'server'
  | 'business'
  | 'unknown';

export class ApiError extends Error {
  type: ApiErrorType;
  status?: number;
  constructor(message: string, type: ApiErrorType, status?: number) {
    super(message);
    this.name = 'ApiError';
    this.type = type;
    this.status = status;
  }
}

const API = (() => {
  // 与遗留逻辑一致：统一用当前源（前后端同源）。
  if (typeof location !== 'undefined') return location.origin;
  return '';
})();

function extractDetail(data: any): string {
  if (!data) return '';
  if (typeof data.detail === 'string') return data.detail;
  if (data.detail && typeof data.detail === 'object')
    return JSON.stringify(data.detail);
  if (typeof data.message === 'string') return data.message;
  if (typeof data.msg === 'string') return data.msg;
  if (typeof data.errmsg === 'string') return data.errmsg;
  if (typeof data.error === 'string') return data.error;
  if (typeof data._raw === 'string') return data._raw.slice(0, 4000);
  return '';
}

function isAuthError(status: number, detail: string): boolean {
  if (status === 401 || status === 403) return true;
  return /未登录|登录失效|登录已过期|token\s*(失效|过期|无效)|认证失败|无权访问|17003|17001|need_img_valid/i.test(
    detail
  );
}

export async function api<T = any>(
  path: string,
  opts: RequestInit = {}
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (e: any) {
    if (e && e.name === 'AbortError') {
      throw new ApiError('请求超时，请检查网络或稍后重试', 'timeout');
    }
    throw new ApiError('网络连接失败，请检查网络或服务是否运行', 'network');
  }

  let text = '';
  try {
    text = await res.text();
  } catch {
    /* ignore */
  }
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { _raw: text };
    }
  } else {
    data = {};
  }

  if (!res.ok) {
    const detail = extractDetail(data);
    if (isAuthError(res.status, detail)) {
      throw new ApiError(detail || '登录已失效，请重新登录', 'auth', res.status);
    }
    const type: ApiErrorType =
      res.status >= 500 ? 'server' : 'business';
    throw new ApiError(
      detail || `HTTP ${res.status} ${res.statusText || ''}`.trim(),
      type,
      res.status
    );
  }
  return (data ?? {}) as T;
}

export async function apiGet<T = any>(path: string): Promise<T> {
  return api<T>(path);
}

export async function apiPost<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export async function apiDelete<T = any>(path: string): Promise<T> {
  return api<T>(path, { method: 'DELETE' });
}
