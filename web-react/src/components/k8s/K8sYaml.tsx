import { useCallback, useEffect, useState } from 'react';
import { api, apiPost } from '../../api/client';
import type { K8sPodsResp, K8sYamlResp } from '../../api/types';
import { useK8s } from './context';

const KINDS = ['pod', 'deployment', 'service', 'configmap', 'secret', 'ingress', 'statefulset', 'daemonset', 'pvc'];

export function K8sYaml() {
  const { target, openDescribe } = useK8s();

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
    if (!name.trim()) { setMsg('请填写资源名称'); return; }
    setMsg('获取中…');
    try {
      const d = await apiPost<K8sYamlResp>('/api/k8s/yaml', {
        env: target.env, kind, name: name.trim(), namespace: ns.trim(), action: 'get', clean,
      });
      if (!d.ok) { setMsg('失败：' + d.error); return; }
      setEditor(d.yaml || '');
      setOut('');
      setMsg('已获取 ' + name + (d.yaml && d.yaml.includes('status:') ? '' : '（已清洗）'));
    } catch (ex: any) {
      setMsg('失败：' + ex.message);
    }
  }, [name, kind, ns, clean, target.env]);

  const applyYaml = useCallback(async () => {
    if (!editor.trim()) { setMsg('内容为空'); return; }
    setMsg('上传中…');
    try {
      const d = await apiPost<K8sYamlResp>('/api/k8s/yaml', {
        env: target.env, kind, name: name.trim(), namespace: ns.trim(), content: editor, action: 'apply',
      });
      if (!d.ok) { setMsg('失败：' + d.error); return; }
      setOut((d.stdout || '') + (d.stderr ? '\n' + d.stderr : ''));
      setMsg('✅ 上传成功');
    } catch (ex: any) {
      setMsg('失败：' + ex.message);
    }
  }, [editor, target.env, kind, name, ns]);

  return (
    <div className="k8s-yaml">
      <div className="k8s-yaml-cfg">
        <label>类型
          <select className="sel" value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        <label>名称<input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="资源名称" /></label>
        <label>命名空间<input className="input" value={ns} onChange={(e) => setNs(e.target.value)} placeholder="（默认 default）" /></label>
        <label className="chk"><input type="checkbox" checked={clean} onChange={(e) => setClean(e.target.checked)} /> 清洗 status</label>
        <button className="btn btn-sm" onClick={getYaml}>获取</button>
        <button className="btn btn-sm" onClick={applyYaml}>应用</button>
        <button
          className="btn btn-sm btn-ghost"
          onClick={() => {
            if (!name.trim()) { setMsg('请先填写资源名称'); return; }
            openDescribe(kind, name.trim(), ns.trim());
          }}
        >描述</button>
      </div>
      <div className="k8s-yaml-podlist">
        <label>从 Pod 自动获取：
          <select className="sel" value="" onChange={(e) => { if (e.target.value) { setName(e.target.value); setKind('pod'); getYaml(); } }}>
            <option value="">— 选择 Pod —</option>
            {podList.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
      </div>
      <div className="k8s-yaml-msg">{msg}</div>
      <textarea className="k8s-yaml-editor" value={editor} onChange={(e) => setEditor(e.target.value)} placeholder="YAML 内容（获取后在此显示，可编辑后「应用」）" />
      {out && <pre className="k8s-yaml-out" style={{ display: 'block' }}>{out}</pre>}
    </div>
  );
}
