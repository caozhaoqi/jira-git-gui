/**
 * AI 接口悬浮按钮——跟随当前功能页签，一键复制该面板的 AI 拉取接口。
 *
 * 放在 workspace 右下角（fixed），App 主窗口渲染一次即可覆盖全部功能界面：
 * 用户在任何功能页点一下，就把 `curl http://127.0.0.1:8787/api/ai/output/<tab>?format=text`
 * 复制到剪贴板，贴给 AI 助手即可让 AI 拉到该窗口的当前输出。
 */
import { useState } from 'react';
import { useAppStore, type TabKey } from '../store/useAppStore';
import { useT } from '../i18n';
import { TabIcon } from './icons';
import { copyText } from '../utils/clipboard';

export function AiApiFab({ tab }: { tab: TabKey }) {
  const addToast = useAppStore((s) => s.addToast);
  const { t } = useT();
  const [showUrl, setShowUrl] = useState(false);

  const url = `http://127.0.0.1:8787/api/ai/output/${tab}?format=text`;

  const copy = async () => {
    const curl = `curl "${url}"`;
    const ok = await copyText(curl);
    addToast(ok ? `${t('ai.copied')}: ${curl}` : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
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
