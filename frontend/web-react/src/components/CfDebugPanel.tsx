import { useEffect, useRef, useState, useCallback, useMemo, memo } from 'react';
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
} from '../api/cfdebug/types';

type RightTab = 'variables' | 'stack' | 'breakpoints' | 'result';

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
  const [breakpoints, setBreakpoints] = useState<Set<number>>(new Set());
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

  const clientRef = useRef<DapClient | null>(null);
  const breakpointsRef = useRef<Set<number>>(new Set());
  // 断点按函数 path 记忆，切换函数不丢失
  const bpByPath = useRef<Map<string, Set<number>>>(new Map());
  const selectedRef = useRef<CfFunction | null>(null);
  const sessionIdRef = useRef('');
  const resultCollecting = useRef(false);
  const resultBuf = useRef<string[]>([]);
  const logRef = useRef<HTMLDivElement>(null);
  const sourceRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    breakpointsRef.current = breakpoints;
  }, [breakpoints]);
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

  // ── 选择函数：加载源码 + 重置断点 + prefill 参数 ──
  const selectFunction = async (f: CfFunction) => {
    setSelected(f);
    // 恢复该函数记忆的断点（没有则空）
    setBreakpoints(bpByPath.current.get(f.path) ?? new Set());
    setCurrentLine(null);
    // 预填参数骨架：每个 execute 参数作为一个 key
    try {
      const seed: Record<string, string> = {};
      f.params.forEach((p) => {
        if (!p.startsWith('*') && !p.startsWith('**')) seed[p] = '';
      });
      setKwargsInput(Object.keys(seed).length ? JSON.stringify(seed, null, 2) : '{}');
    } catch {
      setKwargsInput('{}');
    }
    await loadSource(f.path);
  };

  const loadSource = async (file: string) => {
    try {
      const s: CfSource = await cfdebug.getSource(file);
      if (s.ok) {
        setSource(s.lines);
        setSourceFile(s.file);
      } else {
        addToast(t('cfdebug.sourceFail', { msg: s.error || '' }), 'error');
      }
    } catch (e: any) {
      addToast(t('cfdebug.sourceFail', { msg: e.message }), 'error');
    }
  };

  const toggleBp = useCallback((ln: number) => {
    setBreakpoints((prev) => {
      const next = new Set(prev);
      if (next.has(ln)) next.delete(ln);
      else next.add(ln);
      // 记忆到当前函数 path
      const f = selectedRef.current;
      if (f) bpByPath.current.set(f.path, next);
      // 运行/暂停期间：实时下发到 debugpy（DAP 支持运行中改断点）
      const st = statusRef.current;
      if ((st === 'running' || st === 'paused') && f && clientRef.current) {
        clientRef.current.setBreakpoints(f.path, Array.from(next)).catch(() => {});
      }
      return next;
    });
  }, []);

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
      if (fr[0]) {
        setCurrentLine(fr[0].line);
        await loadFrameVariables(fr[0].id);
      }
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
          const lines = Array.from(breakpointsRef.current);
          await client.setBreakpoints(f.path, lines);
          await client.configurationDone();
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
    await loadFrameVariables(frame.id);
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
          <div className="cfdebug-source" ref={sourceRef}>
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
                {breakpoints.size === 0 ? (
                  <div className="empty-hint">{t('cfdebug.noBp')}</div>
                ) : (
                  Array.from(breakpoints)
                    .sort((a, b) => a - b)
                    .map((ln) => (
                      <div key={ln} className="cfdebug-bp-row">
                        <span className="cfdebug-bp-dot">●</span>
                        <span className="cfdebug-bp-file">{sourceFile.split('/').pop()}</span>
                        <span className="cfdebug-bp-line">:{ln}</span>
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
  breakpoints: Set<number>;
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
        const isBp = breakpoints.has(ln);
        const isCur = currentLine === ln;
        return (
          <div
            key={ln}
            className={`cfd-code-line${isCur ? ' current' : ''}${isBp ? ' has-bp' : ''}`}
            onClick={() => onToggleBp(ln)}
          >
            <span className="cfd-gutter">{isBp ? '●' : ''}</span>
            <span className="cfd-ln">{ln}</span>
            <span className="cfd-code" dangerouslySetInnerHTML={{ __html: html[i] }} />
          </div>
        );
      })}
    </>
  );
});

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
