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
  const [auto, setAuto] = useState('0');
  const [wrap, setWrap] = useState(true);
  const [lineno, setLineno] = useState(true);
  const [levelOn, setLevelOn] = useState(true);
  const [font, setFont] = useState(13);

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
    if (!p.pod) { setStatus(t('logviewer.noPod')); setIsErr(true); return; }
    const follow = autoFollowRef.current || isAtBottom();
    setStatus(t('logviewer.loading'));
    setIsErr(false);
    try {
      const q = new URLSearchParams({ name: p.pod });
      if (p.env) q.set('env', p.env);
      if (p.container) q.set('container', p.container);
      if (p.namespace) q.set('namespace', p.namespace);
      q.set('tail', tailRef.current);
      if (prevRef.current) q.set('previous', '1');
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

  const matches = useMemo(() => {
    if (!re) return [] as number[];
    const out: number[] = [];
    for (let i = 0; i < lines.length; i++) {
      if (/^=+\s/.test(lines[i])) continue; // 分隔行不计入匹配
      re.lastIndex = 0;
      if (re.test(lines[i])) out.push(i);
    }
    return out;
  }, [lines, re]);

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
          <button className="btn btn-sm" onClick={refresh} title={t('logviewer.refresh')}>↻ {t('logviewer.refresh')}</button>
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
          <select className="sel" value={auto} onChange={(e) => setAuto(e.target.value)}>
            <option value="0">{t('logviewer.off')}</option>
            <option value="3">3 {t('logviewer.seconds')}</option>
            <option value="5">5 {t('logviewer.seconds')}</option>
            <option value="10">10 {t('logviewer.seconds')}</option>
          </select>
        </label>

        <label className="lv-toggle"><input type="checkbox" checked={previous} onChange={(e) => setPrevious(e.target.checked)} /> {t('logviewer.previous')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> {t('logviewer.wrap')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={lineno} onChange={(e) => setLineno(e.target.checked)} /> {t('logviewer.lineNo')}</label>
        <label className="lv-toggle"><input type="checkbox" checked={levelOn} onChange={(e) => setLevelOn(e.target.checked)} /> {t('logviewer.levelHighlight')}</label>

        <span className="lv-spacer" />
        <span className="lv-font">{t('logviewer.fontSize')}
          <button className="btn btn-xs" title={t('logviewer.shrink')} onClick={() => setFont((f) => Math.max(10, f - 1))}>A−</button>
          <button className="btn btn-xs" title={t('logviewer.enlarge')} onClick={() => setFont((f) => Math.min(22, f + 1))}>A+</button>
        </span>
      </div>

      <main className="lv-body" ref={bodyRef as any}>
        {status && (
          <div className="lv-statusline" style={{ color: isErr ? 'var(--danger)' : 'var(--muted)' }}>{status}</div>
        )}
        <div className={'lv-log' + (wrap ? ' wrap' : '')} style={{ fontSize: font + 'px' }}>
          {lines.map((line, i) => {
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
