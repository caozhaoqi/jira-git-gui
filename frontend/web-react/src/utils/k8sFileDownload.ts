// 容器内文件「分片下载 + 断点续传 + 进度回调」。
//
// 背景：编辑器的 fileDownload 直接把 editContent（后端 file/read 已按 max_bytes=200000
// 截断）塞进 Blob，所以 10MB 的文件下载下来只有几百 KB，二进制文件更是被直接拦下。
// 正确做法是不碰编辑器缓冲，改走 /api/k8s/file/stat + /api/k8s/file/download：
//   - stat 先拿总大小 → 进度条有分母，也能提前算出分片数；
//   - download 按 offset/length 一片片取（容器内 tail -c + head -c + base64），
//     前端把 base64 还原成 Uint8Array 攒在内存里，最后一次性组装 Blob。
//
// 断点续传的实现要点：**offset 只在成功接到数据后才前进**。任何一片失败（网络抖动、
// kubectl exec 超时、Pod 短暂不可达）都只重试当前 offset，已下载的部分不用重来。

import { apiPost } from '../api/client';
import type { K8sFileDownloadResp, K8sFileStatResp } from '../api/types';

/** 单片默认 1MB：kubectl exec 每次建连有 0.5~2s 固定开销，片太小会让大文件拖很久。 */
export const DEFAULT_CHUNK_SIZE = 1024 * 1024;
/** 单片上限，与后端 MAX_CHUNK 保持一致（后端会自行 clamp，这里只是避免白跑一趟）。 */
export const MAX_CHUNK_SIZE = 8 * 1024 * 1024;

export type DownloadStatus =
  | 'idle'
  | 'preparing'
  | 'downloading'
  | 'paused'
  | 'done'
  | 'error'
  | 'cancelled';

export interface DownloadProgress {
  /** 已下载字节数 */
  received: number;
  /** 文件总字节数；0 表示未知（stat 失败但下载仍可继续） */
  total: number;
  /** 0~100；total 未知时为 -1 */
  percent: number;
  /** 瞬时速度 B/s（指数平滑） */
  speed: number;
  /** 已重试次数（用于提示「正在断点续传」） */
  retries: number;
  status: DownloadStatus;
  /** 当前分片序号（从 1 开始） */
  chunkIndex: number;
  /** 预计的分片总数；未知时为 0 */
  chunkTotal: number;
  /** 失败原因或阶段提示 */
  message?: string;
}

export interface DownloadTarget {
  env: string;
  pod: string;
  container?: string;
  namespace?: string;
  path: string;
}

export interface DownloaderOptions {
  chunkSize?: number;
  /** 单片最大重试次数，超过则整体失败 */
  maxRetries?: number;
  /**
   * 内存上限（字节）。分片是攒在内存里最后一次性组装 Blob 的，
   * 几个 GB 的日志文件会把标签页撑爆，所以超限时提前失败并给出替代方案。
   * 传 0 表示不限制。
   */
  maxSize?: number;
  onProgress?: (p: DownloadProgress) => void;
}

/** 默认内存上限 1.5GB（再大就得走 kubectl cp / 容器侧分片导出） */
export const DEFAULT_MAX_SIZE = 1536 * 1024 * 1024;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** 字节数格式化（与 utils/format.fmtSize 同款，避免 util 层循环依赖） */
function humanSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(1)} GB`;
}

/** base64 → Uint8Array（不用 atob + spread，避免大数组爆栈） */
export function base64ToBytes(b64: string): Uint8Array {
  const clean = (b64 || '').replace(/\s+/g, '');
  if (!clean) return new Uint8Array(0);
  const bin = atob(clean);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function fmtSpeed(bps: number): string {
  if (!bps || bps <= 0 || !isFinite(bps)) return '—';
  if (bps < 1024) return `${bps.toFixed(0)} B/s`;
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`;
  return `${(bps / 1048576).toFixed(1)} MB/s`;
}

/** 剩余时间估算，返回 'mm:ss' 或 '—' */
export function fmtEta(received: number, total: number, speed: number): string {
  if (!speed || speed <= 0 || total <= 0 || received >= total) return '—';
  const sec = Math.max(0, Math.round((total - received) / speed));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return `${m}m${String(s).padStart(2, '0')}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
}

/**
 * 分片下载器：start / pause / resume / cancel。
 *
 * 用法：
 * ```ts
 * const d = new K8sFileDownloader(target, { onProgress: setProg });
 * const blob = await d.run();   // 取消时返回 null
 * ```
 */
export class K8sFileDownloader {
  private target: DownloadTarget;
  private chunkSize: number;
  private maxRetries: number;
  private maxSize: number;
  private onProgress?: (p: DownloadProgress) => void;

  private chunks: Uint8Array[] = [];
  private received = 0;
  private total = 0;
  private retries = 0;
  private chunkIndex = 0;
  private speed = 0;
  private status: DownloadStatus = 'idle';
  private message = '';

  private cancelled = false;
  private paused = false;
  private waiters: (() => void)[] = [];
  private running = false;
  private abort: AbortController | null = null;

  constructor(target: DownloadTarget, opts: DownloaderOptions = {}) {
    this.target = target;
    this.chunkSize = Math.min(
      Math.max(64 * 1024, opts.chunkSize || DEFAULT_CHUNK_SIZE),
      MAX_CHUNK_SIZE
    );
    this.maxRetries = opts.maxRetries ?? 5;
    this.maxSize = opts.maxSize ?? DEFAULT_MAX_SIZE;
    this.onProgress = opts.onProgress;
  }

  private emit() {
    if (!this.onProgress) return;
    const total = this.total;
    this.onProgress({
      received: this.received,
      total,
      percent: total > 0 ? Math.min(100, (this.received / total) * 100) : -1,
      speed: this.speed,
      retries: this.retries,
      status: this.status,
      chunkIndex: this.chunkIndex,
      chunkTotal: total > 0 ? Math.ceil(total / this.chunkSize) : 0,
      message: this.message || undefined,
    });
  }

  private flushWaiters() {
    const ws = this.waiters;
    this.waiters = [];
    ws.forEach((w) => w());
  }

  /** 暂停：已下载的字节保留，恢复后从当前 offset 继续。 */
  pause() {
    if (this.status !== 'downloading' && this.status !== 'preparing') return;
    this.paused = true;
    this.status = 'paused';
    this.message = '';
    this.emit();
  }

  resume() {
    if (!this.paused) return;
    this.paused = false;
    this.status = 'downloading';
    this.message = '';
    this.emit();
    this.flushWaiters();
  }

  /** 取消：立即中断在途请求，丢弃已下载内容。 */
  cancel() {
    if (this.status === 'done' || this.status === 'cancelled') return;
    this.cancelled = true;
    this.paused = false;
    this.status = 'cancelled';
    this.message = '';
    try {
      this.abort?.abort();
    } catch {
      /* ignore */
    }
    this.emit();
    this.flushWaiters();
  }

  get isPaused() {
    return this.paused;
  }
  get isActive() {
    return this.status === 'preparing' || this.status === 'downloading' || this.status === 'paused';
  }

  /** 等待解除暂停；被取消时抛出以便跳出循环。 */
  private async gate() {
    while (this.paused && !this.cancelled) {
      await new Promise<void>((r) => this.waiters.push(r));
    }
    if (this.cancelled) throw new CancelledError();
  }

  /** 退避等待：500ms → 1s → 2s → 4s …，上限 8s */
  private backoff(attempt: number): number {
    return Math.min(8000, 500 * Math.pow(2, Math.max(0, attempt - 1)));
  }

  /** 取文件总大小；失败不阻断下载（退化为「未知大小」模式）。 */
  private async fetchSize(signal: AbortSignal): Promise<number> {
    try {
      const st = await apiPost<K8sFileStatResp>(
        '/api/k8s/file/stat',
        {
          env: this.target.env,
          pod: this.target.pod,
          container: this.target.container || '',
          namespace: this.target.namespace || '',
          path: this.target.path,
        },
        { signal },
      );
      if (st && st.ok && typeof st.size === 'number') return st.size;
      this.message = st?.error || '无法获取文件大小（将按未知大小下载）';
      return 0;
    } catch (ex: any) {
      this.message = ex?.message || '无法获取文件大小（将按未知大小下载）';
      return 0;
    }
  }

  /**
   * 执行下载。返回组装好的 Blob；被取消或失败返回 null
   * （失败原因通过 onProgress 的 message + status 传出）。
   */
  async run(): Promise<Blob | null> {
    if (this.running) return null;
    this.running = true;
    this.status = 'preparing';
    this.emit();

    try {
      const ac0 = new AbortController();
      this.abort = ac0;
      try {
        this.total = await this.fetchSize(ac0.signal);
      } finally {
        this.abort = null;
      }
      if (this.cancelled) {
        this.status = 'cancelled';
        this.emit();
        return null;
      }
      // 内存保护：分片是攒在内存里最后一次性组装 Blob 的，超大文件提前劝退
      if (this.maxSize > 0 && this.total > this.maxSize) {
        this.status = 'error';
        this.message =
          `文件 ${humanSize(this.total)} 超过内存上限 ${humanSize(this.maxSize)}，` +
          `浏览器一趟装不下；建议用 K8s Shell 里的 kubectl cp，或在容器内先 split 后再下载`;
        this.emit();
        return null;
      }
      this.status = 'downloading';
      this.emit();

      let offset = 0;
      let attempt = 0;

      for (;;) {
        await this.gate();

        this.chunkIndex += 1;
        const ac = new AbortController();
        this.abort = ac;
        const t0 = Date.now();

        let resp: K8sFileDownloadResp | null = null;
        let err = '';
        try {
          resp = await apiPost<K8sFileDownloadResp>(
            '/api/k8s/file/download',
            {
              env: this.target.env,
              pod: this.target.pod,
              container: this.target.container || '',
              namespace: this.target.namespace || '',
              path: this.target.path,
              offset,
              length: this.chunkSize,
            },
            { signal: ac.signal },
          );
        } catch (ex: any) {
          err = ex?.message || String(ex);
        } finally {
          this.abort = null;
        }

        // 被取消：不要走重试逻辑
        if (this.cancelled) {
          this.status = 'cancelled';
          this.emit();
          return null;
        }

        if (!resp || !resp.ok) {
          err = err || resp?.error || '分片读取失败';
          attempt += 1;
          this.retries += 1;
          this.chunkIndex -= 1; // 失败的分片不算数
          if (attempt > this.maxRetries) {
            this.status = 'error';
            this.message = `${err}（已重试 ${this.maxRetries} 次）`;
            this.emit();
            return null;
          }
          // 断点续传的关键：offset 保持不变，只重试当前片
          this.message = `${err} · ${attempt}/${this.maxRetries} 重试中（已下载 ${this.received} 字节，不会重来）`;
          // 暂停期间出错不要擅自把状态改回 downloading，否则界面与 gate() 的实际行为打架
          if (!this.paused) this.status = 'downloading';
          this.emit();
          await sleep(this.backoff(attempt));
          continue;
        }

        attempt = 0;
        const raw = base64ToBytes(resp.data || '');
        const dt = Math.max(1, Date.now() - t0) / 1000;
        const inst = raw.length / dt;
        this.speed = this.speed === 0 ? inst : this.speed * 0.7 + inst * 0.3;

        if (raw.length === 0) break; // 空文件 / 已到末尾

        this.chunks.push(raw);
        this.received += raw.length;
        offset += raw.length;
        this.message = '';
        this.emit();

        // 到末尾：本片不足请求长度，或已覆盖 stat 得到的总大小
        if (resp.eof) break;
        if (this.total > 0 && offset >= this.total) break;
      }

      if (this.cancelled) {
        this.status = 'cancelled';
        this.emit();
        return null;
      }

      this.status = 'done';
      this.message = '';
      this.emit();
      return new Blob(this.chunks as BlobPart[], { type: 'application/octet-stream' });
    } catch (ex) {
      if (ex instanceof CancelledError || this.cancelled) {
        this.status = 'cancelled';
        this.emit();
        return null;
      }
      this.status = 'error';
      this.message = (ex as any)?.message || String(ex);
      this.emit();
      return null;
    }
  }
}

class CancelledError extends Error {
  constructor() {
    super('cancelled');
  }
}

/** 触发浏览器保存 Blob 到本地 */
export function saveBlob(blob: Blob, filename: string) {
  const a = document.createElement('a');
  const url = URL.createObjectURL(blob);
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 立刻 revoke 在部分浏览器会打断下载，延后释放
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** 从容器内路径提取文件名 */
export function baseNameOf(p: string): string {
  return (p || '').split('/').filter(Boolean).pop() || 'file.bin';
}
