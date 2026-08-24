import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { apiGet, apiText } from '../api/client';
import type { K8sPodsResp } from '../api/types';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';

/* ============================================================
   独立全屏日志查看页 —— 迁移自 web/log_viewer.{html,js,css}

   能力对齐原生：搜索高亮（正则 / 忽略大小写 / 上一个下一个 / 匹配计数）、
   级别高亮（FATAL/ERROR/WARN/INFO/DEBUG）、Pod 与容器自由切换、tail 行数、
   --previous、自动刷新（live tail，自动跟随底部）、行号、换行、字号、
   下载、主题切换、返回。

   与原生的差异（有意为之）：
   - 不再手拼 innerHTML，改为 React 节点渲染，天然免疫 XSS；
   - 复用统一 API 客户端（apiText / apiGet），错误分类一致；
   - 主题复用 useAppStore（同一 localStorage key `jgg-theme`）。
   ============================================================ */

interface Params {
  pod: string;
  env: string;
  container: string;
  namespace: string;
}

interface PodOpt {
  name: string;
  namespace?: string;
  phase?: string;
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 级别识别（与原生 detectLevel 完全一致） */
function detectLevel(line: string): string {
  const u = line.toUpperCase();
  if (/\b(FATAL|CRITICAL)\b/.test(u)) return 'FATAL';
  if (/\b(ERROR|ERR|EXCEPTION|TRACEBACK|UNCAUGHT)\b/.test(u)) return 'ERROR';
  if (/\b(WARN|WARNING)\b/.test(u)) return 'WARN';
  if (/\bINFO\b/.test(u)) return 'INFO';
  if (/\bDEBUG\b/.test(u)) return 'DEBUG';
  return '';
}

/** 把一行按搜索正则切成「普通文本 + <mark> 命中」の React 节点 */
function highlightNodes(line: string, re: RegExp | null): ReactNode {
  if (!re) return line || '\u00a0';
  const g = new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g');
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  g.lastIndex = 0;
  while ((m = g.exec(line)) !== null) {
    if (m.index > last) out.push(line.slice(last, m.index));
    out.push(<mark className="lv-hl" key={key++}>{m[0]}</mark>);
    last = m.index + m[0].length;
    if (m.index === g.lastIndex) g.lastIndex++;
  }
  const tail = line.slice(last);
  if (tail) out.push(tail);
  return out.length ? out : '\u00a0';
}

/* ============================================================
   时间戳转换：把日志行内的长数字时间戳（如 INFO 281460870429024）
   转换为 UTC+8 可读的「yyyy mm dd hh:mm:ss」。
   - 支持单位：毫秒 / 微秒 / 纳秒；
   - 自动模式：依次试 ms/us/ns，取落在合理范围（2000~now+1y）的结果；
   - 自定义基准（epochSec）：用于非 1970 起点的私有时间戳，
     例如某框架以「2015-01-01」为 0 点的微秒，则填 1420070400。
   - 注意：HCM/python 日志行中「级别后面的长数字」是线程号（%(thread)d），
     形如 INFO 281469013717408 [handlers.py:451] ... —— 它不是时间戳，
     一律跳过，避免被误转换成 1970/1978 之类的错误日期。
   ============================================================ */

/** HCM/python 日志行：LEVEL <线程号(13~19位)> [文件名.后缀:行号] 消息 */
const HCM_THREAD_ID_RE =
  /^\s*(?:FATAL|CRITICAL|ERROR|ERR|WARN|WARNING|INFO|DEBUG)\s+(\d{13,19})\s+\[[\w./-]+\.\w+:\d+\]/i;

/** 把 Date 格式化为 Asia/Shanghai 的 yyyy mm dd hh:mm:ss */
function fmtUtc8(d: Date): string {
  const s = d.toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai', hour12: false });
  return s.replace(/-/g, ' ');
}

function saneDate(ms: number): Date | null {
  const d = new Date(ms);
  if (isNaN(d.getTime())) return null;
  const y = d.getUTCFullYear();
  if (y < 1970 || y > 2100) return null;
  return d;
}

function convertTs(numStr: string, unit: string, epochSec: string): Date | null {
  const n = Number(numStr);
  if (!isFinite(n)) return null;
  let ms: number;
  if (epochSec.trim() !== '') {
    const base = Number(epochSec) * 1000;
    if (!isFinite(base)) return null;
    const mul = unit === 'ns' ? 1e-6 : unit === 'us' ? 1e-3 : 1; // 归一到毫秒
    ms = base + n * mul;
  } else {
    ms = unit === 'ns' ? n / 1e6 : unit === 'us' ? n / 1e3 : n; // ms / us / ns → ms
  }
  return saneDate(ms);
}

// HCM 类后台常以「某私有纪元（如 2015~2022 某天）为 0 点」的 微秒/纳秒 计数，
// 标准 1970 纪元无法解释（如 281460870429024 既非 1970 的 ms/us/ns）。
// 1970 模式全部落空时，回退到这些常见私有纪元（单位 µs/ns）。
const PRIVATE_EPOCHS = [
  1420070400, // 2015-01-01
  1451606400, // 2016-01-01
  1483228800, // 2017-01-01
  1514764800, // 2018-01-01
  1546300800, // 2019-01-01
  1577836800, // 2020-01-01
  1609459200, // 2021-01-01
  1640995200, // 2022-01-01
];

function detectTs(numStr: string, epochSec: string): Date | null {
  if (epochSec.trim() !== '') {
    for (const u of ['ms', 'us', 'ns']) {
      const d = convertTs(numStr, u, epochSec);
      if (d) return d;
    }
    return null;
  }
  const lower = Date.UTC(2000, 0, 1);
  const upper = Date.now() + 365 * 864e5;
  // 1) 标准 1970 纪元（ms/us/ns）
  for (const u of ['ms', 'us', 'ns']) {
    const d = convertTs(numStr, u, '');
    if (d && d.getTime() > lower && d.getTime() < upper) return d;
  }
  // 2) 私有纪元回退（µs/ns）—— 命中第一个在合理区间的，按基准从早到晚
  for (const base of PRIVATE_EPOCHS) {
    for (const u of ['us', 'ns']) {
      const d = convertTs(numStr, u, String(base));
      if (d && d.getTime() > lower && d.getTime() < upper) return d;
    }
  }
  return null;
}

/** 把一行内 13~19 位的纯数字时间戳就地转换为「可读时间 (原值)」 */
function convertTsInLine(line: string, unit: string, epochSec: string): string {
  // HCM/python 日志行的第二段是线程号（LEVEL <thread> [xxx.py:NNN]），不是时间戳，跳过
  const threadId = line.match(HCM_THREAD_ID_RE)?.[1];
  return line.replace(/\b\d{13,19}\b/g, (m) => {
    if (threadId && threadId === m) return m;
    const d = epochSec.trim() !== '' ? detectTs(m, epochSec) : unit === 'auto' ? detectTs(m, '') : convertTs(m, unit, '');
    if (!d) return m;
    return `${fmtUtc8(d)} (${m})`;
  });
}

/** 把行首 kubectl --timestamps 的 RFC3339 UTC 时间（如 2026-08-24T13:36:54.123456789Z）转为本地时区显示 */
function convertUtcToLocal(line: string): string {
  return line.replace(/^(\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\s/, (m, ts: string) => {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return m;
    const local = d.toLocaleString('sv-SE', { hour12: false }).replace(/-/g, ' ');
    return `${local} `;
  });
}

export function LogViewer() {
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);
  const { t } = useT();

  const [params, setParams] = useState<Params>(() => {
    const qs = new URLSearchParams(location.search);
    return {
      pod: qs.get('pod') || '',
      env: qs.get('env') || '',
      container: qs.get('container') || '',
      namespace: qs.get('namespace') || '',
    };
  });

  const [pods, setPods] = useState<PodOpt[]>([]);
  const [containers, setContainers] = useState<string[]>([]);
  const [raw, setRaw] = useState('');
  const [status, setStatus] = useState(t('logviewer.preparing'));
  const [isErr, setIsErr] = useState(false);

  // 工具栏
  const [tail, setTail] = useState('200');
  const [previous, setPrevious] = useState(false);
  const [timestamps, setTimestamps] = useState(true); // kubectl --timestamps，默认带时间戳
  const [auto, setAuto] = useState('0');
  const [wrap, setWrap] = useState(true);
  const [lineno, setLineno] = useState(true);
  const [levelOn, setLevelOn] = useState(true);
  const [font, setFont] = useState(13);

  // 时间戳转换
  const [tsConv, setTsConv] = useState(true);
  const [tsUnit, setTsUnit] = useState('auto'); // auto | ms | us | ns
  const [tsEpoch, setTsEpoch] = useState(''); // 自定义基准（Unix 秒），用于非 1970 起点的时间戳

  // 时间范围 / label 聚合 / 级别过滤 / 排除
  const [since, setSince] = useState('');
  const [until, setUntil] = useState('');
  const [labelMode, setLabelMode] = useState(false);
  const [label, setLabel] = useState('');
  const [levelFilter, setLevelFilter] = useState('ALL');
  const [exclude, setExclude] = useState(false);

  // P2：本地时区显示 / 复制反馈 / Events 联动 / 流式跟随
  const [localTz, setLocalTz] = useState(false);
  const [copiedHint, setCopiedHint] = useState(''); // '' | 'all' | 'visible'
  const [eventsOn, setEventsOn] = useState(false);
  const [events, setEvents] = useState<{ items: any[]; error: string }>({ items: [], error: '' });
  const [streaming, setStreaming] = useState(false);

  // 搜索
  const [search, setSearch] = useState('');
  const [useRegex, setUseRegex] = useState(false);
  const [ci, setCi] = useState(true);
  const [cur, setCur] = useState(-1);

  const bodyRef = useRef<HTMLElement | null>(null);
  const lineRefs = useRef<(HTMLDivElement | null)[]>([]);
  const autoFollowRef = useRef(false);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const tailRef = useRef(tail);
  tailRef.current = tail;
  const prevRef = useRef(previous);
  prevRef.current = previous;
  const tsRef = useRef(timestamps);
  tsRef.current = timestamps;
  const sinceRef = useRef(since);
  sinceRef.current = since;
  const untilRef = useRef(until);
  untilRef.current = until;
  const labelModeRef = useRef(labelMode);
  labelModeRef.current = labelMode;
  const labelRef = useRef(label);
  labelRef.current = label;
  const levelRef = useRef(levelFilter);
  levelRef.current = levelFilter;
  const excludeRef = useRef(exclude);
  excludeRef.current = exclude;
  const localTzRef = useRef(localTz);
  localTzRef.current = localTz;
  const abortRef = useRef<AbortController | null>(null);
  const streamingRef = useRef(false);
  streamingRef.current = streaming;
  const copiedTimerRef = useRef<number | null>(null);

  /* ---------- 滚动 ---------- */
  const isAtBottom = useCallback(() => {
    const b = bodyRef.current;
    if (!b) return true;
    return b.scrollHeight - b.scrollTop - b.clientHeight < 48;
  }, []);
  const scrollBottom = useCallback(() => {
    const b = bodyRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, []);

  /* ---------- 拉取日志 ---------- */
  const refresh = useCallback(async () => {
    const p = paramsRef.current;
    const useLabel = labelModeRef.current && labelRef.current.trim();
    if (!useLabel && !p.pod) { setStatus(t('logviewer.noPod')); setIsErr(true); return; }
    const follow = autoFollowRef.current || isAtBottom();
    setStatus(t('logviewer.loading'));
    setIsErr(false);
    try {
      const q = new URLSearchParams();
      if (useLabel) {
        q.set('label', labelRef.current.trim());
        if (p.env) q.set('env', p.env);
      } else {
        q.set('name', p.pod);
        if (p.env) q.set('env', p.env);
        if (p.container) q.set('container', p.container);
        if (p.namespace) q.set('namespace', p.namespace);
      }
      q.set('tail', tailRef.current);
      if (prevRef.current) q.set('previous', '1');
      q.set('timestamps', tsRef.current ? '1' : '0');
      if (sinceRef.current.trim()) q.set('since', sinceRef.current.trim());
      if (untilRef.current.trim()) q.set('until', untilRef.current.trim());
      const text = await apiText('/api/k8s/log?' + q.toString());
      setRaw(text);
      setStatus('');
      if (follow) requestAnimationFrame(scrollBottom);
    } catch (ex: any) {
      setStatus(t('logviewer.loadFail', { msg: ex.message }));
      setIsErr(true);
    }
  }, [isAtBottom, scrollBottom, t]);

  /* ---------- 容器列表 ---------- */
  const loadContainers = useCallback(async (pod: string, env: string) => {
    if (!pod) { setContainers([]); return; }
    try {
      const d = await apiGet<{ ok?: boolean; containers?: string[]; namespace?: string }>(
        `/api/k8s/pod-containers?name=${encodeURIComponent(pod)}&env=${encodeURIComponent(env)}`
      );
      if (d.ok && Array.isArray(d.containers)) {
        setContainers(d.containers);
        if (d.namespace && !paramsRef.current.namespace) {
          setParams((s) => ({ ...s, namespace: d.namespace as string }));
        }
      } else {
        setContainers([]);
      }
    } catch {
      // 与原生一致：容器列表失败不影响日志拉取
      setContainers([]);
    }
  }, []);

  /* ---------- Pod 列表 ---------- */
  const loadPods = useCallback(async () => {
    const p = paramsRef.current;
    if (!p.env) { setStatus(t('logviewer.noEnv')); setIsErr(true); return; }
    try {
      let q = '/api/k8s/pods?env=' + encodeURIComponent(p.env);
      if (p.namespace) q += '&namespace=' + encodeURIComponent(p.namespace);
      const d = await apiGet<K8sPodsResp>(q);
      if (!d.ok || !Array.isArray(d.pods)) {
        setStatus(t('logviewer.podListFail', { msg: d.error || t('logviewer.unknown') }));
        setIsErr(true);
        return;
      }
      setPods(d.pods as PodOpt[]);
      if (!p.pod) { setStatus(t('logviewer.pickPod')); setIsErr(false); }
    } catch (ex: any) {
      setStatus(t('logviewer.podListFail', { msg: ex.message }));
      setIsErr(true);
    }
  }, [t]);

  // 启动：Pod 列表 → 容器列表 → 日志（与原生启动順序一致）
  useEffect(() => {
    (async () => {
      await loadPods();
      await loadContainers(paramsRef.current.pod, paramsRef.current.env);
      if (paramsRef.current.pod) await refresh();
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ---------- 切替 Pod ---------- */
  const switchPod = useCallback(async (podName: string) => {
    setRaw('');
    if (!podName) {
      setParams((s) => ({ ...s, pod: '', container: '', namespace: '' }));
      setContainers([]);
      setStatus(t('logviewer.pickPod'));
      setIsErr(false);
      return;
    }
    const hit = pods.find((x) => x.name === podName);
    const next: Params = {
      ...paramsRef.current,
      pod: podName,
      container: '',
      namespace: hit?.namespace || '',
    };
    setParams(next);
    paramsRef.current = next;
    await loadContainers(podName, next.env);
    await refresh();
  }, [pods, loadContainers, refresh, t]);

  const switchContainer = useCallback(async (name: string) => {
    const next = { ...paramsRef.current, container: name };
    setParams(next);
    paramsRef.current = next;
    await refresh();
  }, [refresh]);

  /* ---------- 自动刷新 ---------- */
  useEffect(() => {
    const sec = parseInt(auto, 10) || 0;
    autoFollowRef.current = sec > 0;
    if (sec <= 0) return;
    const t = window.setInterval(refresh, sec * 1000);
    return () => window.clearInterval(t);
  }, [auto, refresh]);

  // tail / previous 変化后立即重拉（对应原生 onchange = refresh）
  const firstTailRun = useRef(true);
  useEffect(() => {
    if (firstTailRun.current) { firstTailRun.current = false; return; }
    if (streamingRef.current) return; // 流式跟随中不允许被覆盖
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tail, previous]);

  /* ---------- 搜索正则（防抖 200ms） ---------- */
  const [debounced, setDebounced] = useState('');
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(search), 200);
    return () => window.clearTimeout(t);
  }, [search]);

  const re = useMemo<RegExp | null>(() => {
    if (!debounced) return null;
    try {
      return new RegExp(useRegex ? debounced : escapeRegExp(debounced), ci ? 'i' : '');
    } catch {
      return null; // 正则非法时不高亮（与原生一致）
    }
  }, [debounced, useRegex, ci]);

  /* ---------- 行解析 ---------- */
  const lines = useMemo(() => raw.split(/\r\n|\r|\n/), [raw]);

  // 时间戳转换后的展示行（不改动 raw，便于下载保留原值）；再叠加本地时区 + 级别过滤 + 排除
  const viewLines = useMemo(() => {
    let arr = tsConv ? lines.map((l) => convertTsInLine(l, tsUnit, tsEpoch)) : lines;
    if (localTzRef.current) arr = arr.map((l) => convertUtcToLocal(l));
    const lv = levelRef.current;
    if (lv !== 'ALL') {
      arr = arr.filter((l) => /^=+\s/.test(l) || detectLevel(l) === lv);
    }
    if (excludeRef.current && re) {
      arr = arr.filter((l) => /^=+\s/.test(l) || !re.test(l));
    }
    return arr;
  }, [lines, tsConv, tsUnit, tsEpoch, localTz, levelFilter, exclude, re]);

  const matches = useMemo(() => {
    if (!re) return [] as number[];
    const out: number[] = [];
    for (let i = 0; i < viewLines.length; i++) {
      if (/^=+\s/.test(viewLines[i])) continue; // 分隔行不计入匹配
      re.lastIndex = 0;
      if (re.test(viewLines[i])) out.push(i);
    }
    return out;
  }, [viewLines, re]);

  // 搜索条件或内容变化后，定位到第一个匹配
  useEffect(() => {
    if (!matches.length) { setCur(-1); return; }
    setCur(0);
  }, [matches]);

  useEffect(() => {
    if (cur < 0 || !matches.length) return;
    const el = lineRefs.current[matches[cur]];
    if (el) el.scrollIntoView({ block: 'center' });
  }, [cur, matches]);

  const gotoMatch = useCallback((dir: number) => {
    if (!matches.length) return;
    setCur((c) => (c + dir + matches.length) % matches.length);
  }, [matches]);

  /* ---------- 下载 ---------- */
  const download = useCallback(() => {
    if (!raw) return;
    const p = paramsRef.current;
    const blob = new Blob([raw], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (p.pod || 'pod') + (p.container ? '__' + p.container : '') + '.log';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }, [raw]);

  const goBack = useCallback(() => {
    if (window.opener) window.close();
    else history.back();
  }, []);

  /* ---------- P2：复制 ---------- */
  const flashCopied = useCallback((which: string) => {
    setCopiedHint(which);
    if (copiedTimerRef.current) window.clearTimeout(copiedTimerRef.current);
    copiedTimerRef.current = window.setTimeout(() => setCopiedHint(''), 1500);
  }, []);

  const copyText = useCallback(async (text: string, which: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* ignore */ }
      document.body.removeChild(ta);
    }
    flashCopied(which);
  }, [flashCopied]);

  const copyAll = useCallback(() => copyText(raw, 'all'), [copyText, raw]);
  const copyVisible = useCallback(
    () => copyText(viewLines.filter((l) => !/^=+\s/.test(l)).join('\n'), 'visible'),
    [copyText, viewLines]
  );

  /* ---------- P2：Events 联动 ---------- */
  const loadEvents = useCallback(async () => {
    const p = paramsRef.current;
    if (!p.pod) { setEvents({ items: [], error: '' }); return; }
    try {
      const q = new URLSearchParams({ name: p.pod, env: p.env });
      if (p.namespace) q.set('namespace', p.namespace);
      const d = await apiGet<{ ok?: boolean; events?: any[]; error?: string }>(
        '/api/k8s/events?' + q.toString()
      );
      if (d.ok && Array.isArray(d.events)) {
        setEvents({ items: d.events, error: '' });
      } else {
        setEvents({ items: [], error: d.error || t('logviewer.unknown') });
      }
    } catch (ex: any) {
      setEvents({ items: [], error: ex.message });
    }
  }, [t]);

  useEffect(() => {
    if (eventsOn) loadEvents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventsOn, params.pod, params.namespace, params.env]);

  /* ---------- P2：流式跟随 ---------- */
  const startStream = useCallback(async () => {
    const p = paramsRef.current;
    if (!p.pod) return;
    const ac = new AbortController();
    abortRef.current = ac;
    setRaw('');
    setStatus(t('logviewer.streaming'));
    setIsErr(false);
    try {
      const q = new URLSearchParams();
      q.set('name', p.pod);
      if (p.env) q.set('env', p.env);
      if (p.container) q.set('container', p.container);
      if (p.namespace) q.set('namespace', p.namespace);
      q.set('tail', tailRef.current);
      if (prevRef.current) q.set('previous', '1');
      q.set('timestamps', tsRef.current ? '1' : '0');
      if (sinceRef.current.trim()) q.set('since', sinceRef.current.trim());
      if (untilRef.current.trim()) q.set('until', untilRef.current.trim());
      const resp = await fetch('/api/k8s/log/stream?' + q.toString(), { signal: ac.signal });
      if (!resp.body) { setStatus(t('logviewer.loadFail', { msg: 'no stream body' })); setIsErr(true); return; }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      const MAX = 200000; // 行数保护上限
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let arr = buf.split('\n');
        if (arr.length > MAX) arr = arr.slice(arr.length - MAX);
        buf = arr.join('\n');
        setRaw(buf);
        if (isAtBottom()) requestAnimationFrame(scrollBottom);
      }
      setStreaming(false);
      setStatus('');
    } catch (ex: any) {
      if (ex && ex.name === 'AbortError') { setStatus(''); return; }
      setStatus(t('logviewer.loadFail', { msg: ex.message || String(ex) }));
      setIsErr(true);
    }
  }, [isAtBottom, scrollBottom, t]);

  const stopStream = useCallback(() => {
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    setStreaming(false);
    setStatus('');
  }, []);

  const toggleStream = useCallback(async (on: boolean) => {
    if (on) {
      setStreaming(true);
      await startStream();
    } else {
      stopStream();
    }
  }, [startStream, stopStream]);

  // 卸载时回收流式子进程
  useEffect(() => {
    return () => {
      if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    };
  }, []);

  // 快捷键：Enter / Shift+Enter 跳转匹配，Ctrl/Cmd+F 聚焦搜索
  const searchRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        searchRef.current?.focus();
        searchRef.current?.select();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  lineRefs.current = [];

  return (
    <div className="logviewer">
      <header className="lv-head">
        <div className="lv-head-left">
          <button className="btn btn-ghost btn-sm" onClick={goBack} title={t('common.back')}>← {t('common.back')}</button>
          <div className="lv-title">
            <select
              className="sel lv-podsel"
              title={t('logviewer.podSelect')}
              value={params.pod}
              disabled={labelMode}
              onChange={(e) => switchPod(e.target.value)}
            >
              <option value="">{t('logviewer.selectPod')}</option>
              {pods.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}{p.namespace ? ` [${p.namespace}]` : ''} · {p.phase || '?'}
                </option>
              ))}
            </select>
            <div className="lv-meta">
              <span className="lv-chip lv-chip-env">env: {params.env || '—'}</span>
              <span className="lv-chip lv-chip-ns">ns: {params.namespace || '—'}</span>
              {params.container && <span className="lv-chip lv-chip-ct">{t('logviewer.container')}: {params.container}</span>}
            </div>
          </div>
        </div>
        <div className="lv-head-right">
          <button className="btn btn-sm" onClick={refresh} title={t('logviewer.refresh')} disabled={streaming}>↻ {t('logviewer.refresh')}</button>
          <button className="btn btn-sm" onClick={download} title={t('logviewer.download')} disabled={!raw}>↓ {t('logviewer.download')}</button>
          <button className="btn btn-icon" onClick={toggleTheme} title={t('app.themeToggle')}>{theme === 'dark' ? '☀' : '🌓'}</button>
        </div>
      </header>

      <div className="lv-toolbar">
        <label className="lv-field">{t('logviewer.container')}
          <select className="sel" value={params.container} onChange={(e) => switchContainer(e.target.value)}>
            <option value="">{t('logviewer.allContainers')}</option>
            {containers.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>

        <label className="lv-field lv-search">{t('logviewer.search')}
          <span className="lv-searchbox">
            <input
              ref={searchRef}
              type="text"
              className="input"
              placeholder={t('logviewer.searchPlaceholder')}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); gotoMatch(e.shiftKey ? -1 : 1); }
              }}
            />
            <span className="lv-match">{(matches.length ? cur + 1 : 0)}/{matches.length}</span>
            <button className="btn btn-xs" title={t('logviewer.prevMatch')} onClick={() => gotoMatch(-1)}>▲</button>
            <button className="btn btn-xs" title={t('logviewer.nextMatch')} onClick={() => gotoMatch(1)}>▼</button>
            <label className="lv-re"><input type="checkbox" checked={useRegex} onChange={(e) => setUseRegex(e.target.checked)} /> {t('logviewer.regex')}</label>
            <label className="lv-re"><input type="checkbox" checked={ci} onChange={(e) => setCi(e.target.checked)} /> {t('logviewer.ignoreCase')}</label>
          </span>
        </label>

        <label className="lv-field">{t('logviewer.lines')}
          <select className="sel" value={tail} onChange={(e) => setTail(e.target.value)}>
            <option value="50">50</option>
            <option value="200">200</option>
            <option value="500">500</option>
            <option value="1000">1000</option>
            <option value="5000">{t('logviewer.all')}</option>
          </select>
        </label>

        <label className="lv-field">{t('logviewer.autoRefresh')}
          <select className="sel" value={auto} onChange={(e) => setAuto(e.target.value)} disabled={streaming}>
            <option value="0">{t('logviewer.off')}</option>
            <option value="3">3 {t('logviewer.seconds')}</option>
            <option value="5">5 {t('logviewer.seconds')}</option>
            <option value="10">10 {t('logviewer.seconds')}</option>
          </select>
        </label>

        <label className="lv-toggle"><input type="checkbox" checked={previous} onChange={(e) => setPrevious(e.target.checked)} /> {t('logviewer.previous')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={timestamps} onChange={(e) => setTimestamps(e.target.checked)} /> {t('logviewer.timestamps')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> {t('logviewer.wrap')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={lineno} onChange={(e) => setLineno(e.target.checked)} /> {t('logviewer.lineNo')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={levelOn} onChange={(e) => setLevelOn(e.target.checked)} /> {t('logviewer.levelHighlight')}</label>

        <label className="lv-toggle"><input type="checkbox" checked={tsConv} onChange={(e) => setTsConv(e.target.checked)} /> {t('logviewer.tsConvert')}</label>
        {tsConv && (
          <label className="lv-field">{t('logviewer.tsUnit')}
            <select className="sel" value={tsUnit} onChange={(e) => setTsUnit(e.target.value)}>
              <option value="auto">{t('logviewer.tsAuto')}</option>
              <option value="ms">{t('logviewer.tsMs')}</option>
              <option value="us">{t('logviewer.tsUs')}</option>
              <option value="ns">{t('logviewer.tsNs')}</option>
            </select>
          </label>
        )}
        {tsConv && (
          <label className="lv-field lv-ts-epoch">{t('logviewer.tsEpoch')}
            <input
              type="text"
              className="input input-sm"
              placeholder={t('logviewer.tsBaseHint')}
              value={tsEpoch}
              onChange={(e) => setTsEpoch(e.target.value.replace(/[^\d]/g, ''))}
            />
          </label>
        )}

        <span className="lv-spacer" />
        <span className="lv-font">{t('logviewer.fontSize')}
          <button className="btn btn-xs" title={t('logviewer.shrink')} onClick={() => setFont((f) => Math.max(10, f - 1))}>A−</button>
          <button className="btn btn-xs" title={t('logviewer.enlarge')} onClick={() => setFont((f) => Math.min(22, f + 1))}>A+</button>
        </span>
      </div>

      <div className="lv-toolbar lv-toolbar-2">
        <label className="lv-field">{t('logviewer.since')}
          <input type="text" className="input input-sm" placeholder={t('logviewer.sinceHint')} value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
        <label className="lv-field">{t('logviewer.until')}
          <input type="text" className="input input-sm" placeholder={t('logviewer.untilHint')} value={until} onChange={(e) => setUntil(e.target.value)} />
        </label>

        <label className="lv-toggle"><input type="checkbox" checked={labelMode} onChange={(e) => setLabelMode(e.target.checked)} /> {t('logviewer.labelMode')}</label>
        {labelMode && (
          <label className="lv-field">{t('logviewer.label')}
            <input type="text" className="input input-sm" placeholder={t('logviewer.labelHint')} value={label} onChange={(e) => setLabel(e.target.value)} />
          </label>
        )}

        <label className="lv-field">{t('logviewer.level')}
          <select className="sel" value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}>
            <option value="ALL">{t('logviewer.levelAll')}</option>
            <option value="ERROR">ERROR</option>
            <option value="WARN">WARN</option>
            <option value="INFO">INFO</option>
            <option value="DEBUG">DEBUG</option>
          </select>
        </label>

        <label className="lv-toggle" title={t('logviewer.excludeHint')}><input type="checkbox" checked={exclude} onChange={(e) => setExclude(e.target.checked)} /> {t('logviewer.exclude')}</label>
      </div>

      <div className="lv-toolbar lv-toolbar-3">
        <label className="lv-toggle" title={t('logviewer.localTzHint')}><input type="checkbox" checked={localTz} onChange={(e) => setLocalTz(e.target.checked)} /> {t('logviewer.localTz')}</label>
        <button className="btn btn-sm" onClick={copyVisible} disabled={!raw}>⧉ {t('logviewer.copyVisible')}</button>
        <button className="btn btn-sm" onClick={copyAll} disabled={!raw}>⧉ {t('logviewer.copyAll')}</button>
        {copiedHint && <span className="lv-copied">{t('logviewer.copied')}</span>}
        <label className="lv-toggle"><input type="checkbox" checked={eventsOn} onChange={(e) => setEventsOn(e.target.checked)} /> {t('logviewer.events')}</label>
        <label className="lv-toggle" title={labelMode ? t('logviewer.stream') : ''}><input type="checkbox" checked={streaming} disabled={labelMode} onChange={(e) => toggleStream(e.target.checked)} /> {t('logviewer.stream')}</label>
        {streaming && <button className="btn btn-sm btn-danger" onClick={stopStream}>{t('logviewer.stopStream')}</button>}
      </div>

      {eventsOn && (
        <div className="lv-events">
          <div className="lv-events-head">
            <span className="lv-events-title">{t('logviewer.events')}</span>
            <span className="lv-events-hint">{t('logviewer.eventsHint')}</span>
          </div>
          {events.error && <div className="lv-statusline" style={{ color: 'var(--danger)' }}>{events.error}</div>}
          {!events.error && events.items.length === 0 && (
            <div className="empty-hint">{t('logviewer.eventsEmpty')}</div>
          )}
          {!events.error && events.items.length > 0 && (
            <div className="lv-events-wrap">
              <table className="lv-events-table">
                <thead>
                  <tr>
                    <th>{t('logviewer.eventsTime')}</th>
                    <th>{t('logviewer.eventsType')}</th>
                    <th>{t('logviewer.eventsReason')}</th>
                    <th>{t('logviewer.eventsMsg')}</th>
                  </tr>
                </thead>
                <tbody>
                  {events.items.map((e, i) => (
                    <tr key={i} className={'ev-' + (e.type || 'Normal')}>
                      <td className="ev-time">{(e.last_seen || '').replace('T', ' ').replace('Z', '').slice(0, 19)}</td>
                      <td className="ev-type">{e.type || ''}</td>
                      <td className="ev-reason">{e.reason || ''}</td>
                      <td className="ev-msg" title={e.message || ''}>{e.message || ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <main className="lv-body" ref={bodyRef as any}>
        {status && (
          <div className="lv-statusline" style={{ color: isErr ? 'var(--danger)' : 'var(--muted)' }}>{status}</div>
        )}
        <div className={'lv-log' + (wrap ? ' wrap' : '')} style={{ fontSize: font + 'px' }}>
          {viewLines.map((line, i) => {
            const sep = /^=+\s/.test(line);
            const lv = !sep && levelOn ? detectLevel(line) : '';
            const isCur = cur >= 0 && matches[cur] === i;
            const cls = [
              'lv-line',
              sep ? 'lv-sep' : '',
              lv ? 'lev-' + lv : '',
              isCur ? 'lv-curmatch' : '',
            ].filter(Boolean).join(' ');
            return (
              <div key={i} className={cls} ref={(el) => { lineRefs.current[i] = el; }}>
                {!sep && lineno && <span className="lv-no">{i + 1}</span>}
                <span className="lv-text">{sep ? line : highlightNodes(line, re)}</span>
              </div>
            );
          })}
        </div>
      </main>
    </div>
  );
}
