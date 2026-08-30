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
  const termObj = useRef<{
    term: Terminal; fit: FitAddon; sendResize: () => void;
  } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const cwdRef = useRef('/'); // 始终与状态同期、供 send 使用

  useEffect(() => { cwdRef.current = cwd; }, [cwd]);

  // 初期化 xterm
  useEffect(() => {
    if (!termRef.current || termObj.current) return;
    const term = new Terminal({
      fontSize: 13,
      cursorBlink: true,
      // TTY 模式下远端 pty 输出的已经是 CRLF；convertEol 会把每个 \n 再转成 \r\n，
      // 变成 \r\r\n —— vim / top 的全屏光标定位会整体错位。因此必须关闭，
      // 本地提示文案统一手写 \r\n。
      convertEol: false,
      theme: { background: '#1e1e1e' },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(termRef.current);

    const sendResize = () => {
      const ws = wsRef.current;
      // cols/rows 为 0 说明容器还没布局完，此时上报会把远端窗口压成 1x1
      if (ws && ws.readyState === WebSocket.OPEN && term.cols > 0 && term.rows > 0) {
        try {
          ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
        } catch { /* noop */ }
      }
    };
    const doFit = () => {
      // 子标签隐藏（display:none）时容器尺寸为 0，xterm 无法正确测量，跳过；
      // 切回该标签时 ResizeObserver / window resize 会触发真正的 fit。
      if (!termRef.current || termRef.current.offsetParent === null) return;
      try { fit.fit(); } catch { /* 容器尚未布局，忽略 */ }
      sendResize();
    };

    // TTY 模式：xterm 直接接受键盘输入并转发给后端 pty（支持 vim / top 全屏交互）
    term.onData((data) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: 'input', data })); } catch { /* noop */ }
      }
    });

    doFit();
    // 面板折叠/展开、分栏拖动时 window.resize 不触发，必须监听容器本身
    const ro = new ResizeObserver(() => doFit());
    ro.observe(termRef.current);
    window.addEventListener('resize', doFit);
    termObj.current = { term, fit, sendResize };
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', doFit);
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
    // 建连时就带上首屏尺寸，让远端启动脚本直接按真实大小布局，避免先按 80x24
    // 起来再被 resize 追着改（首屏错位 + 拖窗口无反应）。
    const term = termObj.current?.term;
    if (term && term.cols > 0 && term.rows > 0) {
      q.set('cols', String(term.cols));
      q.set('rows', String(term.rows));
    }
    let ws: WebSocket;
    try {
      ws = new WebSocket(`${proto}${location.host}/ws/k8s/exec?${q.toString()}`);
    } catch (ex: any) {
      termObj.current?.term.write(`\r\n${t('k8s.shell.cannotConnect')}${ex.message}\r\n`);
      return;
    }
    wsRef.current = ws;
    setConnected(false);
    termObj.current?.term.write(
      `\r\n${t('k8s.shell.connecting')} ${tgt.pod}${tgt.container ? '/' + tgt.container : ''} …\r\n`);
    ws.onopen = () => {};
    ws.onmessage = (ev) => {
      let m: any;
      try { m = JSON.parse(ev.data); } catch { termObj.current?.term.write(ev.data); return; }
      if (m.type === 'ready') {
        cwdRef.current = m.cwd || '/';
        setCwd(cwdRef.current);
        setConnected(true);
        const obj = termObj.current;
        if (m.tty) {
          // TTY 模式：清掉连接日志，让本地屏幕与远端 pty 完全对齐，然后交给远端
          // shell 自己画 prompt。**这里不能写任何本地文案** —— 多占一行会让
          // vim / top 的全屏光标定位整体下移、退出后残留错位。
          obj?.term.clear();
          try { obj?.fit.fit(); } catch { /* noop */ }
          obj?.sendResize();
        } else {
          // 降级模式没有远端 prompt，只能本地画一个
          obj?.term.write(
            `\r\n${t('k8s.shell.connectedAs', { pod: tgt.pod + (tgt.container ? '/' + tgt.container : '') })} · ${t('k8s.shell.cwd')} ${cwdRef.current}\r\n`);
          obj?.term.write(`${cwdRef.current} $ `);
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
        termObj.current?.term.write(`\r\n${t('k8s.shell.error')}${m.msg || ''}\r\n`);
      }
    };
    ws.onclose = () => {
      setConnected(false);
      if (wsRef.current === ws) wsRef.current = null;
      termObj.current?.term.write(`\r\n— ${t('k8s.shell.closed')} —\r\n`);
    };
    ws.onerror = () => {
      termObj.current?.term.write(`\r\n${t('k8s.shell.wsError')}\r\n`);
    };
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
        {connected ? t('k8s.shell.tipTty') : t('k8s.shell.tipIdle')}
      </div>
    </div>
  );
}
