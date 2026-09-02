// SSE 事件管理器，复刻 web/js/01-core.js 的 connectSSE()：
// - 连接 /api/events
// - 分发已注册的事件名到监听器
// - 断线后自动重连（2s）
import type { SSEEventMap } from './types';

type Handler<K extends keyof SSEEventMap> = (data: SSEEventMap[K]) => void;

class SSEManager {
  private es: EventSource | null = null;
  private listeners = new Map<keyof SSEEventMap, Set<(data: any) => void>>();
  /** 已经在底层 EventSource 上挂过监听的事件名，避免重复挂导致 handler 被调多次 */
  private esBound = new Set<keyof SSEEventMap>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  connect() {
    if (this.es) return;
    const url = `${location.origin}/api/events`;
    this.es = new EventSource(url);
    // 为每个已注册的事件名挂一个「只负责从 Set 分发」的监听（幂等）
    this.listeners.forEach((_set, evt) => this.bindEs(evt));
    this.es.onerror = () => {
      this.closeEs();
      if (!this.reconnectTimer) {
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null;
          this.connect();
        }, 2000);
      }
    };
  }

  /** 给某个事件名在底层 EventSource 上挂一个转发监听（同一事件名只挂一次） */
  private bindEs(evt: keyof SSEEventMap) {
    if (this.esBound.has(evt) || !this.es) return;
    this.esBound.add(evt);
    this.es.addEventListener(evt, (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data);
        this.listeners.get(evt)?.forEach((h) => h(data));
      } catch {
        /* ignore malformed */
      }
    });
  }

  on<K extends keyof SSEEventMap>(evt: K, handler: Handler<K>): () => void {
    let set = this.listeners.get(evt);
    if (!set) {
      set = new Set();
      this.listeners.set(evt, set);
    }
    set.add(handler as (data: any) => void);
    // 确保底层已为该事件名挂监听（幂等：已挂过则跳过，不会重复触发）
    this.bindEs(evt);
    return () => {
      set!.delete(handler as (data: any) => void);
    };
  }

  private closeEs() {
    this.es?.close();
    this.es = null;
    this.esBound.clear();
  }

  disconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.closeEs();
  }
}

export const sse = new SSEManager();
