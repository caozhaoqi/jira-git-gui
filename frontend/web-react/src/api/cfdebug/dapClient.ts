// 云函数调试控制台：浏览器侧 DAP 客户端。
//
// 传输：浏览器通过 WebSocket 连接后端 /api/cf-debug/ws/{session_id}，
// 后端 dap_bridge 把 WS 文本帧(JSON) ⇄ debugpy 裸 TCP DAP(Content-Length 分帧) 互转。
// 因此本客户端直接以「一条文本帧 = 一个 JSON DAP 消息」收发，无需自己拼帧头。
//
// 握手顺序（已用裸 TCP 版 /tmp/test_dap.py 端到端验证）：
//   1. request initialize  → 等待其 response
//   2. request attach({request:"attach", connect:{host,port}})  // 不 await（debugpy 会hold响应直到 configurationDone）
//   3. 收到事件 initialized → request setBreakpoints + request configurationDone
//   4. 命中断点 → 事件 stopped → threads / stackTrace / scopes / variables
//   5. 单步/继续：next / stepIn / stepOut / continue → 再次 stopped 或 terminated
//
// 后端 debugpy 以 in-process `debugpy.listen()+wait_for_client()` 方式启动，
// 等价于 DAP「listen/server attach」模式，故客户端用 attach 而非 launch。

type DapMessage = {
  seq?: number;
  type: 'request' | 'response' | 'event';
  command?: string;
  event?: string;
  request_seq?: number;
  success?: boolean;
  message?: string;
  body?: any;
  arguments?: any;
};

type EventHandler = (body: any) => void;

export class DapClient {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>();
  private handlers = new Map<string, Set<EventHandler>>();
  private opened = false;
  private openWaiters: Array<() => void> = [];
  private onClose?: () => void;

  /** 注册 DAP 事件监听（initialized / stopped / terminated / exited / output / ...）。返回取消函数。 */
  on(event: string, cb: EventHandler): () => void {
    let set = this.handlers.get(event);
    if (!set) {
      set = new Set();
      this.handlers.set(event, set);
    }
    set.add(cb);
    return () => set!.delete(cb);
  }

  /** 连接 WebSocket（同源）。resolve 于 onopen。 */
  connect(url: string): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        const ws = new WebSocket(url);
        this.ws = ws;
        ws.onopen = () => {
          this.opened = true;
          this.openWaiters.forEach((w) => w());
          this.openWaiters = [];
          resolve();
        };
        ws.onmessage = (e: MessageEvent) => {
          try {
            const msg = JSON.parse(e.data as string) as DapMessage;
            this._dispatch(msg);
          } catch {
            /* 忽略非法帧 */
          }
        };
        ws.onerror = () => {
          if (!this.opened) reject(new Error('WebSocket 连接失败'));
        };
        ws.onclose = () => {
          this.opened = false;
          this.onClose?.();
          // 关闭时把未完成的请求全部 reject，避免悬挂 promise
          this.pending.forEach((p) => p.reject(new Error('DAP 连接已关闭')));
          this.pending.clear();
        };
      } catch (e) {
        reject(e);
      }
    });
  }

  setOnClose(cb: () => void) {
    this.onClose = cb;
  }

  private _dispatch(msg: DapMessage) {
    if (msg.type === 'response') {
      const p = this.pending.get(msg.request_seq ?? -1);
      if (p) {
        this.pending.delete(msg.request_seq ?? -1);
        if (msg.success === false) {
          p.reject(new Error(msg.message || `DAP ${msg.command} 失败`));
        } else {
          p.resolve(msg);
        }
      }
      return;
    }
    if (msg.type === 'event' && msg.event) {
      const set = this.handlers.get(msg.event);
      set?.forEach((cb) => {
        try {
          cb(msg.body);
        } catch {
          /* 单个 handler 异常不影响其他 */
        }
      });
    }
  }

  private _send(obj: DapMessage) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new Error('DAP 未连接');
    }
    this.ws.send(JSON.stringify(obj));
  }

  /** 发送一个 request 并等待其 response。 */
  request(command: string, args?: any): Promise<any> {
    this.seq += 1;
    const seq = this.seq;
    this._send({ seq, type: 'request', command, arguments: args });
    return new Promise((resolve, reject) => {
      this.pending.set(seq, { resolve, reject });
    });
  }

  // ===== 高层封装 =====

  async initialize(): Promise<any> {
    return this.request('initialize', {
      clientID: 'jgg-cfdebug',
      clientName: 'Jira Git GUI CF Debug',
      adapterID: 'debugpy',
      pathFormat: 'path',
      linesStartAt1: true,
      columnsStartAt1: true,
      supportsConfigurationDoneRequest: true,
      supportsVariableType: true,
      supportsEvaluateForHovers: true,
    });
  }

  /** attach（listen 模式）。不 await：debugpy 会在 configurationDone 后才返回该响应。 */
  attach(host: string, port: number): Promise<any> {
    return this.request('attach', { request: 'attach', connect: { host, port } });
  }

  async setBreakpoints(file: string, lines: number[]): Promise<any> {
    return this.request('setBreakpoints', {
      source: { path: file },
      breakpoints: lines.map((line) => ({ line })),
      sourceModified: false,
    });
  }

  async configurationDone(): Promise<any> {
    return this.request('configurationDone', {});
  }

  async threads(): Promise<any> {
    return this.request('threads', {});
  }

  async stackTrace(threadId: number): Promise<any> {
    return this.request('stackTrace', { threadId });
  }

  async scopes(frameId: number): Promise<any> {
    return this.request('scopes', { frameId });
  }

  async variables(variablesReference: number): Promise<any> {
    return this.request('variables', { variablesReference });
  }

  next(threadId: number): Promise<any> {
    return this.request('next', { threadId });
  }
  stepIn(threadId: number): Promise<any> {
    return this.request('stepIn', { threadId });
  }
  stepOut(threadId: number): Promise<any> {
    return this.request('stepOut', { threadId });
  }
  stepBack(threadId: number): Promise<any> {
    // debugpy / Python 不支持反向调试；调用会失败。UI 侧已将该按钮置灰。
    return this.request('stepBack', { threadId });
  }
  continue(threadId: number): Promise<any> {
    return this.request('continue', { threadId });
  }
  pause(threadId: number): Promise<any> {
    return this.request('pause', { threadId });
  }

  /** 断开并尝试关闭 WS（best effort）。 */
  disconnect(): void {
    try {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.request('disconnect', { restart: false, terminateDebuggee: true }).catch(
          () => {},
        );
        this.ws.close();
      }
    } catch {
      /* ignore */
    }
  }

  close(): void {
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = null;
  }
}
