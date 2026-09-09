// 内置浏览器（Electron 内嵌 Chromium 窗口）打开外部 HCM Cloud 网页的通用入口。
// 与 openExternal（跳系统浏览器）互补：内置浏览器留在应用内，且可注入 HCM token cookie 自动登录。

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
