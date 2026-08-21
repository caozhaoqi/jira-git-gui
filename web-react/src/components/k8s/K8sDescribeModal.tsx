import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet } from '../../api/client';
import type { K8sDescribeResp, K8sEvent } from '../../api/types';
import { copyText } from '../../utils/clipboard';
import { useK8s } from './context';

/**
 * 资源描述弹窗 —— 迁移自 web/js/05-k8s.js 的
 * openK8sDescribe / runK8sDescribe / renderK8sDescribeEvents。
 *
 * 与原生一致：打开即自动执行一次 describe；可改 kind/name/namespace 后重新描述；
 * 输出 kubectl describe 原始文本 + 该资源相关事件（Warning 标红）。
 */

const KINDS = ['pod', 'deployment', 'service', 'configmap', 'ingress', 'statefulset', 'node', 'namespace'];

export interface DescribeSeed {
  kind: string;
  name: string;
  namespace: string;
}

export function K8sDescribeModal({ seed, onClose }: { seed: DescribeSeed; onClose: () => void }) {
  const { target } = useK8s();

  const [kind, setKind] = useState(seed.kind || 'pod');
  const [name, setName] = useState(seed.name || '');
  const [ns, setNs] = useState(seed.namespace || '');
  const [msg, setMsg] = useState('');
  const [text, setText] = useState('');
  const [events, setEvents] = useState<K8sEvent[]>([]);
  const [busy, setBusy] = useState(false);

  // 用 ref 读取最新输入值，避免 run 依赖变化导致自动执行重复触发。
  const formRef = useRef({ kind, name, ns });
  formRef.current = { kind, name, ns };

  const run = useCallback(async () => {
    const k = formRef.current.kind.trim();
    const n = formRef.current.name.trim();
    const namespace = formRef.current.ns.trim();
    if (!k || !n) { setMsg('请填写资源类型与名称'); return; }
    setBusy(true);
    setMsg('描述中…');
    setText('');
    setEvents([]);
    try {
      const q = new URLSearchParams({ env: target.env, kind: k, name: n });
      if (namespace) q.set('namespace', namespace);
      const d = await apiGet<K8sDescribeResp>('/api/k8s/describe?' + q.toString());
      if (!d.ok) { setMsg('失败：' + (d.error || '未知错误')); return; }
      setText(d.text || '(无输出)');
      setEvents(d.events || []);
      setMsg(`✅ 已描述 ${k}/${n}`);
    } catch (ex: any) {
      setMsg('失败：' + ex.message);
    } finally {
      setBusy(false);
    }
  }, [target.env]);

  // 打开即自动描述一次（对应原生 openK8sDescribe 末尾的 runK8sDescribe()）
  useEffect(() => {
    if (seed.name) run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const copy = useCallback(async () => {
    if (!text) { setMsg('无内容可复制'); return; }
    const ok = await copyText(text);
    setMsg(ok ? '已复制' : '复制失败');
  }, [text]);

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal modal-lg" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>资源描述</h3>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>✕</button>
        </div>

        <div className="modal-body">
          <div className="k8s-form-grid">
            <label className="field-col">资源类型
              <select className="sel" value={kind} onChange={(e) => setKind(e.target.value)}>
                {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
            <label className="field-col">资源名称
              <input
                className="input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') run(); }}
                placeholder="如 core-6bc569958d-2ggkx"
              />
            </label>
            <label className="field-col">命名空间(留空用环境默认)
              <input
                className="input"
                value={ns}
                onChange={(e) => setNs(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') run(); }}
                placeholder="default"
              />
            </label>
          </div>

          <div className="k8s-actions">
            <button className="btn btn-sm" onClick={run} disabled={busy}>{busy ? '描述中…' : '描述'}</button>
            <button className="btn btn-sm btn-ghost" onClick={copy}>复制</button>
            <span className="panel-sub">{msg}</span>
          </div>

          <pre className="k8s-describe-text">{text}</pre>

          <div className="k8s-describe-head">相关事件</div>
          <div className="k8s-describe-events">
            {events.length === 0 ? (
              <div className="empty-hint">该资源无相关事件</div>
            ) : (
              events.slice(0, 30).map((e, i) => {
                const warn = e.type === 'Warning';
                return (
                  <div className="k8s-check" key={i}>
                    <div className={'k8s-chk-ico ' + (warn ? 'fail' : 'ok')}>{warn ? '✕' : '✓'}</div>
                    <div>
                      <div className="k8s-chk-name">
                        {e.reason || ''}{e.count && e.count > 1 ? ` ×${e.count}` : ''}
                      </div>
                      <div className="k8s-chk-detail">{e.message || ''}</div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
