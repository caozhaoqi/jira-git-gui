/**
 * AI 数据桥客户端——把面板输出上报给本地后端 /api/ai/output/{tab}，
 * 供同机的 AI 助手（Claude Code / Cursor 等）通过 HTTP 拉取。
 *
 * 设计约束：
 * - fire-and-forget：上报失败完全静默（AI 桥是附加能力，不能影响面板本身）；
 * - 开关存 localStorage（jgg-ai-bridge，默认开）；
 * - 每 tab 节流 800ms：面板数据高频刷新（如日志流）时只上报最后一帧。
 */
import type { TabKey } from '../store/useAppStore';

const ENABLE_KEY = 'jgg-ai-bridge';
const THROTTLE_MS = 800;

export function aiBridgeEnabled(): boolean {
  try {
    return localStorage.getItem(ENABLE_KEY) !== '0';
  } catch {
    return true;
  }
}

export function setAiBridgeEnabled(on: boolean): void {
  try {
    localStorage.setItem(ENABLE_KEY, on ? '1' : '0');
  } catch { /* ignore */ }
}

const lastSent = new Map<string, number>();
const pending = new Map<string, ReturnType<typeof schedule>>();

type Payload = { text?: string; data?: unknown; meta?: Record<string, unknown> };

function schedule(fn: () => void) {
  return { fn, timer: setTimeout(fn, THROTTLE_MS) };
}

/**
 * 面板数据更新时调用。text 传「给 AI 看的文本」（通常是序列化后的展示数据），
 * data 传结构化原始数据（可选）。同一 tab 高频调用自动合并到最后一帧。
 */
export function reportPanelOutput(tab: TabKey, payload: Payload): void {
  if (!aiBridgeEnabled()) return;
  pending.set(tab, schedule(() => {
    pending.delete(tab);
    const now = Date.now();
    if (now - (lastSent.get(tab) || 0) < THROTTLE_MS) return;
    lastSent.set(tab, now);
    const body = JSON.stringify({
      text: payload.text ?? '',
      data: (payload.data ?? null) as Record<string, unknown> | null,
      meta: { ...(payload.meta || {}), reported_at: new Date().toISOString() },
    });
    fetch(`/api/ai/output/${tab}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    }).catch(() => { /* 静默：AI 桥失败不影响面板 */ });
  }));
}
