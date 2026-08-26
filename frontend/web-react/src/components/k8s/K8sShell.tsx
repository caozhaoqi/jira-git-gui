import { useCallback, useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { api } from '../../api/client';
import type { K8sPodsResp } from '../../api/types';
import { useK8s } from './context';
import { useT } from '../../i18n';

interface PodInfo { name: string; phase?: string; }

export function K8sShell() {
  const { target, setTarget, addToast } = useK8s();
  const { t } = useT();

  const [pods, setPods] = useState<PodInfo[]>([]);
  const [containers, setContainers] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const [cwd, setCwd] = useState('/');

  const termRef = useRef<HTMLDivElement | null>(null);
  const termObj = useRef<{ term: Terminal; fit: FitAddon } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const cwdRef = useRef('/'); // 始终与状态同期、供 send 使用

  useEffect(() => { cwdRef.current = cwd; }, [cwd]);

  // 初期化 xterm
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
    // TTY 模式：xterm 直接接受键盘输入并转发给后端 pty（支持 vim / htop 全屏交互）
    term.onData((data) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: 'input', data })); } catch { /* noop */ }
      }
    });
    const sendResize = () => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
        } catch { /* noop */ }
      }
    };
    const onResize = () => {
      try { fit.fit(); sendResize(); } catch { /* noop */ }
    };
    window.addEventListener('resize', onResize);
    termObj.current = { term, fit };
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
      /* コンテナ一覧読込失敗は利用をブロックしない */
    }
  }, [target.env, setTarget]);

  const connect = useCallback(() => {
    const tgt = { pod: target.pod, container: target.container, namespace: target.namespace };
    if (!tgt.pod) { addToast(t('k8s.shell.pickPodFirst'), 'warn'); return; }
    if (wsRef.current) { try { wsRef.current.close(); } catch { /* noop */ } }
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const q = new URLSearchParams({ env: target.env, pod: tgt.pod, tty: '1' });
    if (tgt.container) q.set('container', tgt.container);
    if (tgt.namespace) q.set('namespace', tgt.namespace);
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${proto}${location.host}/ws/k8s/exec?${q.toString()}`);
    } catch (ex: any) {
      termObj.current?.term.writeln(t('k8s.shell.cannotConnect') + ex.message);
      return;
    }
    wsRef.current = ws;
    setConnected(false);
    termObj.current?.term.writeln(`${t('k8s.shell.connecting')} ${tgt.pod}${tgt.container ? '/' + tgt.container : ''} …`);
    ws.onopen = () => {};
    ws.onmessage = (ev) => {
      let m: any;
      try { m = JSON.parse(ev.data); } catch { termObj.current?.term.write(ev.data); return; }
      if (m.type === 'ready') {
        cwdRef.current = m.cwd || '/';
        setCwd(cwdRef.current);
        setConnected(true);
        termObj.current?.term.clear();
        termObj.current?.term.writeln(`${t('k8s.shell.connectedAs', { pod: tgt.pod + (tgt.container ? '/' + tgt.container : '') })} · ${t('k8s.shell.cwd')} ${cwdRef.current}`);
        if (m.tty) {
          // TTY 模式：等远程 shell 的 prompt，无本地 prompt；通知后端终端尺寸
          const fit = termObj.current?.fit;
          try { fit?.fit(); } catch { /* noop */ }
          try {
            ws.send(JSON.stringify({
              type: 'resize',
              cols: termObj.current?.term.cols ?? 80,
              rows: termObj.current?.term.rows ?? 24,
            }));
          } catch { /* noop */ }
        } else {
          termObj.current?.term.write(`${cwdRef.current} $ `);
        }
      } else if (m.type === 'output') {
        const text = m.data || '';
        // 后端 cwd 跟踪标记可能泄露到出力ストリーム、前端でもう一度フィルタして表示させない
        if (text.includes('__PWD__')) return;
        termObj.current?.term.write(text);
      } else if (m.type === 'cwd') {
        cwdRef.current = m.cwd || cwdRef.current;
        setCwd(cwdRef.current);
        // cwd 更新后补一个 prompt、次の行出力が旧 prompt の後ろに直接継がないよう
        termObj.current?.term.write(`\r\n${cwdRef.current} $ `);
      } else if (m.type === 'error') {
        termObj.current?.term.writeln('\r\n' + t('k8s.shell.error') + (m.msg || ''));
      }
    };
    ws.onclose = () => {
      setConnected(false);
      if (wsRef.current === ws) wsRef.current = null;
      termObj.current?.term.writeln('\r\n— ' + t('k8s.shell.closed') + ' —');
    };
    ws.onerror = () => { termObj.current?.term.writeln('\r\n' + t('k8s.shell.wsError')); };
  }, [target.env, target.pod, target.container, target.namespace, addToast, t]);

  function disconnect() {
    if (wsRef.current) {
      try { wsRef.current.send(JSON.stringify({ type: 'disconnect' })); } catch { /* noop */ }
      try { wsRef.current.close(); } catch { /* noop */ }
      wsRef.current = null;
    }
    setConnected(false);
  }

  return (
    <div className="k8s-shell">
      <div className="k8s-shell-connbar" style={{ display: 'flex' }}>
        <select className="sel" value={target.pod} onChange={(e) => onPodChange(e.target.value)}>
          <option value="">{t('k8s.shell.selectPod')}</option>
          {pods.map((p) => <option key={p.name} value={p.name}>{p.name} · {p.phase || ''}</option>)}
        </select>
        <select className="sel" value={target.container} onChange={(e) => setTarget({ container: e.target.value })}>
          <option value="">{t('k8s.shell.defaultContainer')}</option>
          {containers.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <input className="input input-sm" value={target.namespace} onChange={(e) => setTarget({ namespace: e.target.value })} placeholder={t('k8s.shell.namespacePh')} />
        <span className={'k8s-conn-status ' + (connected ? 'on' : 'off')}>{connected ? t('k8s.shell.connected') : t('k8s.shell.disconnected')}</span>
        <button className="btn btn-sm" onClick={connect} disabled={connected || !target.pod}>{t('k8s.shell.connect')}</button>
        <button className="btn btn-sm btn-ghost" onClick={disconnect} disabled={!connected}>{t('k8s.shell.disconnect')}</button>
      </div>
      <div className="k8s-shell-term" ref={termRef} />
      <div className="k8s-shell-tip">
        TTY 模式：直接在终端中输入命令，支持 vim / top / htop 等全屏程序
      </div>
    </div>
  );
}
