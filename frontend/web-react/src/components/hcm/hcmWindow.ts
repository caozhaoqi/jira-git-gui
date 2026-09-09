import { useAppStore } from '../../store/useAppStore';

/**
 * 构建 HCM「独立轻窗口」URL，并自动携带当前 token（从 store 读取），
 * 避免新窗口因跨窗口 localStorage 不可靠（window.open 后新窗口还是 about:blank，
 * 直接写 w.localStorage 会抛 SecurityError 被吞掉）而读不到 token 导致打开报错。
 *
 * 网关（target）由各面板通过 extra 传入（网关选择依赖具体面板，store 未存）。
 *
 * 用法：
 *   buildHcmUrl('/web/?hcm-detail=1&hcm-model=' + encId, { 'hcm-target': targetUrl })
 *   openHcmWindow('/web/?hcm-cf-err=1', '_blank', 'width=1100,height=900', { 'hcm-target': targetUrl })
 */
export function buildHcmUrl(basePath: string, extra?: Record<string, string>): string {
  const [base, qs] = basePath.split('?');
  const params = new URLSearchParams(qs || '');
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v != null && v !== '') params.set(k, v);
    }
  }
  const tk = (useAppStore.getState().hcmToken || '').trim();
  if (tk) params.set('hcm-token', tk);
  return `${base}?${params.toString()}`;
}

export function openHcmWindow(
  basePath: string,
  name: string,
  features = '',
  extra?: Record<string, string>,
): Window | null {
  const url = buildHcmUrl(basePath, extra);
  const w = window.open(url, name, features);
  if (w) w.focus();
  return w;
}
