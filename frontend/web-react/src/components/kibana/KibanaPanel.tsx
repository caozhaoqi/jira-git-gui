import { useCallback, useEffect, useState } from 'react';
import { useAppStore } from '../../store/useAppStore';
import { useT } from '../../i18n';
import { KibanaContext, DEFAULT_QUERY, type KibanaQuery } from './context';
import { KibanaFilters, openSiteManagerWindow } from './KibanaFilters';
import { KibanaExplorer } from './KibanaExplorer';
import { KibanaDiscover } from './KibanaDiscover';
import { apiGet } from '../../api/client';

type SubTab = 'explorer' | 'discover';
const SUBTABS: { key: SubTab; labelKey: string }[] = [
  { key: 'explorer', labelKey: 'kibana.subtabs.explorer' },
  { key: 'discover', labelKey: 'kibana.subtabs.discover' },
];

const REFRESH_OPTS = [0, 15, 30, 60];

export function KibanaPanel() {
  const { t } = useT();
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);

  const [sites, setSites] = useState<{ name: string; label?: string }[]>([]);
  const [site, setSite] = useState('');
  const [query, setQuery] = useState<KibanaQuery>(DEFAULT_QUERY);
  const [sub, setSub] = useState<SubTab>('explorer');
  const [wrap, setWrap] = useState(true);
  const [refreshSec, setRefreshSec] = useState(0);
  // 「查询」按钮此前是空实现（onSearch={() => {}}），而它是这一页唯一显式的搜索控件。
  // 用一个自增计数触发子面板重新拉取；busy 由子面板回报，用于按钮加载态。
  const [reloadKey, setReloadKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);

  const reloadSites = useCallback(async () => {
    try {
      const d = await apiGet<any>('/api/kibana/sites');
      const list = d.sites || [];
      setSites(list);
      const cur = d.current || (list[0] && list[0].name) || '';
      setSite((prev) => (prev || cur));
    } catch (ex: any) {
      pushLog(t('kibana.loadSitesFail') + ex.message, 'error');
    }
  }, [pushLog, t]);

  useEffect(() => { reloadSites(); }, [reloadSites]);

  // 站点在独立管理窗口（?view=kibana-sites）里增删改，回到本窗口（获得焦点）时自动刷新
  useEffect(() => {
    const onFocus = () => { reloadSites(); };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [reloadSites]);

  const setSiteAndReload = useCallback(async (name: string) => {
    setSite(name);
  }, []);

  const onChange = useCallback((patch: Partial<KibanaQuery>) => {
    setQuery((q) => ({ ...q, ...patch }));
  }, []);

  const ctx = {
    sites, site, setSite: setSiteAndReload, reloadSites,
    query, setQuery: onChange, pushLog, addToast,
    reloadKey, busy, setBusy, loadedOnce, setLoadedOnce,
    openSiteModal: () => openSiteManagerWindow(t('kibana.manageSites')),
  };

  return (
    <KibanaContext.Provider value={ctx as any}>
      <div className="kibana-panel">
        <div className="kibana-topbar">
          <label className="field-inline">
            {t('kibana.siteLabel')}
            <select className="sel" value={site}
                    onChange={(e) => setSiteAndReload(e.target.value)}>
              {sites.length === 0 && <option value="">{t('kibana.noSite')}</option>}
              {sites.map((s) => (
                <option key={s.name} value={s.name}>{s.label || s.name}</option>
              ))}
            </select>
          </label>
          <button className="btn btn-ghost btn-sm"
                  onClick={() => openSiteManagerWindow(t('kibana.manageSites'))}>
            {t('kibana.manageSites')}
          </button>
          <div className="spacer" />
          <label className="field-inline">
            {t('kibana.autoRefresh')}
            <select className="sel" value={refreshSec}
                    onChange={(e) => setRefreshSec(Number(e.target.value))}>
              {REFRESH_OPTS.map((s) => (
                <option key={s} value={s}>
                  {s === 0 ? t('kibana.off') : `${s}s`}
                </option>
              ))}
            </select>
          </label>
        </div>

        <KibanaFilters
          site={site}
          value={query}
          onChange={onChange}
          onSearch={() => setReloadKey((k) => k + 1)}
          busy={busy}
        />

        <div className="kibana-subtabs">
          {SUBTABS.map((st) => (
            <button key={st.key}
                    className={`kibana-subtab${sub === st.key ? ' active' : ''}`}
                    onClick={() => setSub(st.key)}>
              {t(st.labelKey)}
            </button>
          ))}
        </div>

        <div className="kibana-subpane">
          <div style={{ display: sub === 'explorer' ? 'flex' : 'none', flex: 1, minHeight: 0 }}>
            <KibanaExplorer site={site} query={query} wrap={wrap}
                            onToggleWrap={() => setWrap((w) => !w)}
                            refreshSec={refreshSec} reloadKey={reloadKey} />
          </div>
          <div style={{ display: sub === 'discover' ? 'flex' : 'none', flex: 1, minHeight: 0 }}>
            <KibanaDiscover site={site} query={query} wrap={wrap}
                            onToggleWrap={() => setWrap((w) => !w)}
                            refreshSec={refreshSec} reloadKey={reloadKey} />
          </div>
        </div>
      </div>
    </KibanaContext.Provider>
  );
}
