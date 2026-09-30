import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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

const REFRESH_OPTS = [0, 5, 10, 15, 30, 60, 120, 300];

// 偏好持久化：自动刷新间隔 / 换行 / 子页签（此前刷新页面即丢，审计 P2 项）
const PREF_KEY = 'kibana.panelPrefs';

function loadPrefs(): { refreshSec: number; wrap: boolean; sub: SubTab } {
  try {
    const raw = localStorage.getItem(PREF_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      return {
        refreshSec: REFRESH_OPTS.includes(p.refreshSec) ? p.refreshSec : 0,
        wrap: p.wrap !== false,
        sub: p.sub === 'discover' ? 'discover' : 'explorer',
      };
    }
  } catch { /* ignore */ }
  return { refreshSec: 0, wrap: true, sub: 'explorer' };
}

export function KibanaPanel() {
  const { t } = useT();
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);

  const prefs = useMemo(loadPrefs, []);
  const [sites, setSites] = useState<{ name: string; label?: string }[]>([]);
  const [site, setSite] = useState('');
  // query = 已提交（驱动实际查询）；draft = 编辑中（改动不立即打后端，点「查询」才生效）
  const [query, setQuery] = useState<KibanaQuery>(DEFAULT_QUERY);
  const [draft, setDraft] = useState<KibanaQuery>(DEFAULT_QUERY);
  const [sub, setSub] = useState<SubTab>(prefs.sub);
  const [wrap, setWrap] = useState(prefs.wrap);
  const [refreshSec, setRefreshSec] = useState(prefs.refreshSec);
  const [reloadKey, setReloadKey] = useState(0);
  // 子面板并发请求数：>0 时「查询」按钮转圈（计数制，布尔会互相覆盖）
  const [busyCount, setBusyCount] = useState(0);
  // 时间范围历史栈：直方图点柱钻取时把上一个范围压栈，「⤺ 返回」逐级弹出
  const [rangeStack, setRangeStack] = useState<{ start: string; end: string }[]>([]);
  const rangeStackRef = useRef(rangeStack);
  rangeStackRef.current = rangeStack;

  const addBusy = useCallback((delta: 1 | -1) => {
    setBusyCount((c) => Math.max(0, c + delta));
  }, []);

  // 偏好持久化
  useEffect(() => {
    try { localStorage.setItem(PREF_KEY, JSON.stringify({ refreshSec, wrap, sub })); }
    catch { /* ignore */ }
  }, [refreshSec, wrap, sub]);

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

  /** 编辑筛选条件：只写 draft，不打后端（一次查询数秒，逐项联动会连打多次） */
  const changeDraft = useCallback((patch: Partial<KibanaQuery>) => {
    setDraft((q) => ({ ...q, ...patch }));
  }, []);

  // 始终指向最新值，供 apply 比较（避免闭包 stale）
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const queryRef = useRef(query);
  queryRef.current = query;

  /** 应用草稿并触发查询（「查询」按钮 / 关键词回车 / 直方图点选时间段）。
   *  条件有变化 → 提交新 query（子面板 reqKey 变化自动重查）；
   *  条件没变化 → reloadKey+1 强制刷新（等价于「再查一次」）。
   *  override 按白名单挑字段：防止误传事件对象等非纯数据（含 DOM 引用）进入
   *  state——那会让 dirty 比较用的 JSON.stringify 因循环结构抛异常白屏。 */
  const applyInternal = useCallback((override?: Partial<KibanaQuery>, opts?: { noRangePush?: boolean }) => {
    const patch: Partial<KibanaQuery> = {};
    if (override) {
      for (const k of Object.keys(DEFAULT_QUERY) as (keyof KibanaQuery)[]) {
        const v = override[k];
        if (v !== undefined) (patch as Record<string, unknown>)[k] = v;
      }
    }
    // 时间范围被显式改变（直方图钻取/自定义输入应用）时，把旧范围压栈供「返回」
    if (!opts?.noRangePush && patch.start !== undefined && patch.start !== queryRef.current.start) {
      setRangeStack((s) => [
        ...s.slice(-9),
        { start: queryRef.current.start, end: queryRef.current.end },
      ]);
    }
    const next = { ...draftRef.current, ...patch };
    setDraft(next);
    if (JSON.stringify(queryRef.current) === JSON.stringify(next)) {
      setReloadKey((k) => k + 1);
    } else {
      setQuery(next);
    }
  }, []);

  /** 对外应用入口（查询按钮 / 回车）：改变时间范围时压栈 */
  const apply = useCallback((override?: Partial<KibanaQuery>) => applyInternal(override), [applyInternal]);

  /** 直方图钻取的「⤺ 返回」：弹出上一级时间范围并应用（不再重复压栈） */
  const popRange = useCallback(() => {
    const stack = rangeStackRef.current;
    const last = stack[stack.length - 1];
    if (!last) return;
    setRangeStack((s) => s.slice(0, -1));
    applyInternal({ start: last.start, end: last.end }, { noRangePush: true });
  }, [applyInternal]);

  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(query), [draft, query]);

  const ctx = {
    sites, site, setSite,
    reloadSites,
    query, setQuery: changeDraft,
    pushLog, addToast,
    reloadKey, busy: busyCount > 0, addBusy,
    openSiteModal: () => openSiteManagerWindow(t('kibana.manageSites')),
  };

  return (
    <KibanaContext.Provider value={ctx as any}>
      <div className="kibana-panel">
        <div className="kibana-topbar">
          <label className="field-inline">
            {t('kibana.siteLabel')}
            <select className="sel" value={site}
                    onChange={(e) => setSite(e.target.value)}>
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
          <label className={`field-inline kb-autoref${refreshSec > 0 ? ' on' : ''}`}
                 title={refreshSec > 0 ? t('kibana.nextRefresh', { s: refreshSec }) : undefined}>
            <span className={`kb-autoref-dot${refreshSec > 0 ? ' run' : ''}`} aria-hidden />
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
          value={draft}
          onChange={changeDraft}
          onApply={apply}
          dirty={dirty}
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
                            refreshSec={refreshSec} reloadKey={reloadKey}
                            onTimeRangePick={apply} />
          </div>
          <div style={{ display: sub === 'discover' ? 'flex' : 'none', flex: 1, minHeight: 0 }}>
            <KibanaDiscover site={site} query={query} wrap={wrap}
                            onToggleWrap={() => setWrap((w) => !w)}
                            refreshSec={refreshSec} reloadKey={reloadKey}
                            onTimeRangePick={apply}
                            rangeStackLen={rangeStack.length}
                            onPopRange={popRange} />
          </div>
        </div>
      </div>
    </KibanaContext.Provider>
  );
}
