import { useCallback, useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { api } from '../../api/client';
import type { K8sPodsResp } from '../../api/types';
import { useK8s } from './context';

interface PodInfo { name: string; phase?: string; }

export function K8sShell() {
  const { target, setTarget, addToast } = useK8s();

  const [pods, setPods] = useState<PodInfo[]>([]);
  const [containers, setContainers] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const [cwd, setCwd] = useState('/');
  const [input, setInput] = useState('');

  const termRef = useRef<HTMLDivElement | null>(null);
  const termObj = useRef<{ term: Terminal; fit: FitAddon } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const historyRef = useRef<string[]>([]);
  const histIdxRef = useRef(0);
  const cwdRef = useRef('/'); // 始终与状态同步，供 send 使用

  useEffect(() => { cwdRef.current = cwd; }, [cwd]);

  // 初始化 xterm
  useEffect(() => {
    if (!termRef.current || termObj.current) return;
    const term = new Terminal({
      fontSize: 13,
      cursorBlink: true,
      convertEol: true,
      theme: { background: '#1e1e1e' },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(termRef.current);
    try { fit.fit(); } catch { /* noop */ }
    // xterm 在此仅作为只读输出区域；命令由底部 input 框统一输入，
    // 避免 xterm 焦点与 input 框同时响应造成重复发送或光标混乱。
    term.attachCustomKeyEventHandler(() => false);
    termObj.current = { term, fit };
    const onResize = () => { try { fit.fit(); } catch { /* noop */ } };
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      disconnect();
      term.dispose();
      termObj.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadPods = useCallback(async () => {
    const q = encodeURIComponent(target.namespace.trim());
    try {
      const d = await api<K8sPodsResp>(`/api/k8s/pods?env=${encodeURIComponent(target.env)}&namespace=${q}`);
      if (d.ok) setPods(d.pods || []);
    } catch {
      setPods([]);
    }
  }, [target.env, target.namespace]);

  useEffect(() => { if (target.env) loadPods(); }, [target.env, loadPods]);

  const onPodChange = useCallback(async (pod: string) => {
    setTarget({ pod, container: '' });
    if (wsRef.current) disconnect();
    setContainers([]);
    if (!pod) return;
    try {
      const q = new URLSearchParams({ name: pod, env: target.env });
      const d = await api<{ containers?: string[] }>(`/api/k8s/pod-containers?${q.toString()}`);
      if (d.containers && d.containers.length) setContainers(d.containers);
    } catch {
      /* 容器列表加载失败不阻断使用 */
    }
  }, [target.env, setTarget]);

  const connect = useCallback(() => {
    const t = { pod: target.pod, container: target.container, namespace: target.namespace };
    if (!t.pod) { addToast('请先选择 Pod', 'warn'); return; }
    if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } }
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const q = new URLSearchParams({ env: target.env, pod: t.pod });
    if (t.container) q.set('container', t.container);
    if (t.namespace) q.set('namespace', t.namespace);
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${proto}${location.host}/ws/k8s/exec?${q.toString()}`);
    } catch (ex: any) {
      termObj.current?.term.writeln('无法建立连接：' + ex.message);
      return;
    }
    wsRef.current = ws;
    setConnected(false);
    termObj.current?.term.writeln(`正在连接 ${t.pod}${t.container ? '/' + t.container : ''} …`);
    ws.onopen = () => {};
    ws.onmessage = (ev) => {
      let m: any;
      try { m = JSON.parse(ev.data); } catch { termObj.current?.term.write(ev.data); return; }
      if (m.type === 'ready') {
        cwdRef.current = m.cwd || '/';
        setCwd(cwdRef.current);
        setConnected(true);
        termObj.current?.term.clear();
        termObj.current?.term.writeln(`已连接 ${t.pod}${t.container ? '/' + t.container : ''} · 工作目录 ${cwdRef.current}`);
        termObj.current?.term.write(`${cwdRef.current} $ `);
      } else if (m.type === 'output') {
        const text = m.data || '';
        // 后端 cwd 跟踪标记可能泄露到输出流，前端再过滤一次确保不显示
        if (text.includes('__PWD__')) return;
        termObj.current?.term.write(text);
      } else if (m.type === 'cwd') {
        cwdRef.current = m.cwd || cwdRef.current;
        setCwd(cwdRef.current);
        // cwd 更新后补一个 prompt，避免下一行输出直接接在旧 prompt 后面
        termObj.current?.term.write(`\r\n${cwdRef.current} $ `);
      } else if (m.type === 'error') {
        termObj.current?.term.writeln('\r\n错误：' + (m.msg || ''));
      }
    };
    ws.onclose = () => {
      setConnected(false);
      if (wsRef.current === ws) wsRef.current = null;
      termObj.current?.term.writeln('\r\n— 连接已关闭 —');
    };
    ws.onerror = () => { termObj.current?.term.writeln('\r\nWebSocket 连接错误'); };
  }, [target.env, target.pod, target.container, target.namespace, addToast]);

  function disconnect() {
    if (wsRef.current) {
      try { wsRef.current.send(JSON.stringify({ type: 'disconnect' })); } catch { /* noop */ }
      try { wsRef.current.close(); } catch { /* noop */ }
      wsRef.current = null;
    }
    setConnected(false);
  }

  const send = useCallback(() => {
    const val = input.trim();
    if (!val || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    // 兼容 Windows 习惯：cls 映射为 clear
    const cmd = val.toLowerCase() === 'cls' ? 'clear' : val;
    if (cmd === 'clear') {
      termObj.current?.term.clear();
      termObj.current?.term.write(`${cwdRef.current} $ `);
    } else {
      // 本地回显命令，否则非交互式 sh 不会回显用户输入
      termObj.current?.term.writeln(`\r\n${cwdRef.current} $ ${cmd}`);
    }
    wsRef.current.send(JSON.stringify({ type: 'cmd', data: cmd + '\n' }));
    historyRef.current.push(val);
    histIdxRef.current = historyRef.current.length;
    setInput('');
  }, [input]);

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { send(); }
    else if (e.key === 'ArrowUp') {
      if (historyRef.current.length && histIdxRef.current > 0) {
        histIdxRef.current--;
        setInput(historyRef.current[histIdxRef.current] || '');
        e.preventDefault();
      }
    } else if (e.key === 'ArrowDown') {
      if (histIdxRef.current < historyRef.current.length - 1) {
        histIdxRef.current++;
        setInput(historyRef.current[histIdxRef.current] || '');
        e.preventDefault();
      } else { histIdxRef.current = historyRef.current.length; setInput(''); e.preventDefault(); }
    }
  };

  return (
    <div className="k8s-shell">
      <div className="k8s-shell-connbar" style={{ display: 'flex' }}>
        <select className="sel" value={target.pod} onChange={(e) => onPodChange(e.target.value)}>
          <option value="">— 选择 Pod —</option>
          {pods.map((p) => <option key={p.name} value={p.name}>{p.name} · {p.phase || ''}</option>)}
        </select>
        <select className="sel" value={target.container} onChange={(e) => setTarget({ container: e.target.value })}>
          <option value="">（默认容器）</option>
          {containers.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <input className="input input-sm" value={target.namespace} onChange={(e) => setTarget({ namespace: e.target.value })} placeholder="命名空间" />
        <span className={'k8s-conn-status ' + (connected ? 'on' : 'off')}>{connected ? '已连接' : '未连接'}</span>
        <button className="btn btn-sm" onClick={connect} disabled={connected || !target.pod}>连接</button>
        <button className="btn btn-sm btn-ghost" onClick={disconnect} disabled={!connected}>断开</button>
      </div>
      <div className="k8s-shell-term" ref={termRef} />
      <div className="k8s-shell-inputbar">
        <span className="k8s-shell-prompt-inline">{cwd} $ </span>
        <input
          className="k8s-shell-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          disabled={!connected}
          placeholder={connected ? '输入命令，回车执行（↑/↓ 历史）' : '请先连接'}
        />
      </div>
    </div>
  );
}
