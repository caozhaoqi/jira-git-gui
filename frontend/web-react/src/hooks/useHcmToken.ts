import { useCallback, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { isTokenLikelyExpired } from '../api/hcm/errDict';

export interface RefreshResult {
  ok: boolean;
  token?: string;
  error?: string;
}

/**
 * HCM token 的全局访问钩子：
 * - token / setToken 来自 store（全局唯一来源 + localStorage 持久化）
 * - isExpired 依据 HCM_TOKEN_TTL_HOURS 判断（默认 2h）
 * - refresh(serverUrl?) 调 /api/cf/refresh-token，用 cf_accounts 已存账密重登取新 token；
 *   成功后 store 会自动更新并持久化，所有面板即时同步。
 *
 * 面板用法（避免重复登录）：发请求前若 isExpired 且已知 serverUrl，
 * 先 `const r = await refresh(targetUrl)`；r.ok 时拿 r.token 继续，否则提示手动重填。
 */
export function useHcmToken(serverUrl?: string) {
  const token = useAppStore((s) => s.hcmToken);
  const setToken = useAppStore((s) => s.setHcmToken);
  const refreshHcmToken = useAppStore((s) => s.refreshHcmToken);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(
    async (url?: string): Promise<RefreshResult> => {
      const su = (url ?? serverUrl ?? '').trim();
      if (!su) return { ok: false, error: '缺少 server_url，无法自动重登' };
      setRefreshing(true);
      try {
        return await refreshHcmToken(su, '');
      } finally {
        setRefreshing(false);
      }
    },
    [refreshHcmToken, serverUrl],
  );

  return {
    token,
    setToken,
    refresh,
    refreshing,
    isExpired: isTokenLikelyExpired(token),
  };
}
