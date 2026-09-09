// 内置浏览器（Electron 内嵌 Chromium 窗口）打开外部 HCM Cloud 网页的通用入口。
// 与 openExternal（跳系统浏览器）互补：内置浏览器留在应用内，且可注入 HCM token cookie 自动登录。
import { apiPost } from '../api/client';
import { useAppStore } from '../store/useAppStore';

export interface BuiltinCookie {
  name: string;
  value: string;
  url?: string; // 缺省用目标 URL 的 origin
}

/**
 * 在 Electron 内置浏览器窗口打开 url；不可用时（非 Electron / 调用失败）回退到系统浏览器 window.open。
 * @returns true 表示已在内置浏览器打开；false 表示走回退路径。
 */
export async function openBuiltinBrowser(url: string, cookies?: BuiltinCookie[]): Promise<boolean> {
  const api = (window as any).electronAPI;
  if (api && typeof api.openBuiltinBrowser === 'function') {
    try {
      const r = await api.openBuiltinBrowser(url, cookies || []);
      if (r && r.ok) return true;
    } catch {
      /* 落到下方回退 */
    }
  }
  // 回退：系统默认浏览器新窗口
  window.open(url, '_blank', 'noopener,noreferrer');
  return false;
}

/**
 * 为 HCM Cloud 网页构造自动登录 cookie。HCM 网页会话 cookie 名为 `token`（见 hcm-core
 * core/manage/auth/handler_other.py 的 set_secure_cookie('token', ...)），故把当前 HCM token
 * 以同名 cookie 注入目标 host 即可免登录。token 与网关绑定，仅当 url 的 host 与 token 所属网关一致才生效。
 */
export function hcmBuiltinCookies(url: string, token?: string): BuiltinCookie[] {
  if (!token || !token.trim()) return [];
  try {
    const origin = new URL(url).origin;
    return [{ name: 'token', value: token.trim(), url: origin }];
  } catch {
    return [];
  }
}

/**
 * 解析「与目标 HCM/CF 网关绑定」的 token 并构造自动登录 cookie。
 *
 * 关键点：注入的 token 必须与打开的页面 host 同网关，否则页面仍会停在登录页
 * （表现为「cookie 没带进去」）。优先级：
 *   1. 调用方显式传入的网关 token（如 CfPanel 里该账号登录得到的 cfg.token）；
 *   2. 后端按 server 现刷新（/api/cf/refresh-token，用 cf_accounts 该网关已存账密，
 *      返回与该网关绑定的新 token，自动处理 2h 过期）；
 *   3. 兜底：全局 store 的 hcmToken（仅当前两者都拿不到时，可能跨网关，不保证生效）。
 *
 * @param url 要打开的页面 URL（其 host 即网关）
 * @param opts.token 调用方已持有的、与 url 同网关的 token（优先使用）
 * @param opts.server 网关 server_url（用于按需刷新取新 token）
 * @param opts.proxy 代理地址（刷新时透传）
 */
export async function hcmCookiesForTarget(
  url: string,
  opts: { token?: string; server?: string; proxy?: string } = {},
): Promise<BuiltinCookie[]> {
  let token = (opts.token || '').trim();
  if (!token && opts.server) {
    try {
      const r = await apiPost<{ ok?: boolean; token?: string; cookie?: string; error?: string }>(
        '/api/cf/refresh-token',
        { server_url: opts.server, proxy: opts.proxy || '' },
      );
      token = (r?.token || r?.cookie || '').trim();
    } catch {
      token = '';
    }
  }
  if (!token) token = (useAppStore.getState().hcmToken || '').trim();
  return hcmBuiltinCookies(url, token);
}
