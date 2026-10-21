/**
 * AI 数据桥面板——把「应用各功能窗口的输出」暴露为本地 HTTP 接口，
 * 供同机 AI 助手（Claude Code / Cursor 等）拉取。
 *
 * 面板职责：
 * 1. 开关总闸（localStorage jgg-ai-bridge）；
 * 2. 展示接口清单 + 可复制的 curl 示例；
 * 3. 实时预览各面板的输出状态（10s 轮询 /api/ai/output），点开可看最新内容。
 *
 * 数据来源：各面板数据更新时调 reportPanelOutput(tab, …) 上报（aiBridge.ts）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { EmptyState } from './EmptyState';
import { useAppStore } from '../store/useAppStore';
import { apiDelete, apiGet, apiPost } from '../api/client';
import { useT } from '../i18n';
import { TabIcon } from './icons';
import { copyText } from '../utils/clipboard';
import { aiBridgeEnabled, setAiBridgeEnabled } from '../api/aiBridge';

type PanelInfo = {
  tab: string;
  description: string;
  entries: number;
  updated_at: number | null;
  age_sec: number | null;
  latest_size: number;
  truncated: boolean;
};

// 面板 key → i18n 的 tab.* 标题（与侧栏一致）
const TAB_LABEL_KEYS: Record<string, string> = {
  repo: 'tab.repo', diff: 'tab.diff', logs: 'tab.logs', k8s: 'tab.k8s',
  kibana: 'tab.kibana', cf: 'tab.cf', cfsync: 'tab.cfsync', cfdebug: 'tab.cfdebug',
  clash: 'tab.clash', hcm: 'tab.hcm', hcmapi: 'tab.hcmapi', hcmops: 'tab.hcmops',
  diagnose: 'tab.diagnose', aibridge: 'tab.aibridge',
};

function fmtTime(ts: number | null): string {
  if (!ts) return '-';
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(2)}MB`;
}

export function AiBridgePanel() {
  const addToast = useAppStore((s) => s.addToast);
  const { t } = useT();
  const [enabled, setEnabled] = useState(aiBridgeEnabled());
  const [inboundEnabled, setInboundEnabled] = useState(false);
  const [inboundBusy, setInboundBusy] = useState(false);
  const [incoming, setIncoming] = useState<Array<{
    id: string; target: 'k8s_yaml' | 'hcm_params' | 'kibana_query';
    title: string; text: string; created_at: number; size: number;
  }>>([]);
  const [incomingError, setIncomingError] = useState('');
  const [panels, setPanels] = useState<PanelInfo[] | null>(null);
  const [preview, setPreview] = useState<{ tab: string; text: string } | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const alive = useRef(true);

  const loadPanels = useCallback(async () => {
    try {
      const r = await apiGet<{ ok: boolean; panels: PanelInfo[] }>('/api/ai/output');
      if (alive.current) setPanels(r.panels || []);
    } catch {
      if (alive.current) setPanels(null);
    }
  }, []);

  const loadIncoming = useCallback(async () => {
    try {
      const r = await apiGet<{
        enabled: boolean;
        drafts: typeof incoming;
      }>('/api/ai/input/pending');
      if (alive.current) {
        setInboundEnabled(r.enabled);
        setIncoming(r.drafts || []);
        setIncomingError('');
      }
    } catch (error) {
      if (alive.current) {
        setIncomingError(error instanceof Error ? error.message : String(error));
      }
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void loadPanels();
    void loadIncoming();
    const timer = setInterval(() => void loadPanels(), 10000);
    return () => { alive.current = false; clearInterval(timer); };
  }, [loadIncoming, loadPanels]);

  useEffect(() => {
    if (!inboundEnabled) return undefined;
    void loadIncoming();
    const timer = setInterval(() => void loadIncoming(), 1500);
    return () => clearInterval(timer);
  }, [inboundEnabled, loadIncoming]);

  const toggle = () => {
    const next = !enabled;
    setEnabled(next);
    setAiBridgeEnabled(next);
    addToast(next ? t('ai.enabled') : t('ai.disabled'), 'success');
  };

  const toggleInbound = async (next: boolean) => {
    setInboundBusy(true);
    try {
      const response = await apiPost<{ enabled: boolean }>('/api/ai/input/settings', { enabled: next });
      setInboundEnabled(response.enabled);
      if (!response.enabled) setIncoming([]);
      setIncomingError('');
      addToast(response.enabled ? t('ai.inboundEnabled') : t('ai.inboundDisabled'), 'info');
    } catch (error) {
      addToast(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setInboundBusy(false);
    }
  };

  const insertDraft = async (draft: (typeof incoming)[number]) => {
    try {
      await apiDelete(`/api/ai/input/${draft.target}/${draft.id}`);
      useAppStore.getState().deliverAiDraft(draft);
      const destination = draft.target === 'k8s_yaml' ? 'k8s'
        : draft.target === 'hcm_params' ? 'hcmapi' : 'kibana';
      useAppStore.getState().setTab(destination);
      setIncoming((items) => items.filter((item) => item.id !== draft.id));
    } catch (error) {
      addToast(error instanceof Error ? error.message : String(error), 'error');
    }
  };

  const openPreview = async (tab: string) => {
    setPreviewBusy(true);
    try {
      const r = await apiGet<{ ok: boolean; text?: string }>(
        `/api/ai/output/${tab}?format=text`);
      setPreview({ tab, text: r.text || t('ai.noText') });
    } catch (e: unknown) {
      setPreview({ tab, text: `${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setPreviewBusy(false);
    }
  };

  const curlList = `TOKEN=$(curl -fsS http://127.0.0.1:8787/api/auth/token)
curl -H "X-API-Token: $TOKEN" http://127.0.0.1:8787/api/ai/output
curl -H "X-API-Token: $TOKEN" http://127.0.0.1:8787/api/ai/output/logs?format=text
curl -H "X-API-Token: $TOKEN" http://127.0.0.1:8787/api/ai/output/cf?format=text
curl -H "X-API-Token: $TOKEN" http://127.0.0.1:8787/api/ai/output/hcmapi
curl -H "X-API-Token: $TOKEN" http://127.0.0.1:8787/api/ai/output/hcmops
curl -H "X-API-Token: $TOKEN" http://127.0.0.1:8787/api/ai/output/hcmapi/history?limit=10`;

  const curlInput = `TOKEN=$(curl -fsS http://127.0.0.1:8787/api/auth/token)
curl -X POST http://127.0.0.1:8787/api/ai/input/k8s_yaml \\
  -H "X-API-Token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"title":"AI YAML 草稿","text":"apiVersion: v1\\nkind: ConfigMap"}'
# 可用 target：k8s_yaml、hcm_params、kibana_query；仅在应用中开启“AI 输入”后接受`;

  const copyCurl = async () => {
    const ok = await copyText(curlList);
    addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
  };

  const copyInputCurl = async () => {
    const ok = await copyText(curlInput);
    addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
  };

  const dismissDraft = async (draft: (typeof incoming)[number]) => {
    try {
      await apiDelete(`/api/ai/input/${draft.target}/${draft.id}`);
      setIncoming((items) => items.filter((item) => item.id !== draft.id));
    } catch (error) {
      addToast(error instanceof Error ? error.message : String(error), 'error');
    }
  };

  return (
    <div className="ai-bridge">
      <div className="ai-bridge-head">
        <div className="ai-bridge-intro">
          <strong>{t('ai.title')}</strong>
          <span>{t('ai.desc')}</span>
        </div>
        <label className="ai-bridge-switch">
          <input type="checkbox" checked={enabled} onChange={toggle} />
          <span>{enabled ? t('ai.enabled') : t('ai.disabled')}</span>
        </label>
        <label className="ai-bridge-switch">
          <input
            type="checkbox"
            checked={inboundEnabled}
            disabled={inboundBusy}
            onChange={(event) => void toggleInbound(event.target.checked)}
          />
          <span>{inboundBusy ? t('common.loading') : inboundEnabled ? t('ai.inboundEnabled') : t('ai.inboundDisabled')}</span>
        </label>
      </div>

      <div className="ai-bridge-curl">
        <div className="ai-bridge-curl-head">
          <span>{t('ai.inboundTitle')}</span>
          <button type="button" className="btn btn-ghost btn-xs" onClick={copyInputCurl}>
            <TabIcon name="copy" size={12} /> {t('hcm.opsCopy')}
          </button>
        </div>
        <p>{t('ai.inboundDesc')}</p>
        <pre className="ai-bridge-curl-body">{curlInput}</pre>
      </div>

      {incomingError && <div className="ai-bridge-incoming-error" role="alert">{incomingError}</div>}
      {inboundEnabled && (
        <div className="ai-bridge-panels">
          <div className="ai-bridge-panels-head">{t('ai.incomingTitle')} · {incoming.length}</div>
          {incoming.length === 0
            ? <EmptyState className="ai-bridge-empty" hint={t('ai.incomingEmpty')} />
            : incoming.map((draft) => (
              <div className="ai-bridge-row ai-bridge-row--draft" key={draft.id}>
                <span className="ai-bridge-tab">{t(`ai.target.${draft.target}`)}</span>
                <span className="ai-bridge-desc" title={draft.title}>{draft.title} · {fmtSize(draft.size)}</span>
                <button type="button" className="btn btn-primary btn-sm" onClick={() => void insertDraft(draft)}>
                  {t('ai.insertDraft')}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void dismissDraft(draft)}
                >
                  {t('common.dismiss')}
                </button>
                <pre className="ai-bridge-preview-body">{draft.text.slice(0, 4000)}</pre>
              </div>
            ))}
        </div>
      )}

      <div className="ai-bridge-curl">
        <div className="ai-bridge-curl-head">
          <span>{t('ai.curlTitle')}</span>
          <button type="button" className="btn btn-ghost btn-xs" onClick={copyCurl}>
            <TabIcon name="copy" size={12} /> {t('hcm.opsCopy')}
          </button>
        </div>
        <pre className="ai-bridge-curl-body">{curlList}</pre>
      </div>

      <div className="ai-bridge-panels">
        <div className="ai-bridge-panels-head">{t('ai.panelsTitle')}</div>
        {!panels && <EmptyState className="ai-bridge-empty" hint={t('common.loading')} />}
        {panels && panels.length === 0 && (
          <EmptyState className="ai-bridge-empty" hint={t('ai.noData')} />
        )}
        {panels && panels.map((p) => (
          <div key={p.tab} className={`ai-bridge-row${p.entries > 0 ? ' has-data' : ''}`}>
            <button
              type="button"
              className="ai-bridge-tab"
              disabled={p.entries === 0 || previewBusy}
              onClick={() => void openPreview(p.tab)}
              title={p.description}
            >
              <TabIcon name={p.entries > 0 ? 'search' : 'file'} size={12} />
              {t(TAB_LABEL_KEYS[p.tab] || p.tab)}
            </button>
            <span className="ai-bridge-desc" title={p.description}>{p.description}</span>
            <span className="ai-bridge-meta">
              {p.entries > 0
                ? `${fmtTime(p.updated_at)} · ${fmtSize(p.latest_size)}${p.truncated ? ' · ✂' : ''}`
                : t('ai.noData')}
            </span>
          </div>
        ))}
      </div>

      {preview && (
        <div className="ai-bridge-preview">
          <div className="ai-bridge-preview-head">
            <span>{t('ai.previewTitle', { tab: preview.tab })}</span>
            <div className="ai-bridge-preview-actions">
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                onClick={async () => {
                  const ok = await copyText(preview.text);
                  addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
                }}
              >
                <TabIcon name="copy" size={12} /> {t('hcm.opsCopy')}
              </button>
              <button type="button" className="btn btn-ghost btn-xs" onClick={() => setPreview(null)}>
                <TabIcon name="x" size={12} /> {t('common.close')}
              </button>
            </div>
          </div>
          <pre className="ai-bridge-preview-body">{preview.text.slice(0, 20000)}</pre>
        </div>
      )}
    </div>
  );
}
