import { useEffect, useRef, useState, useCallback, useMemo, memo, type MouseEvent as ReactMouseEvent } from 'react';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { sse } from '../api/events';
import { cfdebug } from '../api/cfdebug/client';
import { DapClient } from '../api/cfdebug/dapClient';
import { CfDebugSyncModal } from './CfDebugSyncModal';
import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';

hljs.registerLanguage('python', python);
import type {
  CfFunction,
  CfEnvConfig,
  CfMode,
  CfDebugStatus,
  CfFunctionList,
  CfSource,
  CfAccount,
  DapStackFrame,
  DapScope,
  DapVariable,
  DapStoppedBody,
  DapOutputBody,
  SSECFDebugLog,
  SSECFDebugDone,
  BpOptions,
  WatchItem,
  DynLogRecord,
} from '../api/cfdebug/types';

type RightTab = 'variables' | 'stack' | 'breakpoints' | 'watches' | 'result' | 'logs';

// 由断点 Map 构造下发到 DAP 的断点列表（仅 enabled；条件/日志/命中次数透传 debugpy）。
function bpListForDap(map: Map<number, BpOptions>): { line: number; condition?: string; hitCondition?: string; logMessage?: string }[] {
  const out: { line: number; condition?: string; hitCondition?: string; logMessage?: string }[] = [];
  for (const [line, o] of map) {
    if (!o.enabled) continue;
    const bp: any = { line };
    if (o.condition) bp.condition = o.condition;
    if (o.hitCondition) bp.hitCondition = o.hitCondition;
    if (o.logMessage) bp.logMessage = o.logMessage;
    out.push(bp);
  }
  return out;
}

// 从源码中提取 kwargs.get("X") / kwargs.get('X') 的键名。
// 用于 **kwargs 函数的入参预填：函数声明只有 **kwargs，AST 拿不到具体键，
// 但函数体里通常会用 kwargs.get("xxx", ...) 取值，据此可推测需要哪些入参。
function extractKwargsFromSource(sourceLines: string[]): string[] {
  const keys = new Set<string>();
  const re = /kwargs\.get\(["']([^"']+)["']/g;
  for (const line of sourceLines) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(line)) !== null) {
      keys.add(m[1]);
    }
  }
  return Array.from(keys);
}

// 右键未选区时，取光标下的「词」（变量名/属性链）作为待求值表达式
function getWordAtPoint(x: number, y: number): string {
  const doc = document as any;
  let range: Range | null = null;
  if (typeof doc.caretRangeFromPoint === 'function') {
    range = doc.caretRangeFromPoint(x, y);
  } else if (typeof doc.caretPositionFromPoint === 'function') {
    const pos = doc.caretPositionFromPoint(x, y);
    if (pos) {
      range = document.createRange();
      range.setStart(pos.offsetNode, pos.offset);
      range.collapse(true);
    }
  }
  if (!range) return '';
  const node = range.startContainer;
  if (node.nodeType !== Node.TEXT_NODE) return '';
  const text = node.textContent || '';
  return wordAround(text, range.startOffset);
}

function wordAround(text: string, offset: number): string {
  let start = offset;
  while (start > 0 && /[A-Za-z0-9_.]/.test(text[start - 1])) start--;
  let end = offset;
  while (end < text.length && /[A-Za-z0-9_.]/.test(text[end])) end++;
  return text.slice(start, end);
}

export function CfDebugPanel() {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);

  // ── 环境配置 ──
  const [envConfig, setEnvConfig] = useState<CfEnvConfig | null>(null);
  const [rootInput, setRootInput] = useState('');
  // ── 调试模式：本地(mock) / 远程(连 cf_accounts 真实服务器) ──
  const [mode, setMode] = useState<CfMode>('local');
  const [accounts, setAccounts] = useState<CfAccount[]>([]);
  const [selectedAccount, setSelectedAccount] = useState(''); // cf_accounts 的 server_url 或 name
  const [syncOpen, setSyncOpen] = useState(false);

  // ── 函数列表 / 选择 ──
  const [functions, setFunctions] = useState<CfFunction[]>([]);
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<CfFunction | null>(null);
  const [scanning, setScanning] = useState(false);

  // ── 参数 / 高级选项 ──
  const [kwargsInput, setKwargsInput] = useState('{}');
  const [debugIdInput, setDebugIdInput] = useState('');
  const [dbUrlInput, setDbUrlInput] = useState('');
  const [allowDdl, setAllowDdl] = useState(false);
  const [dbSave, setDbSave] = useState(false);
  const [writeReal, setWriteReal] = useState(false);
  const [advOpen, setAdvOpen] = useState(false);
  // 入参 / 调试日志 默认收起（用户要求默认关掉，需要时再展开）
  const [paramsOpen, setParamsOpen] = useState(false);
  const [consoleOpen, setConsoleOpen] = useState(false);
  // 右栏（变量/调用栈/断点/结果）默认收起，仅源码常驻
  const [rightOpen, setRightOpen] = useState(false);
  // 左栏（云函数列表 + 入参）：默认展开（用于选函数），可整体收成窄轨，源码占满
  const [leftOpen, setLeftOpen] = useState(true);
  // 云函数列表子区块：默认展开，仅隐藏列表、保留入参区
  const [fnListOpen, setFnListOpen] = useState(true);

  // ── 源码 + 断点 ──
  const [source, setSource] = useState<string[] | null>(null);
  const [sourceFile, setSourceFile] = useState('');
  // 断点：line -> 选项（条件/日志/命中次数/启用）。替代原 Set<number>，支撑条件断点。
  const [breakpoints, setBreakpoints] = useState<Map<number, BpOptions>>(new Map());
  const [currentLine, setCurrentLine] = useState<number | null>(null);

  // ── 会话 / 状态 ──
  const [status, setStatus] = useState<CfDebugStatus>('idle');
  const [sessionId, setSessionId] = useState('');
  const [logs, setLogs] = useState<{ level: string; msg: string }[]>([]);
  const [result, setResult] = useState<string | null>(null);

  // ── DAP 调试信息 ──
  const [frames, setFrames] = useState<DapStackFrame[]>([]);
  const [pausedThreadId, setPausedThreadId] = useState<number>(1);
  const [variables, setVariables] = useState<DapVariable[]>([]);
  const [rightTab, setRightTab] = useState<RightTab>('variables');
  // 求值（右键表达式）用：当前帧 id + 上下文菜单 / 结果弹窗
  const [evalFrameId, setEvalFrameId] = useState<number | null>(null);
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; expr: string; line: number | null } | null>(null);
  const [evalResult, setEvalResult] = useState<{
    x: number; y: number; expr: string; result?: string; type?: string; error?: string;
  } | null>(null);

  const clientRef = useRef<DapClient | null>(null);
  // 断点：line -> 选项。活跃态实时下发用。
  const breakpointsRef = useRef<Map<number, BpOptions>>(new Map());
  // 断点按函数 path 记忆，切换函数不丢失
  const bpByPath = useRef<Map<string, Map<number, BpOptions>>>(new Map());
  // 运行到光标产生的临时断点行（命中即移除，且不持久化到 bpByPath）
  const tempBpRef = useRef<number | null>(null);
  const selectedRef = useRef<CfFunction | null>(null);
  const sessionIdRef = useRef('');
  const resultCollecting = useRef(false);
  const resultBuf = useRef<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);
  const sourceRef = useRef<HTMLDivElement>(null);

  // 异常断点（默认关闭，避免云函数抛错频繁刷屏）
  const [excRaised, setExcRaised] = useState(false);
  const [excUncaught, setExcUncaught] = useState(false);

  // 监视表达式（Watches）：持久化表达式，每次暂停自动求值
  const [watchList, setWatchList] = useState<WatchItem[]>([]);
  const [watchInput, setWatchInput] = useState('');
  const watchListRef = useRef<WatchItem[]>([]);
  const [editingBp, setEditingBp] = useState<number | null>(null);

  // ── 日志管理（服务器 dynamic_log） ──
  const [dynLogs, setDynLogs] = useState<DynLogRecord[]>([]);
  const [dynLogsLoading, setDynLogsLoading] = useState(false);
  const [dynLogsError, setDynLogsError] = useState<string | null>(null);
  const [dynLogsSearch, setDynLogsSearch] = useState('');
  const [dynLogsType, setDynLogsType] = useState('');
  const [dynLogsSelected, setDynLogsSelected] = useState<Set<string | number>>(new Set());
  const [dynLogsDeleting, setDynLogsDeleting] = useState(false);

  useEffect(() => {
    breakpointsRef.current = breakpoints;
  }, [breakpoints]);
  useEffect(() => {
    watchListRef.current = watchList;
  }, [watchList]);

  // 异常断点：运行/暂停期切换时实时下发（初始化时也会在 initialized 里下发一次）
  useEffect(() => {
    const client = clientRef.current;
    if (client && (statusRef.current === 'running' || statusRef.current === 'paused')) {
      const f: string[] = [];
      if (excRaised) f.push('raised');
      if (excUncaught) f.push('uncaught');
      client.setExceptionBreakpoints(f).catch(() => {});
    }
  }, [excRaised, excUncaught]);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  // 最新 status，供 toggleBp 等回调在不重建函数引用的情况下读取
  const statusRef = useRef<CfDebugStatus>('idle');
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // ── 日志写入（含 CLOUD_FUNCTION_RESULT 块捕获） ──
  const appendLog = (level: string, msg: string) => {
    if (resultCollecting.current) {
      if (msg.includes('===== END =====')) {
        resultCollecting.current = false;
        setResult(resultBuf.current.join('\n'));
        resultBuf.current = [];
        return;
      }
      resultBuf.current.push(msg);
      return;
    }
    if (msg.includes('===== CLOUD_FUNCTION_RESULT =====')) {
      resultCollecting.current = true;
      resultBuf.current = [];
      return;
    }
    setLogs((prev) => {
      const next = prev.length > 2000 ? prev.slice(prev.length - 2000) : prev;
      return [...next, { level, msg }];
    });
  };

  // ── SSE 订阅（运行/错误/debug 日志 + 会话结束） ──
  useEffect(() => {
    const offLog = sse.on('cf_debug_log', (d: SSECFDebugLog) => {
      const accept = !sessionIdRef.current || d.session_id === sessionIdRef.current;
      if (accept) appendLog(d.level || 'info', d.msg);
    });
    const offDone = sse.on('cf_debug_done', (d: SSECFDebugDone) => {
      const accept = !sessionIdRef.current || d.session_id === sessionIdRef.current;
      if (accept) setStatus((s) => (s === 'idle' || s === 'connecting' ? s : 'finished'));
    });
    return () => {
      offLog();
      offDone();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  // 暂停时自动滚动源码到当前执行行（标准调试器行为；手动定位按钮见 locateCurrentLine）
  useEffect(() => {
    if (currentLine && sourceRef.current) {
      const el = sourceRef.current.querySelector('.cfd-code-line.current') as HTMLElement | null;
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, [currentLine]);

  // envConfigKeep 用于在异步回调中拿到最新的 root（避免闭包旧值）
  const envConfigKeep = useRef<CfEnvConfig | null>(null);
  useEffect(() => {
    envConfigKeep.current = envConfig;
  }, [envConfig]);

  // ── 加载环境配置（完成后自动扫描一次） ──
  const loadEnv = async () => {
    try {
      const cfg = await cfdebug.getEnv();
      setEnvConfig(cfg);
      envConfigKeep.current = cfg;
      setRootInput(cfg.functions_root);
      void scan(cfg.functions_root || undefined);
    } catch (e: any) {
      addToast(t('cfdebug.envLoadFail', { msg: e.message }), 'error');
    }
  };

  // ── 远程模式：加载 cf_accounts 服务器列表 ──
  const loadAccounts = async () => {
    try {
      const r = await cfdebug.listAccounts();
      setAccounts(r.items || []);
    } catch (e: any) {
      addToast(t('cfdebug.accountsLoadFail', { msg: e.message }), 'error');
    }
  };

  const onModeChange = (m: CfMode) => {
    setMode(m);
    if (m === 'local') {
      setSelectedAccount('');
    } else {
      void loadAccounts();
    }
  };

  // ── 扫描函数 ──
  const scan = async (root?: string) => {
    setScanning(true);
    try {
      const list: CfFunctionList = await cfdebug.listFunctions(root || rootInput || undefined);
      setFunctions(list.functions);
      if (list.root) setRootInput(list.root);
    } catch (e: any) {
      addToast(t('cfdebug.scanFail', { msg: e.message }), 'error');
    } finally {
      setScanning(false);
    }
  };

  useEffect(() => {
    void loadEnv();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 选择函数：加载源码 + 恢复断点 + prefill 参数（显式参数 + **kwargs 源码提取） ──
  const selectFunction = async (f: CfFunction) => {
    setSelected(f);
    // 恢复该函数记忆的断点（没有则空）
    setBreakpoints(bpByPath.current.get(f.path) ?? new Map());
    setCurrentLine(null);

    // 加载源码（完成后从中提取 **kwargs 函数的预期参数）
    let sourceLines: string[] | null = null;
    try {
      const s: CfSource = await cfdebug.getSource(f.path);
      if (s.ok) {
        sourceLines = s.lines;
        setSource(s.lines);
        setSourceFile(s.file);
      } else {
        addToast(t('cfdebug.sourceFail', { msg: s.error || '' }), 'error');
      }
    } catch (e: any) {
      addToast(t('cfdebug.sourceFail', { msg: e.message }), 'error');
    }

    // 预填参数骨架：显式参数 + **kwargs 源码提取
    const seed: Record<string, string> = {};
    f.params.forEach((p) => {
      if (!p.startsWith('*') && !p.startsWith('**')) seed[p] = '';
    });
    if (sourceLines) {
      extractKwargsFromSource(sourceLines).forEach((k) => {
        if (!(k in seed)) seed[k] = '';
      });
    }
    setKwargsInput(Object.keys(seed).length ? JSON.stringify(seed, null, 2) : '{}');

    // **kwargs 函数：默认展开入参面板，方便用户立即看到并填写预期参数
    if (f.params.some((p) => p.startsWith('*'))) {
      setParamsOpen(true);
    }
  };

  // 统一下发断点：更新 state + 记忆到 path（排除临时运行到光标断点）+ 运行期实时下发 DAP
  const pushBreakpoints = useCallback((next: Map<number, BpOptions>) => {
    const f = selectedRef.current;
    if (f) {
      const persist = new Map(next);
      if (tempBpRef.current != null) persist.delete(tempBpRef.current);
      bpByPath.current.set(f.path, persist);
    }
    const st = statusRef.current;
    if ((st === 'running' || st === 'paused') && f && clientRef.current) {
      clientRef.current.setBreakpoints(f.path, bpListForDap(next)).catch(() => {});
    }
    setBreakpoints(next);
  }, []);

  const toggleBp = useCallback((ln: number) => {
    setBreakpoints((prev) => {
      const next = new Map(prev);
      if (next.has(ln)) next.delete(ln);
      else next.set(ln, { enabled: true });
      pushBreakpoints(next);
      return next;
    });
  }, [pushBreakpoints]);

  // 保存某行的断点选项（条件/日志/命中次数/启用）
  const saveBp = useCallback((ln: number, opts: BpOptions) => {
    setBreakpoints((prev) => {
      const next = new Map(prev);
      next.set(ln, opts);
      pushBreakpoints(next);
      return next;
    });
    setEditingBp(null);
  }, [pushBreakpoints]);

  const removeBp = useCallback((ln: number) => {
    setBreakpoints((prev) => {
      const next = new Map(prev);
      next.delete(ln);
      if (tempBpRef.current === ln) tempBpRef.current = null;
      pushBreakpoints(next);
      return next;
    });
    setEditingBp(null);
  }, [pushBreakpoints]);

  // 运行到光标：在该行设临时断点（不持久化）并继续，命中即移除
  const runToCursor = useCallback(async (ln: number) => {
    const client = clientRef.current;
    const f = selectedRef.current;
    setCtxMenu(null);
    if (!client || !f) return;
    const cur = breakpointsRef.current;
    let next = new Map(cur);
    const isTemp = !next.has(ln);
    if (isTemp) {
      next.set(ln, { enabled: true });
      tempBpRef.current = ln;
      pushBreakpoints(next);
    }
    try {
      await client.continue(pausedThreadId);
    } catch (e: any) {
      appendLog('error', `[dap] runToCursor 失败: ${e.message}`);
    }
  }, [pausedThreadId, pushBreakpoints]);

  // 监视表达式：每次暂停（或切帧）时对所有表达式在当前帧作用域求值
  const evaluateWatches = useCallback(async (frameId: number | null) => {
    if (frameId == null) return;
    const client = clientRef.current;
    if (!client) return;
    const list = watchListRef.current;
    if (list.length === 0) return;
    try {
      const updated = await Promise.all(
        list.map(async (w) => {
          if (!w.expr.trim()) return w;
          try {
            const res = await client.evaluate(w.expr, frameId, 'repl');
            const body = res?.body || {};
            return { ...w, result: body.result, type: body.type, error: undefined };
          } catch (e: any) {
            return { ...w, result: undefined, error: e?.message || String(e) };
          }
        }),
      );
      setWatchList(updated);
    } catch {
      /* ignore */
    }
  }, []);

  const addWatch = useCallback(() => {
    const expr = watchInput.trim();
    if (!expr) return;
    setWatchList((prev) => [...prev, { expr }]);
    setWatchInput('');
    if (evalFrameId != null) void evaluateWatches(evalFrameId);
  }, [watchInput, evalFrameId, evaluateWatches]);

  const updateWatch = useCallback((idx: number, expr: string) => {
    setWatchList((prev) => prev.map((w, i) => (i === idx ? { ...w, expr } : w)));
    if (evalFrameId != null) void evaluateWatches(evalFrameId);
  }, [evalFrameId, evaluateWatches]);

  const removeWatch = useCallback((idx: number) => {
    setWatchList((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  // ── 日志管理：拉取 / 删除服务器 dynamic_log ──
  const fetchDynLogs = useCallback(async () => {
    setDynLogsLoading(true);
    setDynLogsError(null);
    try {
      const r = await cfdebug.listDynamicLogs({
        env: envConfig?.current_env,
        log_type: dynLogsType.trim() || undefined,
        search: dynLogsSearch.trim() || undefined,
        page_size: 100,
      });
      if (r.ok) {
        setDynLogs(r.records || []);
        // 清理已不存在的勾选
        const ids = new Set((r.records || []).map((x) => x.id_));
        setDynLogsSelected((prev) => new Set(Array.from(prev).filter((id) => ids.has(id))));
      } else {
        setDynLogsError(r.error || t('cfdebug.logLoadFail'));
      }
    } catch (e: any) {
      setDynLogsError(e?.message || String(e));
    } finally {
      setDynLogsLoading(false);
    }
  }, [envConfig?.current_env, dynLogsType, dynLogsSearch, t]);

  const toggleDynLogSelected = useCallback((id: string | number) => {
    setDynLogsSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);

  const toggleDynLogSelectAll = useCallback(() => {
    setDynLogsSelected((prev) => {
      if (prev.size === dynLogs.length) return new Set();
      return new Set(dynLogs.map((r) => r.id_));
    });
  }, [dynLogs]);

  const deleteDynLogs = useCallback(async (ids: Array<string | number>) => {
    if (ids.length === 0) return;
    if (!window.confirm(t('cfdebug.logConfirmDelete', { n: ids.length }))) return;
    setDynLogsDeleting(true);
    setDynLogsError(null);
    try {
      const r = await cfdebug.deleteDynamicLogs({ ids, env: envConfig?.current_env });
      if (!r.ok) {
        setDynLogsError(r.error || t('cfdebug.logDeleteFail'));
      } else if (r.failed?.length) {
        setDynLogsError(t('cfdebug.logPartialFail', {
          ok: r.deleted, total: r.total,
          first: r.failed.slice(0, 3).map((f) => `${f.id}: ${f.error}`).join('; '),
        }));
      }
      await fetchDynLogs();
      setDynLogsSelected(new Set());
    } catch (e: any) {
      setDynLogsError(e?.message || String(e));
    } finally {
      setDynLogsDeleting(false);
    }
  }, [envConfig?.current_env, fetchDynLogs, t]);

  // 一键定位到当前执行行（暂停时高亮的 current 行）
  const locateCurrentLine = () => {
    if (!currentLine || !sourceRef.current) return;
    const container = sourceRef.current;
    const el =
      (container.children[currentLine - 1] as HTMLElement | null) ||
      (container.querySelector('.cfd-code-line.current') as HTMLElement | null);
    if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };

  // ── DAP：取某帧作用域与变量 ──
  const loadFrameVariables = async (frameId: number) => {
    const client = clientRef.current;
    if (!client) return;
    try {
      const sc: { body?: { scopes?: DapScope[] } } = await client.scopes(frameId);
      const scopes = sc.body?.scopes || [];
      const localScope =
        scopes.find((s) => /local/i.test(s.name)) || scopes[0];
      if (localScope && localScope.variablesReference > 0) {
        const vr: { body?: { variables?: DapVariable[] } } = await client.variables(
          localScope.variablesReference,
        );
        setVariables(vr.body?.variables || []);
      } else {
        setVariables([]);
      }
    } catch (e: any) {
      appendLog('error', `[dap] 取变量失败: ${e.message}`);
    }
  };

  const handleStopped = async (body: DapStoppedBody) => {
    const client = clientRef.current;
    if (!client) return;
    const tid = body.threadId ?? 1;
    setPausedThreadId(tid);
    setStatus('paused');
    setCurrentLine(null);
    try {
      const th = await client.threads();
      const threads: { id: number }[] = th.body?.threads || [];
      const useTid = threads[0]?.id ?? tid;
      const st = await client.stackTrace(useTid);
      const fr: DapStackFrame[] = st.body?.stackFrames || [];
      setFrames(fr);
      setEvalFrameId(fr[0]?.id ?? null);
      if (fr[0]) {
        setCurrentLine(fr[0].line);
        await loadFrameVariables(fr[0].id);
      }

      // 异常断点命中：取异常详情并打到控制台
      if (body.reason === 'exception') {
        try {
          const ei = await client.exceptionInfo(tid);
          const b = ei?.body || {};
          appendLog('error', `[${t('cfdebug.excInfo')}] ${b.exceptionId || ''}: ${b.description || b.formattedDescription || ''}`);
        } catch {
          /* ignore */
        }
      }

      // 运行到光标的临时断点：命中即移除（不再持久化）
      if (tempBpRef.current != null && fr[0] && fr[0].line === tempBpRef.current) {
        const ln = tempBpRef.current;
        tempBpRef.current = null;
        setBreakpoints((prev) => {
          const n2 = new Map(prev);
          n2.delete(ln);
          const f = selectedRef.current;
          if (f && clientRef.current) clientRef.current.setBreakpoints(f.path, bpListForDap(n2)).catch(() => {});
          return n2;
        });
      }

      // 所有监视表达式在当前帧作用域求值（每次暂停自动刷新）
      await evaluateWatches(fr[0]?.id ?? null);

      // 命中断点自动展开右侧调试信息栏，并切到变量页（符合调试器直觉）
      setRightOpen(true);
      setRightTab('variables');
    } catch (e: any) {
      appendLog('error', `[dap] 取调用栈失败: ${e.message}`);
    }
  };

  const handleFinished = () => {
    setStatus('finished');
    setCurrentLine(null);
    setEvalFrameId(null);
    setCtxMenu(null);
    setEvalResult(null);
    clientRef.current?.close();
    clientRef.current = null;
  };

  // ── 启动调试 ──
  const startDebug = async () => {
    const f = selectedRef.current;
    if (!f) {
      addToast(t('cfdebug.pickFirst'), 'warn');
      return;
    }
    let kwargsObj: any;
    try {
      kwargsObj = JSON.parse(kwargsInput || '{}');
      if (typeof kwargsObj !== 'object' || kwargsObj === null) throw new Error('must be object');
    } catch (e: any) {
      addToast(t('cfdebug.kwargsInvalid'), 'error');
      return;
    }

    setStatus('connecting');
    setLogs([]);
    setResult(null);
    setCurrentLine(null);
    setFrames([]);
    setVariables([]);
    tempBpRef.current = null;
    setWatchList((prev) => prev.map((w) => ({ expr: w.expr })));
    resultCollecting.current = false;
    resultBuf.current = [];

    try {
      const req: any = {
        file: f.path,
        root: rootInput || undefined,
        kwargs: JSON.stringify(kwargsObj),
        env: mode === 'local' ? 'mock' : 'test',
        entry: f.entry,
        debug_id: debugIdInput || undefined,
        db_url: dbUrlInput || undefined,
        allow_ddl: allowDdl,
        db_save: dbSave,
        write_real: writeReal,
      };
      if (mode === 'remote') {
        // 远程模式：后端按 cf_accounts 登录取 token（密码不出前端）
        if (!selectedAccount) {
          addToast(t('cfdebug.pickAccount'), 'warn');
          return;
        }
        req.server_account = selectedAccount;
      }
      const res = await cfdebug.run(req);
      if (!res.ok) {
        addToast(t('cfdebug.runFail', { msg: res.error || '' }), 'error');
        setStatus('error');
        return;
      }
      setSessionId(res.session_id);

      const client = new DapClient();
      clientRef.current = client;
      client.setOnClose(handleFinished);

      client.on('initialized', async () => {
        try {
          await client.setBreakpoints(f.path, bpListForDap(breakpointsRef.current));
          await client.configurationDone();
          // 异常断点（默认关闭；用户开启后才下发）
          const ef: string[] = [];
          if (excRaised) ef.push('raised');
          if (excUncaught) ef.push('uncaught');
          if (ef.length) await client.setExceptionBreakpoints(ef).catch(() => {});
        } catch (e: any) {
          appendLog('error', `[dap] 断点配置失败: ${e.message}`);
        }
      });
      client.on('stopped', (b: DapStoppedBody) => void handleStopped(b));
      client.on('terminated', handleFinished);
      client.on('exited', handleFinished);
      client.on('output', (b: DapOutputBody) => {
        const out = b.output || '';
        appendLog('debug', `[dap:${b.category || 'out'}] ${out}`);
      });

      const wsUrl = `${
        location.protocol === 'https:' ? 'wss://' : 'ws://'
      }${location.host}${res.ws_url}`;
      await client.connect(wsUrl);
      await client.initialize();
      void client.attach(res.dap_host, res.dap_port); // 不 await
      setStatus('running');
    } catch (e: any) {
      addToast(t('cfdebug.runFail', { msg: e.message }), 'error');
      setStatus('error');
      clientRef.current?.close();
      clientRef.current = null;
    }
  };

  // ── 停止调试 ──
  const stopDebug = async () => {
    const sid = sessionIdRef.current;
    if (sid) {
      try {
        await cfdebug.stop(sid);
      } catch {
        /* ignore */
      }
    }
    clientRef.current?.close();
    clientRef.current = null;
    setStatus('finished');
    setCurrentLine(null);
    setEvalFrameId(null);
    setCtxMenu(null);
    setEvalResult(null);
    setSessionId('');
    sessionIdRef.current = '';
  };

  // ── 单步控制 ──
  const step = async (kind: 'next' | 'stepIn' | 'stepOut' | 'continue' | 'pause' | 'stepBack') => {
    const client = clientRef.current;
    if (!client) return;
    setCurrentLine(null);
    try {
      if (kind === 'continue') await client.continue(pausedThreadId);
      else if (kind === 'next') await client.next(pausedThreadId);
      else if (kind === 'stepIn') await client.stepIn(pausedThreadId);
      else if (kind === 'stepOut') await client.stepOut(pausedThreadId);
      else if (kind === 'stepBack') await client.stepBack(pausedThreadId);
      else if (kind === 'pause') await client.pause(pausedThreadId);
    } catch (e: any) {
      appendLog('error', `[dap] ${kind} 失败: ${e.message}`);
    }
  };

  const selectFrame = async (frame: DapStackFrame) => {
    setCurrentLine(frame.line);
    setRightTab('variables');
    setEvalFrameId(frame.id);
    await loadFrameVariables(frame.id);
    await evaluateWatches(frame.id);
  };

  // 关闭右键菜单 / 求值弹窗（点遮罩时）
  const closeOverlays = () => {
    setCtxMenu(null);
    setEvalResult(null);
  };

  // 源码区右键：暂停态下根据「选区」或「光标下词」弹出求值菜单；并支持「运行到光标」
  const onSourceContextMenu = (e: ReactMouseEvent) => {
    if (status !== 'paused' || evalFrameId == null) return; // 仅暂停时可操作
    e.preventDefault();
    const el = (e.target as HTMLElement).closest('.cfd-code-line');
    const line = el ? Number((el as HTMLElement).getAttribute('data-ln')) : null;
    const sel = (window.getSelection()?.toString() || '').trim();
    const expr = sel || getWordAtPoint(e.clientX, e.clientY);
    setCtxMenu({ x: e.clientX, y: e.clientY, expr, line });
  };

  // 执行 DAP evaluate 并在光标附近弹出结果
  const doEvaluate = async (expr: string, x: number, y: number) => {
    setCtxMenu(null);
    const client = clientRef.current;
    if (!client || evalFrameId == null) {
      setEvalResult({ x, y, expr, error: t('cfdebug.evalNoFrame') });
      return;
    }
    try {
      const res = await client.evaluate(expr, evalFrameId, 'repl');
      const body = res?.body || {};
      setEvalResult({ x, y, expr, result: body.result, type: body.type });
    } catch (err: any) {
      setEvalResult({ x, y, expr, error: err?.message || String(err) });
    }
  };

  const filtered = functions.filter(
    (f) =>
      !search ||
      f.name.toLowerCase().includes(search.toLowerCase()) ||
      f.path.toLowerCase().includes(search.toLowerCase()),
  );

  const statusLabel: Record<CfDebugStatus, string> = {
    idle: t('cfdebug.status.idle'),
    connecting: t('cfdebug.status.connecting'),
    running: t('cfdebug.status.running'),
    paused: t('cfdebug.status.paused'),
    finished: t('cfdebug.status.finished'),
    error: t('cfdebug.status.error'),
  };

  const saveEnvConfig = async (patch: Partial<CfEnvConfig>) => {
    try {
      const cfg = await cfdebug.saveEnv(patch);
      setEnvConfig(cfg);
    } catch (e: any) {
      addToast(t('cfdebug.envSaveFail', { msg: e.message }), 'error');
    }
  };

  const onRootCommit = () => {
    saveEnvConfig({ functions_root: rootInput });
    void scan(rootInput);
  };

  const busy = status === 'running' || status === 'connecting' || status === 'paused';

  return (
    <section className="cfdebug-panel">
      {/* 顶部工具栏 */}
      <div className="cfdebug-toolbar">
        <div className="cfdebug-mode">
          <button
            className={`seg ${mode === 'local' ? 'active' : ''}`}
            onClick={() => onModeChange('local')}
          >
            {t('cfdebug.modeLocal')}
          </button>
          <button
            className={`seg ${mode === 'remote' ? 'active' : ''}`}
            onClick={() => onModeChange('remote')}
          >
            {t('cfdebug.modeRemote')}
          </button>
        </div>
        <div className="cfdebug-root">
          <input
            className="input"
            placeholder={t('cfdebug.rootPlaceholder')}
            value={rootInput}
            onChange={(e) => setRootInput(e.target.value)}
            onBlur={onRootCommit}
          />
          <button className="btn btn-sm" onClick={() => void scan(rootInput)} disabled={scanning}>
            {scanning ? t('cfdebug.scanning') : t('cfdebug.scan')}
          </button>
        </div>
        <div className="spacer" />
        <button className="btn btn-sm btn-ghost" onClick={() => setRightOpen((o) => !o)} title={t('cfdebug.debugInfo')}>
          {rightOpen ? '▾' : '▸'} {t('cfdebug.debugInfo')}
        </button>
        {mode === 'remote' && (
          <label className="cfdebug-server-inline">
              <select
              className="sel"
              value={selectedAccount}
              onChange={(e) => {
                setSelectedAccount(e.target.value);
                if (e.target.value) void scan(rootInput);
              }}
            >
              <option value="">{t('cfdebug.pickAccount')}</option>
              {accounts.map((a) => (
                <option key={a.index} value={a.server_url || a.name}>
                  {a.name} · {a.server_url}
                </option>
              ))}
            </select>
          </label>
        )}
        <button className="btn btn-sm btn-ghost" onClick={() => setSyncOpen(true)}>
          ⚙ {t('cfdebug.sync')}
        </button>
        {busy ? (
          <button className="btn btn-sm btn-danger" onClick={() => void stopDebug()}>
            ⏹ {t('cfdebug.stop')}
          </button>
        ) : (
          <button
            className="btn btn-sm btn-primary"
            onClick={() => void startDebug()}
            disabled={!selected || (mode === 'remote' && !selectedAccount)}
          >
            ▶ {t('cfdebug.run')}
          </button>
        )}
        <span className={`cfdebug-status cfd-${status}`}>{statusLabel[status]}</span>
      </div>

      {/* 主体三栏 */}
      <div className={`cfdebug-body${rightOpen ? '' : ' right-collapsed'}${leftOpen ? '' : ' left-collapsed'}`}>
        {/* 左：函数列表 + 参数 */}
        <div className="cfdebug-left">
          {leftOpen ? (
            <>
              <div className="cfdebug-left-head">
                <button type="button" className="cfdebug-collapser" onClick={() => setFnListOpen((o) => !o)}>
                  <span className="cfd-caret">{fnListOpen ? '▾' : '▸'}</span>
                  <span className="section-title">{t('cfdebug.functionList')}</span>
                </button>
                <button
                  type="button"
                  className="cfdebug-panel-toggle"
                  title={t('cfdebug.leftPanelCollapse')}
                  aria-label={t('cfdebug.leftPanelCollapse')}
                  onClick={() => {
                    setLeftOpen(false);
                    setRightOpen(true);
                  }}
                >
                  ⟨
                </button>
              </div>
          {fnListOpen && (
            <>
              <div className="cfdebug-search">
                <input
                  className="input"
                  placeholder={t('cfdebug.searchPlaceholder')}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              <div className="cfdebug-fnlist">
            {filtered.length === 0 ? (
              <div className="empty-hint">{t('cfdebug.noFunc')}</div>
            ) : (
              filtered.map((f) => (
                <div
                  key={f.path}
                  className={`cfdebug-fn ${selected?.path === f.path ? 'selected' : ''}`}
                  onClick={() => void selectFunction(f)}
                  title={f.path}
                >
                  <div className="cfdebug-fn-name">{f.name}</div>
                  <div className="cfdebug-fn-meta">
                    <span className={`cfd-model m${f.model}`}>模型{f.model}</span>
                    {f.params.length > 0 && (
                      <span className="cfdebug-fn-params">{f.params.join(', ')}</span>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
          </>
          )}

          {mode === 'remote' && selectedAccount && (
            <div className="cfdebug-remote-banner">
              {t('cfdebug.remoteDataHint', {
                server: accounts.find((a) => (a.server_url || a.name) === selectedAccount)?.name || selectedAccount,
                root: rootInput || t('cfdebug.rootPlaceholder'),
              })}
            </div>
          )}

          <div className="cfdebug-params">
            <button type="button" className="cfdebug-collapser" onClick={() => setParamsOpen((o) => !o)}>
              <span className="cfd-caret">{paramsOpen ? '▾' : '▸'}</span>
              <span className="section-title">{t('cfdebug.params')}</span>
            </button>
            {paramsOpen && (
              selected ? (
                <>
                  <textarea
                    className="cfdebug-kwargs"
                    spellCheck={false}
                    value={kwargsInput}
                    onChange={(e) => setKwargsInput(e.target.value)}
                  />
                  <details className="cfdebug-adv" open={advOpen} onToggle={(e) => setAdvOpen((e.target as any).open)}>
                    <summary>{t('cfdebug.advanced')}</summary>
                    <div className="cfdebug-adv-fields">
                      <label className="field-col">
                        <span>{t('cfdebug.debugId')}</span>
                        <input className="input" value={debugIdInput} onChange={(e) => setDebugIdInput(e.target.value)} />
                      </label>
                      <label className="field-col">
                        <span>{t('cfdebug.dbUrl')}</span>
                        <input className="input" value={dbUrlInput} onChange={(e) => setDbUrlInput(e.target.value)} />
                      </label>
                      <label className="chk">
                        <input type="checkbox" checked={allowDdl} onChange={(e) => setAllowDdl(e.target.checked)} />
                        {t('cfdebug.allowDdl')}
                      </label>
                      <label className="chk">
                        <input type="checkbox" checked={dbSave} onChange={(e) => setDbSave(e.target.checked)} />
                        {t('cfdebug.dbSave')}
                      </label>
                      <label className="chk">
                        <input type="checkbox" checked={writeReal} onChange={(e) => setWriteReal(e.target.checked)} />
                        {t('cfdebug.writeReal')}
                      </label>
                    </div>
                  </details>
                </>
              ) : (
                <div className="empty-hint">{t('cfdebug.pickFirst')}</div>
              )
            )}
          </div>
          </>) : (
            <button
              type="button"
              className="cfdebug-left-rail"
              title={t('cfdebug.leftPanelExpand')}
              aria-label={t('cfdebug.leftPanelExpand')}
              onClick={() => setLeftOpen(true)}
            >
              ▶
            </button>
          )}
        </div>

        {/* 中：源码 + 断点 */}
        <div className="cfdebug-center">
          <div className="cfdebug-source-head">
            <span className="cfdebug-src-name" title={sourceFile}>
              {sourceFile ? sourceFile.split('/').pop() : t('cfdebug.noSource')}
            </span>
            <span className="cfdebug-src-path">{sourceFile}</span>
            <div className="spacer" />
            <div className="cfdebug-stepbar">
            <button className="btn btn-xs" disabled={status !== 'paused'} title={t('cfdebug.continue')} onClick={() => void step('continue')}>
              ▶ {t('cfdebug.continue')}
            </button>
            {status === 'running' && (
              <button className="btn btn-xs" title={t('cfdebug.pause')} onClick={() => void step('pause')}>
                ⏸ {t('cfdebug.pause')}
              </button>
            )}
            <button className="btn btn-xs" disabled={status !== 'paused'} title={t('cfdebug.stepNext')} onClick={() => void step('next')}>
              ⤼ {t('cfdebug.stepNext')}
            </button>
            <button className="btn btn-xs" disabled={status !== 'paused'} title={t('cfdebug.stepIn')} onClick={() => void step('stepIn')}>
              ↓ {t('cfdebug.stepIn')}
            </button>
            <button className="btn btn-xs" disabled={status !== 'paused'} title={t('cfdebug.stepOut')} onClick={() => void step('stepOut')}>
              ↑ {t('cfdebug.stepOut')}
            </button>
            <button className="btn btn-xs" disabled title={t('cfdebug.stepBackHint')} onClick={() => void step('stepBack')}>
              ↶ {t('cfdebug.stepBack')}
            </button>
          </div>
          <button
            className="btn btn-xs btn-locate"
            disabled={!currentLine}
            title={t('cfdebug.locateLine')}
            onClick={locateCurrentLine}
          >
            ⊙ {t('cfdebug.locateLine')}
          </button>
          </div>
          <div className="cfdebug-source" ref={sourceRef} onContextMenu={onSourceContextMenu}>
            {source ? (
              <SourceView source={source} breakpoints={breakpoints} currentLine={currentLine} onToggleBp={toggleBp} />
            ) : (
              <div className="empty-hint">{selected ? t('cfdebug.sourceLoading') : t('cfdebug.pickFirst')}</div>
            )}
          </div>
        </div>

        {/* 右：变量 / 调用栈 / 断点 / 结果（默认收起，源码常驻） */}
        <div className="cfdebug-right">
          <button type="button" className="cfdebug-right-toggle" onClick={() => setRightOpen((o) => !o)} title={t('cfdebug.debugInfo')}>
            <span className="cfd-caret">{rightOpen ? '▾' : '▸'}</span>
            {rightOpen && <span className="section-title">{t('cfdebug.debugInfo')}</span>}
          </button>
          {rightOpen && (
          <>
          <div className="cfdebug-subtabs">
            <button className={`cfdebug-subtab ${rightTab === 'variables' ? 'active' : ''}`} onClick={() => setRightTab('variables')}>
              {t('cfdebug.tabVariables')}
            </button>
            <button className={`cfdebug-subtab ${rightTab === 'stack' ? 'active' : ''}`} onClick={() => setRightTab('stack')}>
              {t('cfdebug.tabStack')}
            </button>
            <button className={`cfdebug-subtab ${rightTab === 'breakpoints' ? 'active' : ''}`} onClick={() => setRightTab('breakpoints')}>
              {t('cfdebug.tabBreakpoints')}
            </button>
            <button className={`cfdebug-subtab ${rightTab === 'watches' ? 'active' : ''}`} onClick={() => setRightTab('watches')}>
              {t('cfdebug.tabWatches')}
            </button>
            <button className={`cfdebug-subtab ${rightTab === 'logs' ? 'active' : ''}`} onClick={() => {
              setRightTab('logs');
              if (dynLogs.length === 0 && !dynLogsLoading) void fetchDynLogs();
            }}>
              {t('cfdebug.tabLogs')}
            </button>
            <button className={`cfdebug-subtab ${rightTab === 'result' ? 'active' : ''}`} onClick={() => setRightTab('result')}>
              {t('cfdebug.tabResult')}
            </button>
          </div>

          <div className="cfdebug-subbody">
            {rightTab === 'variables' && (
              <div className="cfdebug-vars">
                {variables.length === 0 ? (
                  <div className="empty-hint">{t('cfdebug.noVars')}</div>
                ) : (
                  variables.map((v, i) => (
                    <VarNode key={`${v.name}-${i}`} node={v} client={clientRef.current} />
                  ))
                )}
              </div>
            )}
            {rightTab === 'stack' && (
              <div className="cfdebug-stack">
                {frames.length === 0 ? (
                  <div className="empty-hint">{t('cfdebug.noStack')}</div>
                ) : (
                  frames.map((fr, i) => (
                    <div
                      key={fr.id}
                      className="cfdebug-frame"
                      onClick={() => void selectFrame(fr)}
                      title={fr.source?.path}
                    >
                      <span className="cfdebug-frame-idx">{i}</span>
                      <span className="cfdebug-frame-name">{fr.name}</span>
                      <span className="cfdebug-frame-loc">
                        {fr.source?.path?.split('/').pop()}:{fr.line}
                      </span>
                    </div>
                  ))
                )}
              </div>
            )}
            {rightTab === 'breakpoints' && (
              <div className="cfdebug-bplist">
                {/* 异常断点控制 */}
                <div className="cfdebug-exc">
                  <div className="section-title">{t('cfdebug.excBreakpoints')}</div>
                  <label className="chk">
                    <input type="checkbox" checked={excRaised} onChange={() => setExcRaised((v) => !v)} />
                    {t('cfdebug.excRaised')}
                  </label>
                  <label className="chk">
                    <input type="checkbox" checked={excUncaught} onChange={() => setExcUncaught((v) => !v)} />
                    {t('cfdebug.excUncaught')}
                  </label>
                  <div className="cfd-hint">{t('cfdebug.excBreakHint')}</div>
                </div>

                {breakpoints.size === 0 ? (
                  <div className="empty-hint">{t('cfdebug.noBp')}</div>
                ) : (
                  Array.from(breakpoints)
                    .sort((a, b) => a[0] - b[0])
                    .map(([ln, o]) => {
                      const kind = o.logMessage ? 'log' : o.condition || o.hitCondition ? 'cond' : 'normal';
                      const badge = o.logMessage
                        ? t('cfdebug.bpLogBadge')
                        : o.condition || o.hitCondition
                        ? t('cfdebug.bpCondBadge')
                        : '';
                      return (
                        <div key={ln} className="cfdebug-bp-row">
                          <span className={`cfdebug-bp-dot ${kind === 'log' ? 'log' : kind === 'cond' ? 'cond' : ''}`}>
                            {kind === 'log' ? '≋' : kind === 'cond' ? '◆' : '●'}
                          </span>
                          <span className="cfdebug-bp-file">{sourceFile.split('/').pop()}</span>
                          <span className="cfdebug-bp-line">:{ln}</span>
                          {badge && <span className="cfdebug-bp-badge">{badge}</span>}
                          <span className="cfdebug-bp-opt" title={o.condition || o.hitCondition || o.logMessage || ''}>
                            {o.condition ? `if ${o.condition}` : o.hitCondition ? `@${o.hitCondition}` : o.logMessage || ''}
                          </span>
                          <span className="cfdebug-bp-actions">
                            <button type="button" className="cfd-bp-mini" onClick={() => setEditingBp(editingBp === ln ? null : ln)} title={t('cfdebug.bpOptions')}>
                              ✎
                            </button>
                            <button type="button" className="cfd-bp-mini" onClick={() => removeBp(ln)} title={t('cfdebug.bpDelete')}>
                              ×
                            </button>
                          </span>
                          {editingBp === ln && (
                            <BpEditor
                              bp={o}
                              onApply={(opts) => saveBp(ln, opts)}
                              onDelete={() => removeBp(ln)}
                            />
                          )}
                        </div>
                      );
                    })
                )}
              </div>
            )}
            {rightTab === 'watches' && (
              <div className="cfdebug-watches">
                <div className="cfdebug-watch-add">
                  <input
                    className="input"
                    placeholder={t('cfdebug.watchPlaceholder')}
                    value={watchInput}
                    onChange={(e) => setWatchInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') addWatch();
                    }}
                  />
                  <button className="btn btn-xs" onClick={addWatch} disabled={!watchInput.trim()}>
                    +
                  </button>
                </div>
                {watchList.length === 0 ? (
                  <div className="empty-hint">{t('cfdebug.noWatches')}</div>
                ) : (
                  watchList.map((w, i) => (
                    <div key={i} className="cfdebug-watch-row">
                      <input
                        className="cfdebug-watch-expr"
                        value={w.expr}
                        spellCheck={false}
                        onChange={(e) => updateWatch(i, e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                        }}
                      />
                      <button type="button" className="cfd-bp-mini" onClick={() => removeWatch(i)} title={t('cfdebug.bpDelete')}>
                        ×
                      </button>
                      <div className="cfdebug-watch-val">
                        {w.error ? (
                          <span className="cfd-eval-error">{w.error}</span>
                        ) : (
                          <>
                            <span className="cfd-eval-val">{w.result ?? '—'}</span>
                            {w.type && <span className="cfd-eval-type">{w.type}</span>}
                          </>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
            {rightTab === 'result' && (
              <pre className="cfdebug-result">
                {result ?? <span className="empty-hint">{t('cfdebug.noResult')}</span>}
              </pre>
            )}
            {rightTab === 'logs' && (
              <div className="cfdebug-dynlogs">
                <div className="cfdebug-dynlogs-toolbar">
                  <input
                    className="input input-sm"
                    placeholder={t('cfdebug.logTypeFilter')}
                    value={dynLogsType}
                    onChange={(e) => setDynLogsType(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void fetchDynLogs(); }}
                    title={t('cfdebug.logTypeHint')}
                  />
                  <input
                    className="input input-sm"
                    placeholder={t('cfdebug.logSearch')}
                    value={dynLogsSearch}
                    onChange={(e) => setDynLogsSearch(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void fetchDynLogs(); }}
                  />
                  <button className="btn btn-sm" onClick={() => void fetchDynLogs()} disabled={dynLogsLoading}>
                    {dynLogsLoading ? t('cfdebug.loading') : t('cfdebug.refresh')}
                  </button>
                  <button
                    className="btn btn-sm btn-danger"
                    onClick={() => void deleteDynLogs(Array.from(dynLogsSelected))}
                    disabled={dynLogsSelected.size === 0 || dynLogsDeleting}
                    title={t('cfdebug.deleteSelectedHint')}
                  >
                    {dynLogsDeleting ? t('cfdebug.deleting') : t('cfdebug.deleteSelected', { n: dynLogsSelected.size })}
                  </button>
                </div>
                {dynLogsError && <div className="cfdebug-dynlogs-error">{dynLogsError}</div>}
                <div className="cfdebug-dynlogs-head">
                  <input
                    type="checkbox"
                    checked={dynLogs.length > 0 && dynLogsSelected.size === dynLogs.length}
                    onChange={toggleDynLogSelectAll}
                    title={t('cfdebug.selectAll')}
                  />
                  <span className="col-type">{t('cfdebug.colType')}</span>
                  <span className="col-content">{t('cfdebug.colContent')}</span>
                  <span className="col-time">{t('cfdebug.colTime')}</span>
                </div>
                {dynLogsLoading && dynLogs.length === 0 ? (
                  <div className="empty-hint">{t('cfdebug.loading')}</div>
                ) : dynLogs.length === 0 ? (
                  <div className="empty-hint">{t('cfdebug.noLogs')}</div>
                ) : (
                  dynLogs.map((r) => (
                    <div key={String(r.id_)} className="cfdebug-dynlogs-row">
                      <input
                        type="checkbox"
                        checked={dynLogsSelected.has(r.id_)}
                        onChange={() => toggleDynLogSelected(r.id_)}
                      />
                      <span className="col-type" title={r.log_type}>{r.log_type || '?'}</span>
                      <span className="col-content" title={r.content}>{r.content || ''}</span>
                      <span className="col-time">{r.create_date || r.create_time || ''}</span>
                      <button
                        className="cfd-bp-mini"
                        onClick={() => void deleteDynLogs([r.id_])}
                        title={t('cfdebug.deleteOne')}
                      >×</button>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
          </>
          )}
        </div>
      </div>
      <div className={`cfdebug-console${consoleOpen ? ' is-open' : ''}`}>
        <div className="cfdebug-console-head">
          <button type="button" className="cfdebug-collapser" onClick={() => setConsoleOpen((o) => !o)}>
            <span className="cfd-caret">{consoleOpen ? '▾' : '▸'}</span>
            <span className="section-title">{t('cfdebug.console')}</span>
          </button>
          <div className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={() => setLogs([])} disabled={logs.length === 0}>
            {t('cfdebug.clear')}
          </button>
        </div>
        {consoleOpen && (
          <div className="cfdebug-console-body" ref={logRef}>
            {logs.length === 0 ? (
              <div className="empty-hint">{t('cfdebug.consoleEmpty')}</div>
            ) : (
              logs.map((l, i) => (
                <div key={i} className={`cfd-log-line ${l.level}`}>
                  {l.msg}
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {syncOpen && (
        <CfDebugSyncModal
          open={syncOpen}
          onClose={() => setSyncOpen(false)}
          onChanged={() => {
            if (mode === 'remote') void loadAccounts();
          }}
        />
      )}

      {/* 右键求值：遮罩 + 上下文菜单 + 结果弹窗（fixed 定位在光标处） */}
      {(ctxMenu || evalResult) && (
        <div className="cfd-overlay" onMouseDown={closeOverlays} />
      )}
      {ctxMenu && (
        <div className="cfd-ctx-menu" style={{ left: ctxMenu.x, top: ctxMenu.y }}>
          {ctxMenu.expr && (
            <>
              <div className="cfd-ctx-expr" title={ctxMenu.expr}>{ctxMenu.expr}</div>
              <button
                type="button"
                className="cfd-ctx-item"
                onClick={() => void doEvaluate(ctxMenu.expr, ctxMenu.x, ctxMenu.y)}
              >
                {t('cfdebug.evalExpr')}
              </button>
            </>
          )}
          {ctxMenu.line != null && ctxMenu.line !== currentLine && (
            <button
              type="button"
              className="cfd-ctx-item"
              title={t('cfdebug.runToCursorHint')}
              onClick={() => void runToCursor(ctxMenu.line as number)}
            >
              {t('cfdebug.runToCursor')}
            </button>
          )}
        </div>
      )}
      {evalResult && (
        <div className="cfd-eval-popup" style={{ left: evalResult.x, top: evalResult.y }}>
          <div className="cfd-eval-head">
            <span className="cfd-eval-expr">{evalResult.expr}</span>
            <button type="button" className="cfd-eval-close" onClick={closeOverlays} aria-label="×">
              ×
            </button>
          </div>
          {evalResult.error ? (
            <div className="cfd-eval-error">{evalResult.error}</div>
          ) : (
            <div className="cfd-eval-body">
              <span className="cfd-eval-val">{evalResult.result ?? ''}</span>
              {evalResult.type && <span className="cfd-eval-type">{evalResult.type}</span>}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// 源码窗格：独立 memo 组件。高亮结果按 source 用 useMemo 缓存，
// 仅当断点/当前行变化时才重渲染行样式，避免日志/变量刷新触发整份源码重高亮（性能痛点）。
const SourceView = memo(function SourceView({
  source,
  breakpoints,
  currentLine,
  onToggleBp,
}: {
  source: string[];
  breakpoints: Map<number, BpOptions>;
  currentLine: number | null;
  onToggleBp: (ln: number) => void;
}) {
  const html = useMemo(
    () => source.map((line) => (line ? hljs.highlight(line, { language: 'python' }).value : ' ')),
    [source],
  );
  return (
    <>
      {source.map((_line, i) => {
        const ln = i + 1;
        const bp = breakpoints.get(ln);
        const isBp = !!bp;
        const bpKind = bp ? (bp.logMessage ? 'log' : bp.condition || bp.hitCondition ? 'cond' : 'normal') : null;
        const isCur = currentLine === ln;
        return (
          <div
            key={ln}
            data-ln={ln}
            className={`cfd-code-line${isCur ? ' current' : ''}${isBp ? ' has-bp' : ''}${bpKind === 'log' ? ' has-bp-log' : bpKind === 'cond' ? ' has-bp-cond' : ''}`}
            onClick={() => onToggleBp(ln)}
            title={bp?.condition ? `if ${bp.condition}` : bp?.logMessage ? bp.logMessage : bp?.hitCondition ? `@${bp.hitCondition}` : undefined}
          >
            <span className="cfd-gutter">{isBp ? (bpKind === 'log' ? '≋' : bpKind === 'cond' ? '◆' : '●') : ''}</span>
            <span className="cfd-ln">{ln}</span>
            <span className="cfd-code" dangerouslySetInnerHTML={{ __html: html[i] }} />
          </div>
        );
      })}
    </>
  );
});

// 断点选项编辑器（内联在「断点」页某一行下方）
function BpEditor({
  bp,
  onApply,
  onDelete,
}: {
  bp: BpOptions;
  onApply: (opts: BpOptions) => void;
  onDelete: () => void;
}) {
  const { t } = useT();
  const [condition, setCondition] = useState(bp.condition || '');
  const [hitCondition, setHitCondition] = useState(bp.hitCondition || '');
  const [logMessage, setLogMessage] = useState(bp.logMessage || '');
  const [enabled, setEnabled] = useState(bp.enabled);
  return (
    <div className="cfdebug-bp-editor">
      <label className="cfd-bp-field">
        <span>{t('cfdebug.bpCondition')}</span>
        <input className="input" value={condition} onChange={(e) => setCondition(e.target.value)} placeholder="e.g. employee_id == 101" />
      </label>
      <label className="cfd-bp-field">
        <span>{t('cfdebug.bpHitCount')}</span>
        <input className="input" value={hitCondition} onChange={(e) => setHitCondition(e.target.value)} placeholder="e.g. 3" />
      </label>
      <label className="cfd-bp-field">
        <span>{t('cfdebug.bpLog')}</span>
        <input className="input" value={logMessage} onChange={(e) => setLogMessage(e.target.value)} placeholder="e.g. 当前 i={i}" />
      </label>
      <label className="chk">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        {t('cfdebug.bpEnabled')}
      </label>
      <div className="cfdebug-bp-editor-actions">
        <button type="button" className="btn btn-xs btn-primary" onClick={() => onApply({ enabled, condition, hitCondition, logMessage })}>
          {t('cfdebug.bpApply')}
        </button>
        <button type="button" className="btn btn-xs" onClick={onDelete}>
          {t('cfdebug.bpDelete')}
        </button>
      </div>
    </div>
  );
}

// ── 变量树节点（可递归展开） ──
function VarNode({ node, client }: { node: DapVariable; client: DapClient | null }) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<DapVariable[] | null>(null);
  const hasChildren = node.variablesReference > 0;

  const toggle = async () => {
    if (!hasChildren) return;
    if (!expanded) {
      if (!children && client) {
        try {
          const vr: { body?: { variables?: DapVariable[] } } = await client.variables(
            node.variablesReference,
          );
          setChildren(vr.body?.variables || []);
        } catch {
          setChildren([]);
        }
      }
      setExpanded(true);
    } else {
      setExpanded(false);
    }
  };

  return (
    <div className="cfd-var-node">
      <div className="cfd-var-row" onClick={() => void toggle()}>
        <span className="cfd-var-toggle">{hasChildren ? (expanded ? '▾' : '▸') : ''}</span>
        <span className="cfd-var-name">{node.name}</span>
        {node.type && <span className="cfd-var-type">{node.type}</span>}
        <span className="cfd-var-value" title={node.value}>{node.value}</span>
      </div>
      {expanded && children && (
        <div className="cfd-var-children">
          {children.length === 0 ? (
            <div className="cfd-var-empty">∅</div>
          ) : (
            children.map((c, i) => <VarNode key={`${c.name}-${i}`} node={c} client={client} />)
          )}
        </div>
      )}
    </div>
  );
}
