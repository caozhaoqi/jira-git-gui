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
import { useAppStore } from '../store/useAppStore';
import { apiGet } from '../api/client';
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

  useEffect(() => {
    alive.current = true;
    void loadPanels();
    const timer = setInterval(() => void loadPanels(), 10000);
    return () => { alive.current = false; clearInterval(timer); };
  }, [loadPanels]);

  const toggle = () => {
    const next = !enabled;
    setEnabled(next);
    setAiBridgeEnabled(next);
    addToast(next ? t('ai.enabled') : t('ai.disabled'), 'success');
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

  const curlList = `curl http://127.0.0.1:8787/api/ai/output                       # 全部面板概览
curl http://127.0.0.1:8787/api/ai/output/logs?format=text            # 应用日志
curl http://127.0.0.1:8787/api/ai/output/cf?format=text              # 云函数日志
curl http://127.0.0.1:8787/api/ai/output/hcmapi                      # HCM 接口调试最新请求/响应
curl http://127.0.0.1:8787/api/ai/output/hcmops                      # HCM 运维(Redis/错误/解密)
curl http://127.0.0.1:8787/api/ai/output/hcmapi/history?limit=10     # 历史记录`;

  const copyCurl = async () => {
    const ok = await copyText(curlList);
    addToast(ok ? t('hcm.copied') : t('hcm.opsCopyFail'), ok ? 'success' : 'error');
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
      </div>

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
        {!panels && <div className="ai-bridge-empty">{t('common.loading')}</div>}
        {panels && panels.length === 0 && (
          <div className="ai-bridge-empty">{t('ai.noData')}</div>
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
