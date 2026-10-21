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
const BUCKET_RATE = 6;
const BUCKET_CAPACITY = 4;

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
  if (!on) {
    for (const item of pending.values()) {
      if (item.timer) clearTimeout(item.timer);
    }
    pending.clear();
  }
}

const pending = new Map<string, { payload: Payload; timer: ReturnType<typeof setTimeout> | null }>();
let tokens = BUCKET_CAPACITY;
let lastRefill = Date.now();

type Payload = { text?: string; data?: unknown; meta?: Record<string, unknown> };

function takeToken(): boolean {
  const now = Date.now();
  tokens = Math.min(BUCKET_CAPACITY, tokens + ((now - lastRefill) / 1000) * BUCKET_RATE);
  lastRefill = now;
  if (tokens < 1) return false;
  tokens -= 1;
  return true;
}

function schedule(tab: TabKey): void {
  const item = pending.get(tab);
  if (!item || item.timer) return;
  const elapsed = (Date.now() - lastRefill) / 1000;
  const available = Math.min(BUCKET_CAPACITY, tokens + elapsed * BUCKET_RATE);
  const delay = Math.max(THROTTLE_MS, Math.ceil(Math.max(0, 1 - available) / BUCKET_RATE * 1000));
  item.timer = setTimeout(() => {
    const current = pending.get(tab);
    if (!current) return;
    current.timer = null;
    if (!aiBridgeEnabled()) {
      pending.delete(tab);
      return;
    }
    if (!takeToken()) {
      schedule(tab);
      return;
    }
    pending.delete(tab);
    const payload = current.payload;
    const body = JSON.stringify({
      text: payload.text ?? '',
      data: payload.data ?? null,
      meta: { ...(payload.meta || {}), reported_at: new Date().toISOString() },
    });
    fetch(`/api/ai/output/${tab}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    }).catch(() => { /* 静默：AI 桥失败不影响面板 */ });
  }, delay);
}

/**
 * 面板数据更新时调用。text 传「给 AI 看的文本」（通常是序列化后的展示数据），
 * data 传结构化原始数据（可选）。同一 tab 高频调用自动合并到最后一帧。
 */
export function reportPanelOutput(tab: TabKey, payload: Payload): void {
  if (!aiBridgeEnabled()) return;
  const existing = pending.get(tab);
  if (existing) {
    existing.payload = payload;
  } else {
    pending.set(tab, { payload, timer: null });
  }
  schedule(tab);
}
