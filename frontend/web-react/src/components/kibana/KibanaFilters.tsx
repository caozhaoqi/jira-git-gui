import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import { useAppStore } from '../../store/useAppStore';
import { useT } from '../../i18n';
import type {
  KibanaBucket, KibanaFieldsResp, KibanaSite, KibanaSitesResp,
} from '../../api/types';

/** 共享的筛选条：时间范围 + 各维度下拉 + 关键词 + 级别。两个子标签共用。 */
export interface FiltersProps {
  site: string;
  value: {
    start: string; end: string; namespace: string; container: string;
    app: string; host: string; keyword: string; excludeKeyword: string;
    levels: string[];
  };
  onChange: (patch: Record<string, unknown>) => void;
  onSearch: () => void;
  busy?: boolean;
  /** 容器视角不需要 Pod 下拉（左侧树选），传 false 可隐藏 */
  showPodSelect?: boolean;
  pod?: string;
}

const TIME_PRESETS = [
  { key: 'now-15m', labelKey: 'kibana.time.15m' },
  { key: 'now-1h', labelKey: 'kibana.time.1h' },
  { key: 'now-6h', labelKey: 'kibana.time.6h' },
  { key: 'now-24h', labelKey: 'kibana.time.24h' },
  { key: 'now-7d', labelKey: 'kibana.time.7d' },
];

const LEVELS = ['ERROR', 'WARN', 'INFO', 'DEBUG'];

export function KibanaFilters({
  site, value, onChange, onSearch, busy, showPodSelect, pod,
}: FiltersProps) {
  const { t } = useT();
  const [fields, setFields] = useState<KibanaFieldsResp>({});
  const [fieldsErr, setFieldsErr] = useState('');

  const loadFields = useCallback(async () => {
    if (!site) return;
    try {
      const d = await apiGet<KibanaFieldsResp>('/api/kibana/fields?start=now-24h');
      setFields(d);
      setFieldsErr(d.ok === false ? (d.error || '') : '');
    } catch (ex: any) {
      setFieldsErr(ex.message || String(ex));
    }
  }, [site]);

  useEffect(() => { loadFields(); }, [loadFields]);

  const opts = (list?: KibanaBucket[]) => list || [];

  return (
    <div className="kb-filters">
      <div className="kb-filter-row">
        <label className="field-inline">
          {t('kibana.timeRange')}
          <select
            className="sel"
            value={TIME_PRESETS.some((p) => p.key === value.start) ? value.start : 'custom'}
            onChange={(e) => {
              const v = e.target.value;
              if (v !== 'custom') onChange({ start: v, end: '' });
            }}
          >
            {TIME_PRESETS.map((p) => (
              <option key={p.key} value={p.key}>{t(p.labelKey)}</option>
            ))}
            {!TIME_PRESETS.some((p) => p.key === value.start) && (
              <option value="custom">{t('kibana.time.custom')}</option>
            )}
          </select>
        </label>
        <input
          className="input input-sm kb-time-input"
          value={value.start}
          onChange={(e) => onChange({ start: e.target.value })}
          placeholder="now-1h"
          title={t('kibana.time.startPh')}
        />
        <span className="kb-time-sep">→</span>
        <input
          className="input input-sm kb-time-input"
          value={value.end}
          onChange={(e) => onChange({ end: e.target.value })}
          placeholder={t('kibana.time.endPh')}
        />

        <label className="field-inline">
          {t('kibana.container')}
          <select className="sel" value={value.container}
                  onChange={(e) => onChange({ container: e.target.value })}>
            <option value="">{t('common.all')}</option>
            {opts(fields.containers).map((b) => (
              <option key={b.key} value={b.key}>{b.key} ({b.count})</option>
            ))}
          </select>
        </label>

        <label className="field-inline">
          {t('kibana.app')}
          <select className="sel" value={value.app}
                  onChange={(e) => onChange({ app: e.target.value })}>
            <option value="">{t('common.all')}</option>
            {opts(fields.apps).map((b) => (
              <option key={b.key} value={b.key}>{b.key}</option>
            ))}
          </select>
        </label>

        <label className="field-inline">
          {t('kibana.host')}
          <select className="sel" value={value.host}
                  onChange={(e) => onChange({ host: e.target.value })}>
            <option value="">{t('common.all')}</option>
            {opts(fields.hosts).map((b) => (
              <option key={b.key} value={b.key}>{b.key}</option>
            ))}
          </select>
        </label>

        {showPodSelect && (
          <label className="field-inline">
            Pod
            <select className="sel" value={pod || ''}
                    onChange={(e) => onChange({ pod: e.target.value })}>
              <option value="">{t('common.all')}</option>
              {opts(fields.pods).map((b) => (
                <option key={b.key} value={b.key}>{b.key}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className="kb-filter-row">
        <input
          className="input input-sm kb-keyword"
          value={value.keyword}
          onChange={(e) => onChange({ keyword: e.target.value })}
          onKeyDown={(e) => { if (e.key === 'Enter') onSearch(); }}
          placeholder={t('kibana.keywordPh')}
        />
        <input
          className="input input-sm kb-keyword"
          value={value.excludeKeyword}
          onChange={(e) => onChange({ excludeKeyword: e.target.value })}
          onKeyDown={(e) => { if (e.key === 'Enter') onSearch(); }}
          placeholder={t('kibana.excludePh')}
        />
        <div className="kb-levels">
          {LEVELS.map((lv) => {
            const on = value.levels.includes(lv);
            return (
              <button
                key={lv}
                className={`kb-level kb-level-${lv.toLowerCase()}${on ? ' on' : ''}`}
                onClick={() => onChange({
                  levels: on ? value.levels.filter((x) => x !== lv)
                             : [...value.levels, lv],
                })}
              >{lv}</button>
            );
          })}
        </div>
        <div className="spacer" />
        {fieldsErr && <span className="kb-err">{fieldsErr}</span>}
        <button className="btn btn-ghost btn-sm" onClick={loadFields}
                title={t('kibana.reloadFields')}>↻</button>
        <button className="btn btn-primary btn-sm" onClick={onSearch} disabled={busy}>
          {busy ? t('common.loading') : t('kibana.search')}
        </button>
      </div>
    </div>
  );
}

/** 站点管理弹窗：增删改 + 连通性测试。 */
export function KibanaSiteModal({ onClose }: { onClose: () => void }) {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);
  const [sites, setSites] = useState<KibanaSite[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [editing, setEditing] = useState<KibanaSite | null>(null);
  const [form, setForm] = useState<Record<string, any>>({});
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);

  const reload = useCallback(async () => {
    try {
      const d = await apiGet<KibanaSitesResp>('/api/kibana/sites');
      setSites(d.sites || []);
      setCurrent(d.current ?? null);
    } catch (ex: any) {
      addToast(ex.message || String(ex), 'error');
    }
  }, [addToast]);

  useEffect(() => { reload(); }, [reload]);

  const startEdit = (s?: KibanaSite) => {
    setTestResult(null);
    setEditing(s || null);
    setForm(s ? { ...s } : {
      name: '', label: '', base_url: '', username: 'elastic', password: '',
      index_pattern: 'logstash-*', time_field: 'es_time', msg_field: 'log',
      field_prefix: 'kubernetes', verify_ssl: false, timeout: 30,
    });
  };

  const save = async () => {
    if (!(form.name || '').trim()) {
      addToast(t('kibana.site.nameRequired'), 'warn');
      return;
    }
    try {
      await apiPost('/api/kibana/sites', form);
      addToast(t('kibana.site.saved'), 'success');
      await reload();
      setEditing(null);
    } catch (ex: any) {
      addToast(ex.message || String(ex), 'error');
    }
  };

  const remove = async (name: string) => {
    if (!window.confirm(t('kibana.site.confirmDelete', { name }))) return;
    try {
      await apiPost('/api/kibana/sites/delete', { name });
      await reload();
    } catch (ex: any) {
      addToast(ex.message || String(ex), 'error');
    }
  };

  const switchTo = async (name: string) => {
    try {
      await apiPost('/api/kibana/sites/switch', { name });
      setCurrent(name);
      await reload();
    } catch (ex: any) {
      addToast(ex.message || String(ex), 'error');
    }
  };

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const payload: Record<string, any> = editing?.name
        ? { name: editing.name }
        : { ...form };
      const d = await apiPost<any>('/api/kibana/test', payload);
      setTestResult(d);
    } catch (ex: any) {
      setTestResult({ ok: false, error: ex.message || String(ex) });
    } finally {
      setTesting(false);
    }
  };

  const set = (k: string, v: any) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal kb-site-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <span>{t('kibana.manageSites')}</span>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>

        <div className="modal-body kb-site-body">
          <div className="kb-site-list">
            <div className="kb-site-list-head">
              <span>{t('kibana.site.list')}</span>
              <button className="btn btn-ghost btn-sm" onClick={() => startEdit()}>
                + {t('kibana.site.add')}
              </button>
            </div>
            {sites.length === 0 && (
              <div className="empty-hint">{t('kibana.site.none')}</div>
            )}
            {sites.map((s) => (
              <div key={s.name}
                   className={`kb-site-item${s.name === current ? ' current' : ''}`}>
                <div className="kb-site-main" onClick={() => switchTo(s.name)}>
                  <div className="kb-site-title">
                    {s.label || s.name}
                    {s.name === current && <span className="kb-cur-tag">●</span>}
                  </div>
                  <div className="kb-site-sub">{s.base_url}</div>
                  <div className="kb-site-sub">
                    {s.username || '—'} · {s.index_pattern} · {s.time_field}
                  </div>
                </div>
                <div className="kb-site-ops">
                  <button className="btn btn-ghost btn-sm"
                          onClick={() => startEdit(s)}>{t('common.edit')}</button>
                  <button className="btn btn-ghost btn-sm"
                          onClick={() => remove(s.name)}>{t('common.delete')}</button>
                </div>
              </div>
            ))}
          </div>

          {editing !== null && (
            <div className="kb-site-form">
              <div className="k8s-form-grid">
                <label>{t('kibana.site.name')}
                  <input className="input input-sm" value={form.name || ''}
                         disabled={!!sites.find((x) => x.name === form.name)}
                         onChange={(e) => set('name', e.target.value)} /></label>
                <label>{t('kibana.site.label')}
                  <input className="input input-sm" value={form.label || ''}
                         onChange={(e) => set('label', e.target.value)} /></label>
                <label className="kb-span2">{t('kibana.site.baseUrl')}
                  <input className="input input-sm" value={form.base_url || ''}
                         placeholder="http://host/kibana"
                         onChange={(e) => set('base_url', e.target.value)} /></label>
                <label>{t('kibana.site.username')}
                  <input className="input input-sm" value={form.username || ''}
                         onChange={(e) => set('username', e.target.value)} /></label>
                <label>{t('kibana.site.password')}
                  <input className="input input-sm" type="password"
                         value={form.password || ''}
                         placeholder={editing ? t('kibana.site.keepPassword') : ''}
                         onChange={(e) => set('password', e.target.value)} /></label>
                <label>{t('kibana.site.indexPattern')}
                  <input className="input input-sm" value={form.index_pattern || ''}
                         onChange={(e) => set('index_pattern', e.target.value)} /></label>
                <label>{t('kibana.site.timeField')}
                  <input className="input input-sm" value={form.time_field || ''}
                         onChange={(e) => set('time_field', e.target.value)} /></label>
                <label>{t('kibana.site.msgField')}
                  <input className="input input-sm" value={form.msg_field || ''}
                         onChange={(e) => set('msg_field', e.target.value)} /></label>
                <label>{t('kibana.site.fieldPrefix')}
                  <input className="input input-sm" value={form.field_prefix || ''}
                         onChange={(e) => set('field_prefix', e.target.value)} /></label>
                <label>{t('kibana.site.timeout')}
                  <input className="input input-sm" type="number"
                         value={form.timeout ?? 30}
                         onChange={(e) => set('timeout', Number(e.target.value))} /></label>
                <label className="chk">
                  <input type="checkbox" checked={!!form.verify_ssl}
                         onChange={(e) => set('verify_ssl', e.target.checked)} />
                  {t('kibana.site.verifySsl')}
                </label>
              </div>

              <div className="kb-modal-actions">
                <button className="btn btn-ghost btn-sm" onClick={test}
                        disabled={testing}>
                  {testing ? t('common.testing') : t('kibana.site.test')}
                </button>
                <div className="spacer" />
                <button className="btn btn-ghost btn-sm"
                        onClick={() => setEditing(null)}>{t('common.cancel')}</button>
                <button className="btn btn-primary btn-sm" onClick={save}>
                  {t('common.save')}
                </button>
              </div>

              {testResult && (
                <div className={`kb-test-result${testResult.ok ? ' ok' : ' bad'}`}>
                  {!testResult.ok && (
                    <div className="kb-test-err">{testResult.error}</div>
                  )}
                  {(testResult.steps || []).map((s: any) => (
                    <div key={s.name} className="kb-test-step">
                      <span className={s.ok ? 'dot ok' : 'dot bad'} />
                      {s.detail}
                    </div>
                  ))}
                  {testResult.ok && (
                    <div className="kb-test-step">
                      {t('kibana.site.testOk', { n: testResult.total ?? 0 })}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function useKibanaSites() {
  const [sites, setSites] = useState<KibanaSite[]>([]);
  const [current, setCurrent] = useState('');
  const reload = useCallback(async () => {
    try {
      const d = await apiGet<KibanaSitesResp>('/api/kibana/sites');
      const list = d.sites || [];
      setSites(list);
      setCurrent(d.current || (list[0]?.name ?? ''));
    } catch { /* 静默：面板里有各自的错误提示 */ }
  }, []);
  useEffect(() => { reload(); }, [reload]);
  return { sites, current, setCurrent, reload };
}

export { TIME_PRESETS, LEVELS };
