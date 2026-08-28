import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, apiPost } from '../../api/client';
import type { K8sPodsResp, K8sYamlResp } from '../../api/types';
import { useK8s } from './context';
import { useT } from '../../i18n';

const KINDS = ['pod', 'deployment', 'service', 'configmap', 'secret', 'ingress', 'statefulset', 'daemonset', 'pvc'];

// ---- 文本工具 ----
function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// 轻量 YAML 语法着色（作用于已转义文本，此时尚未注入 <mark>）
function yamlColorize(escaped: string): string {
  let s = escaped;
  // 注释（行内 # 之后）
  s = s.replace(/(^|\s)(#.*)$/, (_m, p1: string, p2: string) => `${p1}<span class="yk-comment">${p2}</span>`);
  // 键名：可选列表短横 "- key:"（key 后紧跟冒号）
  s = s.replace(
    /^(\s*)(?:- )?([A-Za-z_][\w.\-/]*)(:)(?=\s|$)/,
    (_m: string, ind: string, key: string, colon: string) => `${ind}<span class="yk-key">${key}</span>${colon}`
  );
  // 引号字符串（双引号会转义为 &quot;，单引号为 &#39; 或保留 '）
  s = s.replace(/(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;|'[^']*')/g, (m: string) => `<span class="yk-str">${m}</span>`);
  // 数值 / 布尔 / null（值位置）
  s = s.replace(
    /(^|\s|>)(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)(?=\s|$)/g,
    (_m: string, p1: string, p2: string) => `${p1}<span class="yk-num">${p2}</span>`
  );
  return s;
}

// 在标签之外的文本段注入 <mark data-mid>，避免破坏语法着色 span
function applySearchMarks(html: string, kw: string): { html: string; matched: number } {
  if (!kw) return { html, matched: 0 };
  const re = new RegExp(`(${escapeRegExp(kw)})`, 'gi');
  let out = '';
  let i = 0;
  const n = html.length;
  let matched = 0;
  while (i < n) {
    if (html[i] === '<') {
      const j = html.indexOf('>', i);
      const end = j === -1 ? n : j + 1;
      out += html.slice(i, end);
      i = end;
    } else {
      const j = html.indexOf('<', i);
      const end = j === -1 ? n : j;
      const seg = html.slice(i, end);
      out += seg.replace(re, (m: string) => `<mark data-mid="${++matched}">${m}</mark>`);
      i = end;
    }
  }
  return { html: out, matched };
}

// 生成高亮视图：先逐行着色，再全局搜索注入 mark（data-mid 全局唯一，便于跳转）
function makeYamlView(text: string, q: string): { html: string; matched: number } {
  const kw = q.trim();
  const colored = text
    .split('\n')
    .map((ln) => yamlColorize(escapeHtml(ln)))
    .join('\n');
  if (!kw) return { html: colored, matched: 0 };
  return applySearchMarks(colored, kw);
}

// 只读高亮 + 搜索视图（复用 JsonBlock 的跳转逻辑）
function YamlView({
  text, q, setQ, onCopy, t,
}: {
  text: string; q: string; setQ: (v: string) => void;
  onCopy: (s: string) => void; t: (k: string) => string;
}) {
  const preRef = useRef<HTMLPreElement>(null);
  const [cur, setCur] = useState(0);
  const kw = q.trim();
  const { html, matched } = useMemo(() => makeYamlView(text, q), [text, q]);

  useEffect(() => {
    setCur(0);
  }, [kw]);

  const goto = useCallback(
    (idx: number) => {
      if (!preRef.current || matched === 0) return;
      const clamped = ((idx - 1 + matched) % matched) + 1; // 1..matched 循环
      const el = preRef.current.querySelector<HTMLElement>(`mark[data-mid="${clamped}"]`);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setCur(clamped);
      }
    },
    [matched]
  );

  useEffect(() => {
    const root = preRef.current;
    if (!root) return;
    root.querySelectorAll('mark.active').forEach((m) => m.classList.remove('active'));
    if (cur > 0) root.querySelector(`mark[data-mid="${cur}"]`)?.classList.add('active');
  }, [cur, html]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter' || !kw) return;
    e.preventDefault();
    if (matched === 0) return;
    if (e.shiftKey) goto(cur <= 1 ? matched : cur - 1);
    else goto(cur === 0 ? 1 : cur >= matched ? 1 : cur + 1);
  };

  return (
    <>
      <div className="k8s-yaml-bar">
        <input
          className="k8s-yaml-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t('k8s.yaml.search')}
          spellCheck={false}
        />
        {kw && (
          <span className="k8s-yaml-count">
            {t('k8s.yaml.match')}: {matched > 0 ? `${cur === 0 ? 1 : cur}/${matched}` : 0}
          </span>
        )}
        <button className="btn btn-sm" onClick={() => goto(cur <= 1 ? matched : cur - 1)} disabled={!kw || matched === 0} title="Shift+Enter">↑</button>
        <button className="btn btn-sm" onClick={() => goto(cur === 0 ? 1 : cur >= matched ? 1 : cur + 1)} disabled={!kw || matched === 0} title="Enter">↓</button>
        <button className="btn btn-sm" onClick={() => onCopy(text)}>{t('k8s.yaml.copy')}</button>
      </div>
      <pre ref={preRef} className="k8s-yaml-pre" dangerouslySetInnerHTML={{ __html: html }} />
    </>
  );
}

export function K8sYaml() {
  const { target, openDescribe } = useK8s();
  const { t } = useT();

  const [kind, setKind] = useState('pod');
  const [name, setName] = useState('');
  const [ns, setNs] = useState('');
  const [clean, setClean] = useState(false);
  const [editor, setEditor] = useState('');
  const [out, setOut] = useState('');
  const [msg, setMsg] = useState('');
  const [podList, setPodList] = useState<string[]>([]);
  const [mode, setMode] = useState<'view' | 'edit'>('view');
  const [q, setQ] = useState('');

  const loadPodList = useCallback(async () => {
    const qry = encodeURIComponent(ns.trim());
    try {
      const d = await api<K8sPodsResp>(`/api/k8s/pods?env=${encodeURIComponent(target.env)}&namespace=${qry}`);
      if (!d.ok) { setPodList([]); return; }
      setPodList((d.pods || []).map((p) => p.name));
    } catch {
      setPodList([]);
    }
  }, [target.env, ns]);

  useEffect(() => {
    if (target.env) loadPodList();
  }, [target.env, loadPodList]);

  const getYaml = useCallback(async (targetName?: string, targetKind?: string) => {
    const n = (targetName ?? name).trim();
    if (!n) { setMsg(t('k8s.yaml.nameRequired')); return; }
    setMsg(t('k8s.yaml.getting'));
    try {
      const d = await apiPost<K8sYamlResp>('/api/k8s/yaml', {
        env: target.env, kind: targetKind || kind, name: n, namespace: ns.trim(), action: 'get', clean,
      });
      if (!d.ok) { setMsg(t('k8s.yaml.fail') + (d.error || '')); return; }
      setEditor(d.yaml || '');
      setOut('');
      setQ('');
      setMode('view');
      setMsg(t('k8s.yaml.got', { name: n + (d.yaml && d.yaml.includes('status:') ? '' : t('k8s.yaml.cleaned')) }));
    } catch (ex: any) {
      setMsg(t('k8s.yaml.fail') + ex.message);
    }
  }, [name, kind, ns, clean, target.env, t]);

  const applyYaml = useCallback(async () => {
    if (!editor.trim()) { setMsg(t('k8s.yaml.empty')); return; }
    setMsg(t('k8s.yaml.uploading'));
    try {
      const d = await apiPost<K8sYamlResp>('/api/k8s/yaml', {
        env: target.env, kind, name: name.trim(), namespace: ns.trim(), content: editor, action: 'apply',
      });
      if (!d.ok) { setMsg(t('k8s.yaml.fail') + (d.error || '')); return; }
      setOut((d.stdout || '') + (d.stderr ? '\n' + d.stderr : ''));
      setMsg(t('k8s.yaml.uploadOk'));
    } catch (ex: any) {
      setMsg(t('k8s.yaml.fail') + ex.message);
    }
  }, [editor, target.env, kind, name, ns, t]);

  const copyYaml = useCallback((text: string) => {
    navigator.clipboard?.writeText(text).catch(() => {});
  }, []);

  return (
    <div className="k8s-yaml">
      <div className="k8s-yaml-cfg">
        <label>{t('k8s.yaml.kind')}
          <select className="sel" value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        <label>{t('k8s.yaml.name')}<input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('k8s.yaml.namePh')} /></label>
        <label>{t('k8s.yaml.namespace')}<input className="input" value={ns} onChange={(e) => setNs(e.target.value)} placeholder={t('k8s.yaml.namespacePh')} /></label>
        <label className="chk"><input type="checkbox" checked={clean} onChange={(e) => setClean(e.target.checked)} /> {t('k8s.yaml.cleanStatus')}</label>
        <button className="btn btn-sm" onClick={() => getYaml()}>{t('k8s.yaml.get')}</button>
        <button className="btn btn-sm" onClick={applyYaml}>{t('k8s.yaml.apply')}</button>
        <button
          className="btn btn-sm btn-ghost"
          onClick={() => {
            if (!name.trim()) { setMsg(t('k8s.yaml.nameRequired')); return; }
            openDescribe(kind, name.trim(), ns.trim());
          }}
        >{t('k8s.yaml.describe')}</button>
      </div>
      <div className="k8s-yaml-podlist">
        <label>{t('k8s.yaml.fromPod')}：
          <select className="sel" value={name} onChange={(e) => { const v = e.target.value; if (v) { setName(v); setKind('pod'); getYaml(v, 'pod'); } }}>
            <option value="">{t('k8s.yaml.selectPod')}</option>
            {podList.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
      </div>
      <div className="k8s-yaml-msg">{msg}</div>
      {mode === 'view' ? (
        <>
          {editor ? (
            <YamlView text={editor} q={q} setQ={setQ} onCopy={copyYaml} t={t} />
          ) : (
            <textarea className="k8s-yaml-editor" value={editor} onChange={(e) => setEditor(e.target.value)} placeholder={t('k8s.yaml.editorPlaceholder')} />
          )}
          <div className="k8s-yaml-modebar">
            <button className="btn btn-sm" onClick={() => setMode('edit')}>{t('k8s.yaml.edit')}</button>
          </div>
        </>
      ) : (
        <>
          <textarea className="k8s-yaml-editor" value={editor} onChange={(e) => setEditor(e.target.value)} placeholder={t('k8s.yaml.editorPlaceholder')} />
          <div className="k8s-yaml-modebar">
            <button className="btn btn-sm" onClick={() => setMode('view')}>{t('k8s.yaml.view')}</button>
          </div>
        </>
      )}
      {out && <pre className="k8s-yaml-out" style={{ display: 'block' }}>{out}</pre>}
    </div>
  );
}
