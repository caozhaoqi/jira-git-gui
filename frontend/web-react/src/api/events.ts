// SSE 事件管理器，复刻 web/js/01-core.js 的 connectSSE()：
// - 连接 /api/events
// - 分发已注册的事件名到监听器
// - 断线后自动重连（2s）
import type { SSEEventMap } from './types';

type Handler<K extends keyof SSEEventMap> = (data: SSEEventMap[K]) => void;

class SSEManager {
  private es: EventSource | null = null;
  private listeners = new Map<keyof SSEEventMap, Set<(data: any) => void>>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  connect() {
    if (this.es) return;
    const url = `${location.origin}/api/events`;
    this.es = new EventSource(url);
    this.listeners.forEach((set, evt) => {
      this.es!.addEventListener(evt, (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          set.forEach((h) => h(data));
        } catch {
          /* ignore malformed */
        }
      });
    });
    this.es.onerror = () => {
      this.es = null;
      if (!this.reconnectTimer) {
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null;
          this.connect();
        }, 2000);
      }
    };
  }

  on<K extends keyof SSEEventMap>(evt: K, handler: Handler<K>): () => void {
    let set = this.listeners.get(evt);
    if (!set) {
      set = new Set();
      this.listeners.set(evt, set);
    }
    set.add(handler as (data: any) => void);
    // 若已连接，为这个事件补挂监听（之前 connect 时该事件可能未注册）
    if (this.es) {
      this.es.addEventListener(evt, (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data);
          (handler as (data: any) => void)(data);
        } catch {
          /* ignore */
        }
      });
    }
    return () => {
      set!.delete(handler as (data: any) => void);
    };
  }

  disconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.es?.close();
    this.es = null;
  }
}

export const sse = new SSEManager();
