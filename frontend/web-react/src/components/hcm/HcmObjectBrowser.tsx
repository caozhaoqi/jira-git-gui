import { Fragment, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useT } from '../../i18n';
import {
  hcmEnvs,
  HcmApiError,
  type HcmEnv,
} from '../../api/hcm/client';
import type { HcmObjectItem, HcmFieldMeta, HcmModelMeta } from '../../api/hcm/types';
import { apiPost } from '../../api/client';
import { writeClipboardText } from '../../utils/clipboard';

const LS_TOKEN = 'hcm.token';

type RightTab = 'fields' | 'json' | 'data';
type MetaKind = 'list' | 'info' | 'view' | 'all';

// 所有服务统一走后端直连（同源 /api/hcm/direct，由后端直连 HCM 网关），彻底摒弃 /hcm-api 代理。
const DIRECT_ENDPOINT = '/api/hcm/direct';

// 数据 JSON 搜索：保留全部行，仅对命中子串包裹 <mark>（不过滤、不隐藏行）。
function hcmHighlightLine(text: string, q: string): ReactNode {
  const needle = q.trim().toLowerCase();
  if (!needle) return text;
  const hay = text.toLowerCase();
  const nodes: ReactNode[] = [];
  let last = 0;
  let idx = hay.indexOf(needle);
  while (idx !== -1) {
    if (idx > last) nodes.push(text.slice(last, idx));
    nodes.push(<mark key={nodes.length}>{text.slice(idx, idx + needle.length)}</mark>);
    last = idx + needle.length;
    idx = hay.indexOf(needle, last);
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function HcmObjectBrowser() {
  const { t } = useT();
  const [token, setToken] = useState(() => localStorage.getItem(LS_TOKEN) || '');
  const [showToken, setShowToken] = useState(false);

  // 可选服务器环境（后端从配置汇总，脱敏）与「使用配置 Token」开关
  const [envs, setEnvs] = useState<HcmEnv[]>([]);
  const [selectedEnv, setSelectedEnv] = useState<string>('');
  const [usePresetToken, setUsePresetToken] = useState(false);
  const [envsError, setEnvsError] = useState('');

  // token 持久化：每次变更（非空）自动写入 localStorage，避免依赖 blur 导致漏存。
  useEffect(() => {
    if (token.trim()) localStorage.setItem(LS_TOKEN, token.trim());
  }, [token]);

  const loadEnvs = useCallback(async () => {
    try {
      const list = await hcmEnvs();
      setEnvs(list);
      setEnvsError('');
    } catch (e: any) {
      setEnvsError(String(e?.message || e));
    }
  }, []);

  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);

  // 所有请求统一走同源 /api/hcm/direct（后端直连网关），固定端点，无代理分支。
  const baseUrl = DIRECT_ENDPOINT;

  // 高级筛选（客户端，基于已加载列表）
  const [advOpen, setAdvOpen] = useState(false);
  const [fClass, setFClass] = useState('');
  const [fCategory, setFCategory] = useState('');

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [items, setItems] = useState<HcmObjectItem[]>([]);
  const [total, setTotal] = useState(0);

  const [selected, setSelected] = useState<HcmObjectItem | null>(null);
  const [rightTab, setRightTab] = useState<RightTab>('fields');
  // 元数据显示维度：list 列表视图 / info 详情视图 / view 表单视图 / all 全部 JSON
  const [metaKind, setMetaKind] = useState<MetaKind>('all');
  const [meta, setMeta] = useState<HcmModelMeta | null>(null);
  const [metaLoading, setMetaLoading] = useState(false);
  const [metaError, setMetaError] = useState('');

  // JSON tab 搜索关键字
  const [jsonQuery, setJsonQuery] = useState('');

  // 字段搜索
  const [fieldQuery, setFieldQuery] = useState('');

  // 复制 JSON 状态
  const [copied, setCopied] = useState(false);

  // 选中对象「数据」查询：调用 hcm.model.list 拉取该对象的记录，以 JSON 展示并可保存
  const [dataResult, setDataResult] = useState<any>(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState('');
  const [dataTotal, setDataTotal] = useState(0);
  const [dataPage, setDataPage] = useState(1);
  const [dataPageSize, setDataPageSize] = useState(20);
  const [dataFilter, setDataFilter] = useState('');
  const [dataJsonQuery, setDataJsonQuery] = useState('');
  const [saveStatus, setSaveStatus] = useState('');
  // 高级数据请求：完整请求体 JSON 编辑、SQL/Profile 调试开关、响应元信息
  const [dataPayload, setDataPayload] = useState('');
  const [dataPayloadError, setDataPayloadError] = useState('');
  const [dataSqlDebug, setDataSqlDebug] = useState(false);
  const [dataProfileDebug, setDataProfileDebug] = useState(false);
  const [dataMeta, setDataMeta] = useState<Record<string, any>>({});
  const [dataAdvOpen, setDataAdvOpen] = useState(false);

  // 挂载时拉取可选服务器环境列表（含直连网关地址）
  useEffect(() => {
    loadEnvs();
  }, [loadEnvs]);

  // 直连模式：前端仍把加密参数交给同源后端 /api/hcm/direct，由后端直连 HCM 网关并解密返回明文。
  // 原因：浏览器对 HCM 网关的真实 POST 响应不带 CORS 头（网关仅在 OPTIONS 预检返回 CORS），
  // 纯浏览器直连会被 CORS 拦截；后端（服务端）发起请求不受 CORS 限制，因此「页面直连」=
  // 前端只负责加密/调同源端点，实际出口由后端直连网关完成。
  const directCall = useCallback(async (apiName: string, params: Record<string, any>, model = '') => {
    const res = await fetch('/api/hcm/direct', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_name: apiName, params, model, token: usePresetToken ? '' : token.trim() }),
    });
    const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok) {
      const detail =
        typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
      throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
    }
    return data?.data;
  }, [token, usePresetToken]);

  // 直连原始响应：返回 {data, meta}，meta 含 srv_begin/srv_end/duration_ms/profile_index/log_index
  const directCallRaw = useCallback(
    async (
      apiName: string,
      params: Record<string, any>,
      model = '',
      opts: { sqlDebug?: boolean; profileDebug?: boolean } = {}
    ): Promise<{ data: any; meta: Record<string, any> }> => {
      const res = await fetch('/api/hcm/direct', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_name: apiName,
          params,
          model,
          token: usePresetToken ? '' : token.trim(),
          sql_debug: opts.sqlDebug,
          profile_debug: opts.profileDebug,
        }),
      });
      const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      if (!res.ok) {
        const detail =
          typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
        throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
      }
      return { data: data?.data, meta: data?.meta || {} };
    },
    [token, usePresetToken]
  );

const loadList = useCallback(async () => {
    if (!usePresetToken && !token.trim()) {
      setError(t('hcm.configRequired'));
      return;
    }
    setLoading(true);
    setError('');
    const q = query.trim();
    try {
      // 统一走后端直连：同源 POST /api/hcm/direct，后端直连网关并解密返回明文。
      // 搜索字段 filter_str 主搜（实测生效），query_str 兼容；额外客户端兜底过滤。
      const res = await directCall('hcm.paas.object.list', {
        model: null, filter_str: q || null, filter_dict: {},
        query_str: q || null,
        page_index: page, page_size: pageSize,
        extra_property: {
          sorts: [],
          filter_params: {
            filter_str: q || null, page_index: page, page_size: pageSize,
            advance_filter_dict: {}, show_fields_key: ['class_', 'model_category', 'update_time'],
            base_object_str: 'hcm.paas.object', key: 'main.setting.hcm_model',
            query_str: q || null,
          },
          only_list: false,
        },
        biz_type: 'list',
      });
      let list = res?.list || [];
      // 客户端搜索兜底：即便网关忽略 query_str（老版本），也按名称/描述/class_ 等过滤，
      // 保证「搜索一定生效」。仅当用户输入了搜索词时启用。
      if (q) {
        const ql = q.toLowerCase();
        const filtered = list.filter((it: HcmObjectItem) =>
          [it.name, it.description, it.class_, it.model_category, it.id]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(ql))
        );
        list = filtered;
      }
      setItems(list);
      setTotal(res?.total ?? res?.count ?? list.length);
    } catch (e: any) {
      setError(e instanceof HcmApiError ? e.message : String(e?.message || e));
      setItems([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, query, t, usePresetToken, token, directCall]);

  useEffect(() => {
    const id = setTimeout(loadList, 250);
    return () => clearTimeout(id);
  }, [loadList]);

  const resetFilter = useCallback(() => {
    setFClass('');
    setFCategory('');
    setPage(1);
  }, []);

  const loadMeta = useCallback(
    async (obj: HcmObjectItem, kind: MetaKind = metaKind) => {
      setSelected(obj);
      setRightTab('fields');
      setMetaKind(kind);
      setMeta(null);
      setMetaError('');
      setFieldQuery('');
      setJsonQuery('');
      // 切换对象时清空已查询的数据，避免串数据
      setDataResult(null);
      setDataError('');
      setDataPage(1);
      setDataFilter('');
      setDataJsonQuery('');
      setDataMeta({});
      setDataPayloadError('');
      setDataPayload(
        JSON.stringify(
          {
            model: obj.id,
            filter_str: null,
            filter_dict: {},
            page_index: 1,
            page_size: 20,
            biz_type: 'list',
          },
          null,
          2
        )
      );
      setMetaLoading(true);
      try {
        // 统一走后端直连：同源 /api/hcm/direct，后端直连网关并解密返回明文元数据。
        const m = await directCall('hcm.model.meta', { model: obj.id, meta_key: kind === 'all' ? '' : kind }, obj.id);
        setMeta(m);
      } catch (e: any) {
        setMetaError(e instanceof HcmApiError ? e.message : String(e?.message || e));
      } finally {
        setMetaLoading(false);
      }
    },
    [directCall, metaKind]
  );

  // 选中对象「数据」查询：调用 hcm.model.list 拉取该对象的记录（非元数据）。
  // 支持完整请求体 JSON 编辑、SQL/Profile 调试开关、响应元信息。
  const loadData = useCallback(
    async (obj: HcmObjectItem, pg: number = dataPage) => {
      if (!usePresetToken && !token.trim()) {
        setDataError(t('hcm.configRequired'));
        return;
      }
      let payload: Record<string, any>;
      try {
        payload = dataPayload.trim() ? JSON.parse(dataPayload) : {};
        setDataPayloadError('');
      } catch (e: any) {
        setDataPayloadError(`JSON 解析失败：${e.message || e}`);
        return;
      }
      // 兜底：确保 model 与当前对象一致，page_index 跟随分页
      payload.model = obj.id;
      payload.page_index = pg;
      if (!payload.page_size) payload.page_size = dataPageSize;
      if (!payload.biz_type) payload.biz_type = 'list';

      setDataLoading(true);
      setDataError('');
      try {
        const { data, meta } = await directCallRaw(
          'hcm.model.list',
          payload,
          obj.id,
          { sqlDebug: dataSqlDebug, profileDebug: dataProfileDebug }
        );
        setDataResult(data);
        setDataMeta(meta);
        setDataTotal(data?.total ?? data?.count ?? (Array.isArray(data?.list) ? data.list.length : 0));
      } catch (e: any) {
        setDataError(e instanceof HcmApiError ? e.message : String(e?.message || e));
        setDataResult(null);
        setDataMeta({});
      } finally {
        setDataLoading(false);
      }
    },
    [token, usePresetToken, directCallRaw, dataPayload, dataPageSize, dataPage, dataSqlDebug, dataProfileDebug, t]
  );

  // 切换到「数据」tab 时，若该对象尚未查询过则自动拉取一次
  const ensureData = useCallback(
    (obj: HcmObjectItem) => {
      if (!dataResult && !dataLoading) loadData(obj);
    },
    [dataResult, dataLoading, loadData]
  );

  // 保存数据 JSON：交给后端写盘（logs/hcm_data/），拿到绝对路径并复制到剪贴板
  const saveDataJson = useCallback(async () => {
    if (!dataResult) return;
    const content = JSON.stringify(dataResult, null, 2);
    setSaveStatus(t('hcm.loading'));
    try {
      const res = await apiPost<{ path?: string; filename?: string; size?: number }>(
        '/api/hcm/data/save',
        { model: selected?.id || '', content }
      );
      if (res.path) {
        let copied = false;
        try {
          await writeClipboardText(res.path);
          copied = true;
        } catch {
          /* 剪贴板不可用时仍返回路径 */
        }
        setSaveStatus(`${t('hcm.savedPath')}: ${res.path}${copied ? '（已复制到剪贴板）' : ''}`);
      } else {
        throw new Error('未返回文件路径');
      }
    } catch (e: any) {
      setSaveStatus(`保存失败：${e?.message || e}`);
    }
  }, [dataResult, selected, t]);

  // 支持从 URL 参数 ?hcm-model=<id> 打开时自动定位到该模型（用于双击新窗口场景）
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('hcm-model');
    if (!id) return;
    const t1 = setTimeout(() => {
      const found = items.find((it) => it.id === id);
      if (found) {
        loadMeta(found);
      } else {
        // 列表里没找到就用 id 直接构造一个占位对象去拉 meta
        loadMeta({ id, name: id, description: null, class_: '', model_category: '', i18n: false, update_time: null });
      }
    }, 700);
    return () => clearTimeout(t1);
  }, [items, loadMeta]);

  // 双击对象：在新窗口打开模型详情页（独立页面，主窗口不受影响）
  const openDetailWindow = useCallback(
    (obj: HcmObjectItem) => {
      const url = `/web/?hcm-model=${encodeURIComponent(obj.id)}&hcm-detail=1`;
      const w = window.open(url, `hcm-${obj.id}`, 'width=1100,height=820,menubar=no,toolbar=no,location=no');
      if (w) {
        // 把当前 token 传给新窗口，让它自己能拉数据（所有连接均走直连，无需传模式）
        try {
          w.localStorage.setItem(LS_TOKEN, token.trim());
        } catch {
          /* 跨窗口 localStorage 可能受限，新窗口会自行提示填 token */
        }
        w.focus();
      }
    },
    [token]
  );

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const hasFilter = Boolean(fClass.trim() || fCategory.trim());

  // 高级筛选：基于已加载列表做客户端前缀/精确匹配（后端 advance_filter_dict 对该视图不生效）
  const clientFiltered = useMemo(() => {
    const fc = fClass.trim().toLowerCase();
    const fcat = fCategory.trim().toLowerCase();
    if (!fc && !fcat) return items;
    return items.filter((it) => {
      const cls = (it.class_ || '').toLowerCase();
      const cat = (it.model_category || '').toLowerCase();
      const clsOk = !fc || cls.startsWith(fc) || cls.includes(fc);
      const catOk = !fcat || cat.startsWith(fcat) || cat.includes(fcat);
      return clsOk && catOk;
    });
  }, [items, fClass, fCategory]);

  const shownItems = hasFilter ? clientFiltered : items;

  // 字段搜索过滤
  const filteredFields = useMemo(() => {
    const q = fieldQuery.trim().toLowerCase();
    const fields = meta?.fields || [];
    if (!q) return fields;
    return fields.filter((f: HcmFieldMeta) =>
      [f.key, f.name, f.type, f.description]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q))
    );
  }, [meta, fieldQuery]);

  // JSON tab 搜索：按关键字过滤（仅显示匹配到的行，含其上下文），并统计命中行
  const jsonView = useMemo(() => {
    if (!meta) return { text: '', lines: [] as string[], matched: 0 };
    const raw = JSON.stringify(meta, null, 2);
    const lines = raw.split('\n');
    const q = jsonQuery.trim().toLowerCase();
    if (!q) return { text: raw, lines, matched: lines.length };
    const matchedLines = lines.filter((ln) => ln.toLowerCase().includes(q));
    return { text: matchedLines.join('\n'), lines, matched: matchedLines.length };
  }, [meta, jsonQuery]);

  // 数据 JSON 搜索：保留全部行，仅高亮命中（不过滤），并统计命中行数
  const dataView = useMemo(() => {
    if (!dataResult) return { lines: [] as string[], matched: 0 };
    const raw = JSON.stringify(dataResult, null, 2);
    const lines = raw.split('\n');
    const q = dataJsonQuery.trim().toLowerCase();
    if (!q) return { lines, matched: 0 };
    const matched = lines.filter((ln) => ln.toLowerCase().includes(q)).length;
    return { lines, matched };
  }, [dataResult, dataJsonQuery]);

  // 复制 JSON
  const copyJson = useCallback(async () => {
    if (!meta) return;
    const text = JSON.stringify(meta, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 降级：用临时 textarea
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      } catch {
        setError(t('hcm.configRequired'));
      }
    }
  }, [meta, t]);

  return (
    <div className="hcm-browser">
      {/* 配置条 */}
      <div className="hcm-config">
        <label className="hcm-config-readonly">
          <span>{t('hcm.baseUrl')}</span>
          <input
            value={baseUrl}
            readOnly
            title="所有连接统一走后端直连 /api/hcm/direct（由后端直连 HCM 网关并解密，无需代理）"
            spellCheck={false}
          />
        </label>
        <label>
          <span>{t('hcm.serverEnv')}</span>
          <select
            value={selectedEnv}
            onChange={(e) => setSelectedEnv(e.target.value)}
            title={envsError || t('hcm.serverEnvHint')}
          >
            <option value="">{t('hcm.serverEnvSelect')}</option>
            {envs.map((ev) => (
              <option key={ev.key} value={ev.key}>
                {ev.name}（{ev.server_url}）
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t('hcm.token')}</span>
          <input
            type={showToken ? 'text' : 'password'}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={t('hcm.tokenPlaceholder')}
            spellCheck={false}
            disabled={usePresetToken}
          />
        </label>
        <button className="btn btn-sm" onClick={() => setShowToken((v) => !v)} disabled={usePresetToken}>
          {showToken ? t('hcm.hide') : t('hcm.show')}
        </button>
        <button
          className={`btn btn-sm ${usePresetToken ? 'btn-primary' : ''}`}
          onClick={() => setUsePresetToken((v) => !v)}
          title={t('hcm.usePresetHint')}
        >
          {usePresetToken ? t('hcm.presetOn') : t('hcm.presetOff')}
        </button>
        <button className="btn btn-sm btn-primary" onClick={loadList} disabled={loading}>
          {loading ? t('hcm.loading') : t('hcm.refresh')}
        </button>
      </div>

      {error && (
        <div className="hcm-error" role="alert">
          <span>{error}</span>
          {String(error).includes('51006') && (
            <div className="hcm-error-hint">{t('hcm.tokenExpired')}</div>
          )}
          {String(error).includes('规则校验不通过') && (
            <div className="hcm-error-hint">{t('hcm.ruleCheckFailed')}</div>
          )}
        </div>
      )}

      <div className="hcm-split">
        {/* 左：对象列表 */}
        <div className="hcm-list">
          <div className="hcm-list-toolbar">
            <input
              className="hcm-search"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
              placeholder={t('hcm.searchPlaceholder')}
              spellCheck={false}
            />
            <button
              className={advOpen ? 'btn btn-sm btn-active' : 'btn btn-sm'}
              onClick={() => setAdvOpen((v) => !v)}
            >
              {t('hcm.advFilter')}
              {hasFilter ? ` (${t('hcm.filterActive')})` : ''}
            </button>
            <span className="hcm-count">
              {t('hcm.total')}: {hasFilter ? `${shownItems.length} / ${total}` : total}
            </span>
          </div>

          {/* 高级筛选面板（客户端实时筛选） */}
          {advOpen && (
            <div className="hcm-adv">
              <label className="hcm-adv-row">
                <span>{t('hcm.advClass')}</span>
                <input
                  value={fClass}
                  onChange={(e) => {
                    setFClass(e.target.value);
                    setPage(1);
                  }}
                  placeholder="core.ds / core.paas …"
                  spellCheck={false}
                />
              </label>
              <label className="hcm-adv-row">
                <span>{t('hcm.advCategory')}</span>
                <input
                  value={fCategory}
                  onChange={(e) => {
                    setFCategory(e.target.value);
                    setPage(1);
                  }}
                  placeholder="object / config …"
                  spellCheck={false}
                />
              </label>
              <div className="hcm-adv-actions">
                <button className="btn btn-sm btn-primary" onClick={() => setAdvOpen(false)}>
                  {t('hcm.advApply')}
                </button>
                <button className="btn btn-sm" onClick={resetFilter} disabled={!hasFilter}>
                  {t('hcm.advReset')}
                </button>
              </div>
            </div>
          )}

          <div className="hcm-list-body">
            <table className="hcm-table">
              <thead>
                <tr>
                  <th>{t('hcm.colName')}</th>
                  <th>{t('hcm.colDesc')}</th>
                  <th>{t('hcm.colClass')}</th>
                </tr>
              </thead>
              <tbody>
                {shownItems.map((it) => (
                  <tr
                    key={it.id}
                    className={selected?.id === it.id ? 'hcm-row-active' : ''}
                    onDoubleClick={() => openDetailWindow(it)}
                    onClick={() => loadMeta(it)}
                    title={t('hcm.dblToViewNew')}
                  >
                    <td>{it.name}</td>
                    <td>{it.description || '—'}</td>
                    <td className="hcm-mono">{it.class_}</td>
                  </tr>
                ))}
                {!loading && shownItems.length === 0 && (
                  <tr>
                    <td colSpan={3} className="hcm-empty">
                      {t('hcm.noData')}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            {loading && <div className="hcm-loading">{t('hcm.loading')}…</div>}
          </div>
          <div className="hcm-pager">
            <button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              ‹
            </button>
            <span>
              {page} / {totalPages}
            </span>
            <button className="btn btn-sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
              ›
            </button>
          </div>
        </div>

        {/* 右：字段 / JSON 元数据 */}
        <div className="hcm-detail">
          {!selected && <div className="hcm-empty hcm-detail-empty">{t('hcm.pickHint')}</div>}
          {selected && (
            <>
              <div className="hcm-detail-head">
                <div className="hcm-detail-title">{selected.name}</div>
                <div className="hcm-detail-sub hcm-mono">{selected.id}</div>
              </div>
              <div className="hcm-subtabs">
                <button
                  className={rightTab === 'fields' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => setRightTab('fields')}
                >
                  {t('hcm.tabFields')}
                </button>
                <button
                  className={rightTab === 'json' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => setRightTab('json')}
                >
                  {t('hcm.tabJson')}
                </button>
                <button
                  className={rightTab === 'data' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => {
                    setRightTab('data');
                    if (selected) ensureData(selected);
                  }}
                  disabled={!selected}
                  title="查询选中对象的记录数据并以 JSON 展示"
                >
                  {t('hcm.tabData')}
                </button>
                {/* 元数据显示维度：list / info / view / all（查询所有 JSON 节点） */}
                <span className="hcm-kind-sep" />
                <button
                  className={metaKind === 'list' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => selected && loadMeta(selected, 'list')}
                  disabled={!selected}
                  title="列表视图维度"
                >
                  list
                </button>
                <button
                  className={metaKind === 'info' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => selected && loadMeta(selected, 'info')}
                  disabled={!selected}
                  title="详情视图维度"
                >
                  info
                </button>
                <button
                  className={metaKind === 'view' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => selected && loadMeta(selected, 'view')}
                  disabled={!selected}
                  title="表单视图维度"
                >
                  view
                </button>
                <button
                  className={metaKind === 'all' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                  onClick={() => selected && loadMeta(selected, 'all')}
                  disabled={!selected}
                  title="全部 JSON 节点"
                >
                  all
                </button>
                {rightTab === 'json' && (
                  <button className="btn btn-sm hcm-copy-btn" onClick={copyJson}>
                    {copied ? `✓ ${t('hcm.copied')}` : t('hcm.copyJson')}
                  </button>
                )}
              </div>

              {/* JSON tab 搜索（可搜索所有 JSON 节点文本） */}
              {rightTab === 'json' && (
                <div className="hcm-fields-toolbar">
                  <input
                    className="hcm-search"
                    value={jsonQuery}
                    onChange={(e) => setJsonQuery(e.target.value)}
                    placeholder={t('hcm.jsonSearch')}
                    spellCheck={false}
                  />
                  {jsonQuery.trim() && (
                    <span className="hcm-count">
                      {t('hcm.jsonMatch')}: {jsonView.matched}
                    </span>
                  )}
                </div>
              )}

              {metaLoading && <div className="hcm-loading">{t('hcm.loadingMeta')}…</div>}
              {metaError && <div className="hcm-error">{metaError}</div>}

              {!metaLoading && !metaError && meta && rightTab === 'fields' && (
                <>
                  <div className="hcm-fields-toolbar">
                    <input
                      className="hcm-search"
                      value={fieldQuery}
                      onChange={(e) => setFieldQuery(e.target.value)}
                      placeholder={t('hcm.fieldSearch')}
                      spellCheck={false}
                    />
                    {fieldQuery.trim() && (
                      <span className="hcm-count">
                        {t('hcm.fieldMatch')}: {filteredFields.length}/{meta.fields?.length || 0}
                      </span>
                    )}
                  </div>
                  <div className="hcm-fields">
                    <table className="hcm-table">
                      <thead>
                        <tr>
                          <th>{t('hcm.fKey')}</th>
                          <th>{t('hcm.fName')}</th>
                          <th>{t('hcm.fType')}</th>
                          <th>{t('hcm.fRequired')}</th>
                          <th>{t('hcm.fLen')}</th>
                          <th>{t('hcm.fDesc')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredFields.map((f: HcmFieldMeta) => (
                          <tr key={f.key}>
                            <td className="hcm-mono">{f.key}</td>
                            <td>{f.name}</td>
                            <td className="hcm-mono">{f.type}</td>
                            <td>{f.is_required ? '✓' : ''}</td>
                            <td>{f.length ?? ''}</td>
                            <td>{f.description || '—'}</td>
                          </tr>
                        ))}
                        {filteredFields.length === 0 && (
                          <tr>
                            <td colSpan={6} className="hcm-empty">
                              {t('hcm.noData')}
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </>
              )}

              {!metaLoading && !metaError && meta && rightTab === 'json' && (
                <pre className="hcm-json">
                  {jsonView.text || `（无匹配 "${jsonQuery}" 的 JSON 节点）`}
                </pre>
              )}

              {/* 选中对象「数据」查询：拉取记录并以 JSON 展示 + 保存 */}
              {rightTab === 'data' && (
                <div className="hcm-data">
                  <div className="hcm-fields-toolbar">
                    <input
                      className="hcm-search"
                      value={dataFilter}
                      onChange={(e) => setDataFilter(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && selected) {
                          setDataPage(1);
                          // 将简单过滤同步到请求体
                          try {
                            const p = dataPayload.trim() ? JSON.parse(dataPayload) : {};
                            p.filter_str = e.currentTarget.value.trim() || null;
                            setDataPayload(JSON.stringify(p, null, 2));
                          } catch {}
                          loadData(selected, 1);
                        }
                      }}
                      placeholder={t('hcm.dataFilterPlaceholder')}
                      spellCheck={false}
                    />
                    <button
                      className="btn btn-sm btn-primary"
                      onClick={() => {
                        if (selected) {
                          setDataPage(1);
                          try {
                            const p = dataPayload.trim() ? JSON.parse(dataPayload) : {};
                            p.filter_str = dataFilter.trim() || null;
                            setDataPayload(JSON.stringify(p, null, 2));
                          } catch {}
                          loadData(selected, 1);
                        }
                      }}
                      disabled={!selected || dataLoading}
                    >
                      {dataLoading ? t('hcm.loading') : t('hcm.queryData')}
                    </button>
                    <button
                      className="btn btn-sm hcm-copy-btn"
                      onClick={saveDataJson}
                      disabled={!dataResult}
                      title={t('hcm.saveJson')}
                    >
                      {t('hcm.saveJson')}
                    </button>
                    <button
                      className={dataAdvOpen ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                      onClick={() => setDataAdvOpen((v) => !v)}
                      disabled={!selected}
                    >
                      {t('hcm.advanced')}
                    </button>
                  </div>

                  {/* 高级请求体编辑（参考 HCM API 测试页：完整 JSON 输入 + SQL/Profile 开关） */}
                  {dataAdvOpen && (
                    <div className="hcm-adv">
                      <label className="hcm-adv-row hcm-adv-vertical">
                        <span>{t('hcm.requestBody')}</span>
                        <textarea
                          className="hcm-json-input"
                          value={dataPayload}
                          onChange={(e) => setDataPayload(e.target.value)}
                          spellCheck={false}
                          rows={10}
                          placeholder={t('hcm.requestBodyHint')}
                        />
                      </label>
                      {dataPayloadError && <div className="hcm-error">{dataPayloadError}</div>}
                      <div className="hcm-adv-row hcm-adv-checks">
                        <label title={t('hcm.sqlDebugHint')}>
                          <input
                            type="checkbox"
                            checked={dataSqlDebug}
                            onChange={(e) => setDataSqlDebug(e.target.checked)}
                          />{' '}
                          {t('hcm.sqlDebug')}
                        </label>
                        <label title={t('hcm.profileDebugHint')}>
                          <input
                            type="checkbox"
                            checked={dataProfileDebug}
                            onChange={(e) => setDataProfileDebug(e.target.checked)}
                          />{' '}
                          {t('hcm.profileDebug')}
                        </label>
                      </div>
                    </div>
                  )}

                  {saveStatus && (
                    <div className="hcm-detail-sub hcm-mono" style={{ fontSize: 11, wordBreak: 'break-all', padding: '0 12px 6px' }}>
                      {saveStatus}
                    </div>
                  )}

                  {/* 数据 JSON 内搜索 */}
                  <div className="hcm-fields-toolbar">
                    <input
                      className="hcm-search"
                      value={dataJsonQuery}
                      onChange={(e) => setDataJsonQuery(e.target.value)}
                      placeholder={t('hcm.dataJsonSearch')}
                      spellCheck={false}
                    />
                    {dataJsonQuery.trim() && (
                      <span className="hcm-count">
                        {t('hcm.dataMatch')}: {dataView.matched}
                      </span>
                    )}
                  </div>

                  {dataLoading && <div className="hcm-loading">{t('hcm.loading')}…</div>}
                  {dataError && <div className="hcm-error">{dataError}</div>}

                  {!dataLoading && !dataError && dataResult && (
                    <>
                      <div className="hcm-detail-sub hcm-mono">
                        {t('hcm.dataCount')}: {dataTotal}
                        {typeof dataMeta.duration_ms === 'number' && (
                          <span className="hcm-meta-duration">
                            {' '}
                            · {t('hcm.duration')}: {(dataMeta.duration_ms / 1000).toFixed(3)}s
                          </span>
                        )}
                        {dataMeta.profile_index && (
                          <a
                            className="hcm-meta-link"
                            href={`/document/temp/download?index=${dataMeta.profile_index}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {t('hcm.downloadProfile')}
                          </a>
                        )}
                        {dataMeta.log_index && (
                          <a
                            className="hcm-meta-link"
                            href={`/document/temp/download?index=${dataMeta.log_index}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {t('hcm.downloadLog')}
                          </a>
                        )}
                      </div>
                      <pre className="hcm-json">
                        {dataView.lines.map((ln, i) => (
                          <Fragment key={i}>
                            {hcmHighlightLine(ln, dataJsonQuery)}
                            {'\n'}
                          </Fragment>
                        ))}
                      </pre>
                      <div className="hcm-pager">
                        <button
                          className="btn btn-sm"
                          disabled={dataPage <= 1}
                          onClick={() => {
                            const p = Math.max(1, dataPage - 1);
                            setDataPage(p);
                            if (selected) loadData(selected, p);
                          }}
                        >
                          ‹
                        </button>
                        <span>
                          {dataPage} / {Math.max(1, Math.ceil(dataTotal / dataPageSize))}
                        </span>
                        <button
                          className="btn btn-sm"
                          disabled={dataPage >= Math.ceil(dataTotal / dataPageSize)}
                          onClick={() => {
                            const p = dataPage + 1;
                            setDataPage(p);
                            if (selected) loadData(selected, p);
                          }}
                        >
                          ›
                        </button>
                        <label className="hcm-data-pagesize">
                          <span>{t('hcm.dataPageSize')}</span>
                          <input
                            className="input input-sm"
                            type="number"
                            min={1}
                            max={1000}
                            value={dataPageSize}
                            onChange={(e) => setDataPageSize(parseInt(e.target.value) || 20)}
                            onBlur={() => {
                              if (selected) {
                                setDataPage(1);
                                loadData(selected, 1);
                              }
                            }}
                          />
                        </label>
                      </div>
                    </>
                  )}

                  {!dataLoading && !dataError && !dataResult && (
                    <div className="hcm-empty">{t('hcm.pickHint')}</div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
