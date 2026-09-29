import { useCallback, useEffect, useRef, useState } from 'react';
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

/** 在独立全屏窗口打开站点管理器（/?view=kibana-sites）：Electron 走原生窗口 IPC，浏览器 window.open 兜底。 */
export function openSiteManagerWindow(title: string, siteName?: string) {
  const q = new URLSearchParams({ view: 'kibana-sites' });
  if (siteName) q.set('site', siteName);
  const path = `/?${q.toString()}`;
  const api = (window as unknown as {
    electronAPI?: { openAppWindow?: (path: string, title: string, maximize?: boolean) => void };
  }).electronAPI;
  if (api?.openAppWindow) {
    api.openAppWindow(path, title, true);
    return;
  }
  window.open(`${window.location.origin}${path}`, '_blank');
}

/**
 * 站点管理器（独立窗口 /?view=kibana-sites）：列表 + 编辑表单并排，
 * 编辑 / 新增都在本窗口内就地完成（不再二级弹窗/再开窗口）。
 * 支持 ?site=<name> 打开即编辑某站点、?add=1 打开即新增；
 * 未编辑时右侧显示引导占位（不留空白区）。
 */
function SiteManager() {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);
  const [sites, setSites] = useState<KibanaSite[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [editing, setEditing] = useState<KibanaSite | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<Record<string, any>>({});
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);

  // URL 参数：?site=<name> 编辑 / ?add=1 新增
  const initialSite = new URLSearchParams(window.location.search).get('site') || null;
  const initialAdd = new URLSearchParams(window.location.search).get('add') === '1';

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
    if (s) {
      // 编辑已有站点：editing 持有该站点，表单据此渲染
      setAdding(false);
      setEditing(s);
      setForm({ ...s });
    } else {
      // 新增：editing 保持 null，用 adding 标记打开空白表单
      setAdding(true);
      setEditing(null);
      setForm({
        name: '', label: '', base_url: '', username: 'elastic', password: '',
        index_pattern: 'logstash-*', time_field: 'es_time', msg_field: 'log',
        field_prefix: 'kubernetes', verify_ssl: false, timeout: 30,
      });
    }
  };

  // 站点列表首次就绪后，按 URL 参数自动打开对应表单（只执行一次）
  const bootedRef = useRef(false);
  useEffect(() => {
    if (bootedRef.current || sites.length === 0) return;
    bootedRef.current = true;
    if (initialAdd) {
      startEdit();
    } else if (initialSite) {
      const s = sites.find((x) => x.name === initialSite);
      if (s) startEdit(s);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sites]);

  const closeForm = () => {
    setAdding(false);
    setEditing(null);
    setTestResult(null);
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
      closeForm();
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

  const listEl = (
    <div className="kb-site-list">
      <div className="kb-site-list-head">
        <span className="kb-site-list-title">{t('kibana.site.list')}</span>
        <button className="btn" onClick={() => startEdit()} title={t('kibana.site.add')}>
          + {t('kibana.site.add')}
        </button>
      </div>
      <div className="kb-site-list-scroll">
        {sites.length === 0 && (
          <div className="kb-site-empty">
            <div className="kb-site-empty-icon">📡</div>
            <div className="kb-site-empty-hint">{t('kibana.site.none')}</div>
          </div>
        )}
        {sites.map((s) => (
          <div key={s.name}
               className={`kb-site-item${s.name === current ? ' current' : ''}`}>
            <div className="kb-site-main" onClick={() => switchTo(s.name)}>
              <div className="kb-site-title">
                <span className="kb-site-title-name">{s.label || s.name}</span>
                {s.name === current && <span className="kb-cur-tag">{t('kibana.site.current')}</span>}
              </div>
              <div className="kb-site-sub">{s.base_url}</div>
              <div className="kb-site-sub">
                {s.username || '—'} · {s.index_pattern} · {s.time_field}
              </div>
            </div>
            <div className="kb-site-ops">
              <button className="kb-site-op-btn"
                      onClick={() => startEdit(s)} title={t('common.edit')} aria-label={t('common.edit')}>✎</button>
              <button className="kb-site-op-btn danger"
                      onClick={() => remove(s.name)} title={t('common.delete')} aria-label={t('common.delete')}>🗑</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );

  const formEl = (editing !== null || adding) && (
    <div className="kb-site-form">
      <div className="kb-site-form-scroll">
        <div className="kb-site-section">
          <h4 className="kb-site-section-title">{t('kibana.site.sectionBasic')}</h4>
          <div className="kb-site-form-grid">
            <label className="kb-required">
              {t('kibana.site.name')}
              <input className="input input-sm" value={form.name || ''}
                     disabled={!!sites.find((x) => x.name === form.name)}
                     onChange={(e) => set('name', e.target.value)} />
              <span className="kb-help">{t('kibana.site.nameHint')}</span>
            </label>
            <label>
              {t('kibana.site.label')}
              <input className="input input-sm" value={form.label || ''}
                     onChange={(e) => set('label', e.target.value)} />
              <span className="kb-help">{t('kibana.site.labelHint')}</span>
            </label>
            <label className="kb-span2 kb-required">
              {t('kibana.site.baseUrl')}
              <input className="input input-sm" value={form.base_url || ''}
                     placeholder="http://host:5601/kibana"
                     onChange={(e) => set('base_url', e.target.value)} />
              <span className="kb-help">{t('kibana.site.baseUrlHint')}</span>
            </label>
          </div>
        </div>

        <div className="kb-site-section">
          <h4 className="kb-site-section-title">{t('kibana.site.sectionAuth')}</h4>
          <div className="kb-site-form-grid">
            <label>
              {t('kibana.site.username')}
              <input className="input input-sm" value={form.username || ''}
                     onChange={(e) => set('username', e.target.value)} />
            </label>
            <label>
              {t('kibana.site.password')}
              <input className="input input-sm" type="password"
                     value={form.password || ''}
                     placeholder={editing ? t('kibana.site.keepPassword') : ''}
                     onChange={(e) => set('password', e.target.value)} />
              <span className="kb-help">{t('kibana.site.passwordHint')}</span>
            </label>
          </div>
        </div>

        <div className="kb-site-section">
          <h4 className="kb-site-section-title">{t('kibana.site.sectionQuery')}</h4>
          <div className="kb-site-form-grid">
            <label>
              {t('kibana.site.indexPattern')}
              <input className="input input-sm" value={form.index_pattern || ''}
                     onChange={(e) => set('index_pattern', e.target.value)} />
            </label>
            <label>
              {t('kibana.site.timeField')}
              <input className="input input-sm" value={form.time_field || ''}
                     onChange={(e) => set('time_field', e.target.value)} />
            </label>
            <label>
              {t('kibana.site.msgField')}
              <input className="input input-sm" value={form.msg_field || ''}
                     onChange={(e) => set('msg_field', e.target.value)} />
            </label>
            <label>
              {t('kibana.site.fieldPrefix')}
              <input className="input input-sm" value={form.field_prefix || ''}
                     onChange={(e) => set('field_prefix', e.target.value)} />
            </label>
          </div>
        </div>

        <div className="kb-site-section">
          <h4 className="kb-site-section-title">{t('kibana.site.sectionAdvanced')}</h4>
          <div className="kb-site-form-grid">
            <label>
              {t('kibana.site.timeout')}
              <input className="input input-sm" type="number"
                     min={1} max={300}
                     value={form.timeout ?? 30}
                     onChange={(e) => set('timeout', Number(e.target.value))} />
            </label>
            <label className="kb-chk">
              <input type="checkbox" checked={!!form.verify_ssl}
                     onChange={(e) => set('verify_ssl', e.target.checked)} />
              {t('kibana.site.verifySsl')}
            </label>
          </div>
        </div>

        {testResult && (
          <div className={`kb-test-result${testResult.ok ? ' ok' : ' bad'}`}>
            <div className="kb-test-result-head">
              <span>{testResult.ok ? '✓' : '✕'}</span>
              <span>{testResult.ok ? t('kibana.site.testOk', { n: testResult.total ?? 0 }) : t('kibana.site.testFail')}</span>
            </div>
            {!testResult.ok && testResult.error && (
              <div className="kb-test-err">{testResult.error}</div>
            )}
            {(testResult.steps || []).map((s: any) => (
              <div key={s.name} className="kb-test-step">
                <span className={s.ok ? 'dot ok' : 'dot bad'} />
                {s.detail}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="kb-site-form-foot">
        <button className="btn btn-ghost" onClick={test} disabled={testing}>
          {testing ? t('common.testing') : t('kibana.site.test')}
        </button>
        <div className="spacer" />
        <button className="btn btn-ghost"
                onClick={closeForm}>{t('common.cancel')}</button>
        <button className="btn btn-primary" onClick={save}>
          {t('common.save')}
        </button>
      </div>
    </div>
  );

  // 未编辑 / 未新增时的右侧占位：给引导而不是留空白
  const placeholderEl = (
    <div className="kb-site-placeholder">
      <div className="kb-site-placeholder-icon" aria-hidden>🛰️</div>
      <div className="kb-site-placeholder-text">{t('kibana.site.pickHint')}</div>
      <button className="btn btn-primary" onClick={() => startEdit()}>
        + {t('kibana.site.add')}
      </button>
    </div>
  );

  return (
    <div className="modal kb-site-modal kb-sites-window">
      <div className="modal-header">
        <span className="kb-modal-title-icon" aria-hidden>🛰️</span>
        <span className="kb-modal-title-text">
          <strong>{t('kibana.manageSites')}</strong>
          <small>{sites.length} · {current ? sites.find((x) => x.name === current)?.label || current : t('kibana.noSite')}</small>
        </span>
        <button className="btn btn-ghost btn-sm" onClick={() => window.close()}
                aria-label={t('common.close')}>{t('common.close')}</button>
      </div>
      <div className="modal-body kb-site-body">
        {listEl}
        {(editing !== null || adding) ? formEl : placeholderEl}
      </div>
    </div>
  );
}

/** 独立窗口全页入口（main.tsx 按 ?view=kibana-sites 路由）：列表 + 完整编辑表单。 */
export function KibanaSitesView() {
  return (
    <div className="app-shell app-shell--detail">
      <main className="workspace">
        <div className="workspace-body">
          <SiteManager />
        </div>
      </main>
    </div>
  );
}

export function useKibanaSites(includeServers = false) {
  const [sites, setSites] = useState<KibanaSite[]>([]);
  const [current, setCurrent] = useState('');
  const reload = useCallback(async () => {
    try {
      // includeServers=true：额外并入由 cf_accounts / hcm_whitelist 派生的「服务器站点」
      const d = await apiGet<KibanaSitesResp>(
        `/api/kibana/sites${includeServers ? '?servers=1' : ''}`
      );
      const list = d.sites || [];
      setSites(list);
      // 不覆盖用户当前选择（reload 时保留已选站点）
      setCurrent((prev) => prev || d.current || (list[0]?.name ?? ''));
    } catch { /* 静默：面板里有各自的错误提示 */ }
  }, [includeServers]);
  useEffect(() => { reload(); }, [reload]);
  return { sites, current, setCurrent, reload };
}

export { TIME_PRESETS, LEVELS };
