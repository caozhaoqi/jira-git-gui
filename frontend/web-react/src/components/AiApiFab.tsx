/**
 * AI 接口悬浮按钮——跟随当前功能页签，一键复制该面板的 AI 拉取接口。
 *
 * 放在 workspace 右下角（fixed），App 主窗口渲染一次即可覆盖全部功能界面：
 * 用户在任何功能页点一下，就把
 *   curl -H "X-API-Token: <本机令牌>" http://127.0.0.1:8787/api/ai/output/<tab>?format=text
 * 复制到剪贴板，贴给 AI 助手即可让 AI 拉到该窗口的当前输出。
 *
 * 为什么带 token：后端 /api/* 对非「同源前端」的调用要求 X-API-Token（见
 * api/auth.py 的 ApiTokenMiddleware）。AI 助手（Claude Code 等）是独立的 curl
 * 客户端，没有 Origin 头，不带 token 会 401。令牌从同源 /api/auth/token 取
 * （该接口仅限本机同源访问，中间件放行前端）。
 */
import { useState } from 'react';
import { useAppStore, type TabKey } from '../store/useAppStore';
import { useT } from '../i18n';
import { TabIcon } from './icons';
import { copyText } from '../utils/clipboard';

/** 取本机 API 令牌（同源 GET，纯文本）；失败返回空串，由调用方退回裸 curl。 */
async function fetchLocalToken(): Promise<string> {
  try {
    const resp = await fetch('/api/auth/token', { cache: 'no-store' });
    if (!resp.ok) return '';
    return (await resp.text()).trim();
  } catch {
    return '';
  }
}

export function AiApiFab({ tab }: { tab: TabKey }) {
  const addToast = useAppStore((s) => s.addToast);
  const { t } = useT();
  const [showUrl, setShowUrl] = useState(false);

  const url = `http://127.0.0.1:8787/api/ai/output/${tab}?format=text`;

  const copy = async () => {
    const token = await fetchLocalToken();
    const curl = token
      ? `curl -H "X-API-Token: ${token}" "${url}"`
      : `curl "${url}"`;
    const ok = await copyText(curl);
    addToast(
      ok ? t(token ? 'ai.copiedWithToken' : 'ai.copied') : t('hcm.opsCopyFail'),
      ok ? 'success' : 'error',
    );
  };

  return (
    <div
      className="ai-api-fab"
      onMouseEnter={() => setShowUrl(true)}
      onMouseLeave={() => setShowUrl(false)}
    >
      {showUrl && <span className="ai-api-fab-url" aria-hidden="true">{url}</span>}
      <button
        type="button"
        className="ai-api-fab-btn"
        onClick={() => void copy()}
        title={t('ai.fabTitle')}
      >
        <TabIcon name="bridge" size={13} />
        <span>{t('ai.fabLabel')}</span>
      </button>
    </div>
  );
}
