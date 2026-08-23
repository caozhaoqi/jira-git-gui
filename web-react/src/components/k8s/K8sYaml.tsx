import { useCallback, useEffect, useState } from 'react';
import { api, apiPost } from '../../api/client';
import type { K8sPodsResp, K8sYamlResp } from '../../api/types';
import { useK8s } from './context';
import { useT } from '../../i18n';

const KINDS = ['pod', 'deployment', 'service', 'configmap', 'secret', 'ingress', 'statefulset', 'daemonset', 'pvc'];

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

  const loadPodList = useCallback(async () => {
    const q = encodeURIComponent(ns.trim());
    try {
      const d = await api<K8sPodsResp>(`/api/k8s/pods?env=${encodeURIComponent(target.env)}&namespace=${q}`);
      if (!d.ok) { setPodList([]); return; }
      setPodList((d.pods || []).map((p) => p.name));
    } catch {
      setPodList([]);
    }
  }, [target.env, ns]);

  useEffect(() => {
    if (target.env) loadPodList();
  }, [target.env, loadPodList]);

  const getYaml = useCallback(async () => {
    if (!name.trim()) { setMsg(t('k8s.yaml.nameRequired')); return; }
    setMsg(t('k8s.yaml.getting'));
    try {
      const d = await apiPost<K8sYamlResp>('/api/k8s/yaml', {
        env: target.env, kind, name: name.trim(), namespace: ns.trim(), action: 'get', clean,
      });
      if (!d.ok) { setMsg(t('k8s.yaml.fail') + (d.error || '')); return; }
      setEditor(d.yaml || '');
      setOut('');
      setMsg(t('k8s.yaml.got', { name: name + (d.yaml && d.yaml.includes('status:') ? '' : t('k8s.yaml.cleaned')) }));
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
        <button className="btn btn-sm" onClick={getYaml}>{t('k8s.yaml.get')}</button>
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
          <select className="sel" value="" onChange={(e) => { if (e.target.value) { setName(e.target.value); setKind('pod'); getYaml(); } }}>
            <option value="">{t('k8s.yaml.selectPod')}</option>
            {podList.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
      </div>
      <div className="k8s-yaml-msg">{msg}</div>
      <textarea className="k8s-yaml-editor" value={editor} onChange={(e) => setEditor(e.target.value)} placeholder={t('k8s.yaml.editorPlaceholder')} />
      {out && <pre className="k8s-yaml-out" style={{ display: 'block' }}>{out}</pre>}
    </div>
  );
}
