import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet } from '../../api/client';
import type { K8sDescribeResp, K8sEvent } from '../../api/types';
import { copyText } from '../../utils/clipboard';
import { useK8s } from './context';
import { useT } from '../../i18n';

/**
 * リソース記述モーダル —— web/js/05-k8s.js の openK8sDescribe / runK8sDescribe / renderK8sDescribeEvents から移行。
 *
 * 原生と同様：開くと自動で describe を1回実行；kind/name/namespace を変更して再記述可能；
 * kubectl describe 生テキスト + 関連イベント（Warning は赤）を出力。
 */

const KINDS = ['pod', 'deployment', 'service', 'configmap', 'ingress', 'statefulset', 'node', 'namespace'];

export interface DescribeSeed {
  kind: string;
  name: string;
  namespace: string;
}

export function K8sDescribeModal({ seed, onClose }: { seed: DescribeSeed; onClose: () => void }) {
  const { target } = useK8s();
  const { t } = useT();

  const [kind, setKind] = useState(seed.kind || 'pod');
  const [name, setName] = useState(seed.name || '');
  const [ns, setNs] = useState(seed.namespace || '');
  const [msg, setMsg] = useState('');
  const [text, setText] = useState('');
  const [events, setEvents] = useState<K8sEvent[]>([]);
  const [busy, setBusy] = useState(false);

  // ref で最新入力値を読み、run 依存変化で自動実行が重複発火しないよう
  const formRef = useRef({ kind, name, ns });
  formRef.current = { kind, name, ns };

  const run = useCallback(async () => {
    const k = formRef.current.kind.trim();
    const n = formRef.current.name.trim();
    const namespace = formRef.current.ns.trim();
    if (!k || !n) { setMsg(t('k8s.describe.kindNameRequired')); return; }
    setBusy(true);
    setMsg(t('k8s.describe.describing'));
    setText('');
    setEvents([]);
    try {
      const q = new URLSearchParams({ env: target.env, kind: k, name: n });
      if (namespace) q.set('namespace', namespace);
      const d = await apiGet<K8sDescribeResp>('/api/k8s/describe?' + q.toString());
      if (!d.ok) { setMsg(t('k8s.describe.fail') + (d.error || t('k8s.describe.unknown'))); return; }
      setText(d.text || t('k8s.describe.noOutput'));
      setEvents(d.events || []);
      setMsg(t('k8s.describe.done', { k, n }));
    } catch (ex: any) {
      setMsg(t('k8s.describe.fail') + ex.message);
    } finally {
      setBusy(false);
    }
  }, [target.env, t]);

  // 開くと自動 describe 1回（原生 openK8sDescribe 末尾の runK8sDescribe() に相当）
  useEffect(() => {
    if (seed.name) run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Esc で閉じる
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const copy = useCallback(async () => {
    if (!text) { setMsg(t('k8s.describe.nothingToCopy')); return; }
    const ok = await copyText(text);
    setMsg(ok ? t('k8s.describe.copied') : t('k8s.describe.copyFail'));
  }, [text, t]);

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal modal-lg" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{t('k8s.describe.title')}</h3>
          <button className="btn btn-sm btn-ghost" onClick={onClose}>✕</button>
        </div>

        <div className="modal-body">
          <div className="k8s-form-grid">
            <label className="field-col">{t('k8s.describe.kind')}
              <select className="sel" value={kind} onChange={(e) => setKind(e.target.value)}>
                {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
            <label className="field-col">{t('k8s.describe.name')}
              <input
                className="input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') run(); }}
                placeholder={t('k8s.describe.namePh')}
              />
            </label>
            <label className="field-col">{t('k8s.describe.ns')}
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
            <button className="btn btn-sm" onClick={run} disabled={busy}>{busy ? t('k8s.describe.describing') : t('k8s.describe.run')}</button>
            <button className="btn btn-sm btn-ghost" onClick={copy}>{t('k8s.describe.copy')}</button>
            <span className="panel-sub">{msg}</span>
          </div>

          <pre className="k8s-describe-text">{text}</pre>

          <div className="k8s-describe-head">{t('k8s.describe.relatedEvents')}</div>
          <div className="k8s-describe-events">
            {events.length === 0 ? (
              <div className="empty-hint">{t('k8s.describe.noEvents')}</div>
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
