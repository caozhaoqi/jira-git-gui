// 统一 API 客户端，复刻遗留 web/js/01-core.js 的 api()/apiPost() 行为：
// - 同源：使用 location.origin（Electron / Web / Tauri 均同源）
// - 仅消费一次 body（先 text 再 parse）
// - 错误分类：timeout / network / auth / server / business
//
// 错误文案走 i18n 的纯函数 t()（非组件模块专用）：此前这里是 7 条硬编码中文，
// 所有语言下都显示中文。循环依赖说明：i18n → useAppStore → client → i18n，
// 但 client 只在请求失败（运行时）才调用 t()，且 t 是函数声明（提升 + live
// binding），模块加载顺序不会出问题。
import { t } from '../i18n';

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
      throw new ApiError(t('api.errTimeout'), 'timeout');
    }
    throw new ApiError(t('api.errNetwork'), 'network');
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
      throw new ApiError(detail || t('api.errAuth'), 'auth', res.status);
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

/**
 * 拉取纯文本响应（如 /api/k8s/log 返回 text/plain）。
 * 不走 JSON.parse，避免日志内容恰好是合法 JSON 时被误解析；
 * 后端以 JSON 返回错误时（{"error": "..."}）仍会识别并抛 ApiError。
 */
export async function apiText(path: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, { cache: 'no-store' });
  } catch (e: any) {
    if (e && e.name === 'AbortError') {
      throw new ApiError(t('api.errTimeout'), 'timeout');
    }
    throw new ApiError(t('api.errNetwork'), 'network');
  }
  const text = await res.text().catch(() => '');
  if (!res.ok) {
    let detail = '';
    try {
      detail = extractDetail(JSON.parse(text));
    } catch {
      detail = text.slice(0, 400);
    }
    if (isAuthError(res.status, detail)) {
      throw new ApiError(detail || t('api.errAuth'), 'auth', res.status);
    }
    throw new ApiError(
      detail || `HTTP ${res.status} ${res.statusText || ''}`.trim(),
      res.status >= 500 ? 'server' : 'business',
      res.status
    );
  }
  // 后端偶发以 JSON 报错但仍返回 200 的情况（{"ok":false,"error":...}）
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    try {
      const j = JSON.parse(text);
      if (j && j.ok === false) {
        throw new ApiError(j.error || t('api.errFetch'), 'business', 200);
      }
    } catch (e) {
      if (e instanceof ApiError) throw e;
    }
  }
  return text;
}

/**
 * POST JSON。`opts` 可传额外 RequestInit（如 `signal` 用于取消在途请求），
 * 不能覆盖 method / body。
 */
export async function apiPost<T = any>(
  path: string,
  body: unknown,
  opts: Omit<RequestInit, 'method' | 'body'> = {}
): Promise<T> {
  return api<T>(path, { ...opts, method: 'POST', body: JSON.stringify(body) });
}
