import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { reportPanelOutput } from '../../api/aiBridge';
import { useT } from '../../i18n';
import {
  hcmEnvs,
  HcmApiError,
  type HcmEnv,
} from '../../api/hcm/client';
import type { HcmObjectItem, HcmFieldMeta, HcmModelMeta } from '../../api/hcm/types';
import { reportLocalApiNetworkError } from '../../api/client';
import { writeClipboardText } from '../../utils/clipboard';
import { saveBlob, saveTextFile } from '../../utils/download';
import { EmptyState } from '../EmptyState';
import { ComboboxInput } from '../ComboboxInput';
import { useAppStore } from '../../store/useAppStore';
import { useHcmToken } from '../../hooks/useHcmToken';
import { isTokenLikelyExpired } from '../../api/hcm/errDict';
import { openHcmWindow } from './hcmWindow';
import {
  buildHcmFilterDict,
  buildPersonLookupFilter,
  buildSyncLedgerCsv,
  buildSyncLedgerFilterDict,
  buildSyncLedgerJson,
  entityTypeLabel,
  exportFileName,
  fetchAllRows,
  hasPersonAppointment,
  isSyncLedgerModel,
  metaFilterCandidates,
  parseHcmList,
  parseGroupCount,
  parseTotalCount,
  pickDefaultFilterKey,
  staticSyncSelectOptions,
  syncLedgerFieldsFor,
  SYNC_COPY_MAX_ROWS,
  SYNC_EXPORT_MAX_ROWS,
  SYNC_EXPORT_PAGE_SIZE,
  type HcmFilterCandidate,
  type HcmFilterOperator,
  type SyncLedgerFilterField,
} from '../../utils/hcmObjectFilters';

const LS_ENV = 'hcm.selectedEnv';

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

// 结构化筛选「字段名」输入：用通用 ComboboxInput（自绘联想弹层）替代原生 <datalist>，
// 原生 datalist 弹出列表无法 CSS 定制（Chrome 以系统默认黑体大白底渲染，与全站 .sel 下拉风格断裂）。
// 包一层 .hcm-struct-field-wrap（指定 220px flex 宽度），input 套 .hcm-struct-field。
function StructFieldInput({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  options: HcmFilterCandidate[];
  placeholder: string;
}) {
  const comboOpts = useMemo(
    () => options.map((o) => ({ value: o.key, label: o.label })),
    [options]
  );
  return (
    <ComboboxInput
      value={value}
      onChange={onChange}
      options={comboOpts}
      placeholder={placeholder}
      wrapperClassName="hcm-struct-field-wrap"
      inputClassName="hcm-struct-field"
      ariaLabel={placeholder}
    />
  );
}

export function HcmObjectBrowser() {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);
  // HCM token：全局唯一来源（store 统一持久化到 hcm.token，刷新后自动回填）
  const token = useAppStore((s) => s.hcmToken);
  const setToken = useAppStore((s) => s.setHcmToken);
  const [showToken, setShowToken] = useState(false);

  // 可选服务器环境（后端从配置汇总，脱敏）与「使用配置 Token」开关
  const [envs, setEnvs] = useState<HcmEnv[]>([]);
  const [selectedEnv, setSelectedEnv] = useState<string>(() => localStorage.getItem(LS_ENV) || '');
  const [usePresetToken, setUsePresetToken] = useState(false);
  const [envsError, setEnvsError] = useState('');

  // 根据 key 查当前环境
  const currentEnv = useMemo(() => envs.find((ev) => ev.key === selectedEnv), [envs, selectedEnv]);
  const targetUrl = currentEnv?.server_url || '';

  // HCM token 自动重登：已知网关 targetUrl 时，请求前若 token 过期则静默刷新
  const { refresh: refreshHcmToken } = useHcmToken(targetUrl);

  // 环境选择持久化（token 由 store 负责）
  useEffect(() => {
    localStorage.setItem(LS_ENV, selectedEnv);
  }, [selectedEnv]);

  // 切到没有预设 token 的环境时，自动关闭「用配置 Token」，避免误用空 token
  useEffect(() => {
    if (currentEnv && !currentEnv.has_preset_token && usePresetToken) {
      setUsePresetToken(false);
    }
  }, [currentEnv, usePresetToken]);

  // 取「最新可用 token」：若已过期且知道网关，先自动重登再返回；失败抛错交给上层提示。
  const ensureFreshToken = useCallback(async (): Promise<string> => {
    let tk = useAppStore.getState().hcmToken;
    if (!usePresetToken && isTokenLikelyExpired(tk) && targetUrl) {
      const r = await refreshHcmToken(targetUrl);
      if (r.ok && r.token) tk = r.token;
      else throw new HcmApiError(r.error || t('hcm.tokenReloginFailed'), 401);
    }
    return tk;
  }, [refreshHcmToken, targetUrl, usePresetToken, t]);

  const loadEnvs = useCallback(async () => {
    try {
      const list = await hcmEnvs();
      setEnvs(list);
      setEnvsError('');
      // 首次拿到环境列表时：若本地没有保存过有效 key，默认选第一个（通常是 proxy）
      setSelectedEnv((prev) => {
        const valid = list.some((ev) => ev.key === prev);
        if (prev && valid) return prev;
        return list[0]?.key || '';
      });
    } catch (e: any) {
      setEnvsError(String(e?.message || e));
    }
  }, []);

  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [personLookupOpen, setPersonLookupOpen] = useState(false);
  const [personLookupName, setPersonLookupName] = useState('');
  const [personLookupModels, setPersonLookupModels] = useState<HcmObjectItem[]>([]);
  const [personLookupModelId, setPersonLookupModelId] = useState('');
  const [personLookupResults, setPersonLookupResults] = useState<Record<string, any>[]>([]);
  const [personLookupSearched, setPersonLookupSearched] = useState(false);
  const personHasAppointment = hasPersonAppointment(personLookupResults);
  const [personLookupLoading, setPersonLookupLoading] = useState(false);
  const [personLookupModelsLoading, setPersonLookupModelsLoading] = useState(false);
  const [personLookupError, setPersonLookupError] = useState('');

  // 所有请求统一走同源 /api/hcm/direct（后端直连网关），固定端点，无代理分支。
  const baseUrl = DIRECT_ENDPOINT;

  // 高级筛选（客户端，基于已加载列表）
  const [advOpen, setAdvOpen] = useState(false);
  // 连接设置（环境/令牌）默认折叠：已选过环境就只显示一行摘要；
  // 没选环境（首次使用/换了机器）时保持展开，避免必填项被藏起来
  const [configOpen, setConfigOpen] = useState(false);
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

  // AI 数据桥：对象浏览数据同步给 /api/ai/output/hcm
  useEffect(() => {
    if (dataResult == null && items.length === 0) return;
    reportPanelOutput('hcm', {
      text: JSON.stringify({ objects: items, dataResult }, null, 2).slice(0, 200000),
      data: { kind: 'hcm-object', objectCount: items.length, hasData: dataResult != null },
    });
  }, [dataResult, items]);
  const [dataLoading, setDataLoading] = useState(false);
  const [dataError, setDataError] = useState('');
  const [dataTotal, setDataTotal] = useState(0);
  const [dataPage, setDataPage] = useState(1);
  const [dataPageSize, setDataPageSize] = useState(20);
  const [dataFilter, setDataFilter] = useState('');
  const [dataJsonQuery, setDataJsonQuery] = useState('');
  // 高级数据请求：完整请求体 JSON 编辑、SQL/Profile 调试开关、响应元信息
  const [dataPayload, setDataPayload] = useState('');
  const [dataPayloadError, setDataPayloadError] = useState('');
  const [dataSqlDebug, setDataSqlDebug] = useState(false);
  const [dataProfileDebug, setDataProfileDebug] = useState(false);
  const [dataMeta, setDataMeta] = useState<Record<string, any>>({});
  const [dataAdvOpen, setDataAdvOpen] = useState(false);
  // 结构化筛选（filter_dict）：字段键值对，多条件按 AND 组合，字段联想来自已加载对象的 meta.fields
  const [dataStruct, setDataStruct] = useState<
    { id: number; field: string; operator: HcmFilterOperator; value: string }[]
  >([]);
  // 默认折叠：U_SyncRaw* 之外的对象（普通业务模型）也用不上结构化筛选时，先把行收起；用户要点 ▸ 才会展开条件行。
  const [dataStructOpen, setDataStructOpen] = useState(false);
  const [quickFilterField, setQuickFilterField] = useState('');
  // 同步台帐（U_SyncRaw*）专用筛选：字段值 + select 字段的动态选项（来自 hcm.model.count group_by）
  const [syncFilter, setSyncFilter] = useState<Record<string, string>>({});
  const [syncSelectOptions, setSyncSelectOptions] = useState<
    Record<string, { value: string; count: number }[]>
  >({});
  // 复制/导出：当前 Data 标签页下「当前筛选结果」外层工具条的状态
  // （不区分台帐/通用模型——以最新 dataPayload 为基准）。
  const [dataExportFormat, setDataExportFormat] = useState<'csv' | 'json'>('csv');
  const [dataExportRaw, setDataExportRaw] = useState(false);
  const [dataExportBusy, setDataExportBusy] = useState<'' | 'export' | 'copy'>('');
  const [dataExportStatus, setDataExportStatus] = useState('');
  const dataExporting = dataExportBusy !== '';
  // 网关临时文件（接口日志 / SQL Profile）下载状态
  const [dataLogBusy, setDataLogBusy] = useState(false);
  const [dataLogStatus, setDataLogStatus] = useState('');
  // 台帐面板默认折叠：U_SyncRaw* 数据体量大、面板占屏高；首次进 U_SyncRawGdyy 之类的对象时不要一上来就铺满。
  const [syncPanelOpen, setSyncPanelOpen] = useState(false);
  const structIdRef = useRef(0);

  // 挂载时拉取可选服务器环境列表（含直连网关地址）
  useEffect(() => {
    loadEnvs();
  }, [loadEnvs]);

  // 若以 ?hcm-token 形式深链打开（如从其它窗口回跳），落地 token 到 store，避免重新要求填 token。
  useEffect(() => {
    const tk = new URLSearchParams(window.location.search).get('hcm-token');
    if (tk) setToken(tk.trim());
  }, [setToken]);

  // 直连模式：前端仍把加密参数交给同源后端 /api/hcm/direct，由后端直连 HCM 网关并解密返回明文。
  // 原因：浏览器对 HCM 网关的真实 POST 响应不带 CORS 头（网关仅在 OPTIONS 预检返回 CORS），
  // 纯浏览器直连会被 CORS 拦截；后端（服务端）发起请求不受 CORS 限制，因此「页面直连」=
  // 前端只负责加密/调同源端点，实际出口由后端直连网关完成。
  const directCall = useCallback(async (apiName: string, params: Record<string, any>, model = '') => {
    const tk = await ensureFreshToken();
    let res: Response;
    try {
      res = await fetch('/api/hcm/direct', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_name: apiName,
          params,
          model,
          token: usePresetToken ? '' : tk.trim(),
          target: targetUrl,
        }),
      });
    } catch (e) {
      // 本地后端不可达：点亮全局网络横幅（有响应的 4xx/5xx 走下方正常错误分支）
      reportLocalApiNetworkError('/api/hcm/direct', e);
      throw e;
    }
    const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok) {
      const detail =
        typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
      throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
    }
    return data?.data;
  }, [ensureFreshToken, usePresetToken, targetUrl]);

  // 直连原始响应：返回 {data, meta}，meta 含 srv_begin/srv_end/duration_ms/profile_index/log_index
  const directCallRaw = useCallback(
    async (
      apiName: string,
      params: Record<string, any>,
      model = '',
      opts: { sqlDebug?: boolean; profileDebug?: boolean } = {}
    ): Promise<{ data: any; meta: Record<string, any> }> => {
      const tk = await ensureFreshToken();
      let res: Response;
      try {
        res = await fetch('/api/hcm/direct', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            api_name: apiName,
            params,
            model,
            token: usePresetToken ? '' : tk.trim(),
            target: targetUrl,
            sql_debug: opts.sqlDebug,
            profile_debug: opts.profileDebug,
          }),
        });
      } catch (e) {
        reportLocalApiNetworkError('/api/hcm/direct', e);
        throw e;
      }
      const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      if (!res.ok) {
        const detail =
          typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
        throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
      }
      return { data: data?.data, meta: data?.meta || {} };
    },
    [ensureFreshToken, usePresetToken, targetUrl]
  );

  const loadPersonLookupModels = useCallback(async () => {
    if (personLookupModelsLoading) return;
    setPersonLookupModelsLoading(true);
    setPersonLookupError('');
    try {
      if (!usePresetToken && !token.trim()) {
        throw new HcmApiError(t('hcm.configRequired'), 401);
      }
      const response = await directCall('hcm.paas.object.list', {
        model: null,
        filter_str: 'U_SyncRaw',
        filter_dict: {},
        query_str: 'U_SyncRaw',
        page_index: 1,
        page_size: 100,
        extra_property: {
          sorts: [],
          filter_params: {
            filter_str: 'U_SyncRaw',
            page_index: 1,
            page_size: 100,
            advance_filter_dict: {},
            show_fields_key: ['class_', 'model_category', 'update_time'],
            base_object_str: 'hcm.paas.object',
            key: 'main.setting.hcm_model',
            query_str: 'U_SyncRaw',
          },
          only_list: false,
        },
        biz_type: 'list',
      });
      const candidates = parseHcmList(response).filter((item) =>
        `${item.id || ''} ${item.name || ''}`.toLowerCase().includes('syncraw')
      );
      const ledgers: HcmObjectItem[] = [];
      for (const candidate of candidates) {
        if (!candidate.id) continue;
        const metadata = await directCall(
          'hcm.model.meta',
          { model: candidate.id, meta_key: '' },
          candidate.id
        );
        if (isSyncLedgerModel(metadata?.fields || [])) ledgers.push(candidate as HcmObjectItem);
      }
      if (ledgers.length === 0) {
        throw new Error(t('hcm.personLookupNoModel'));
      }
      setPersonLookupModels(ledgers);
      setPersonLookupModelId((current) =>
        ledgers.some((model) => model.id === current) ? current : ledgers[0].id
      );
    } catch (e: any) {
      setPersonLookupError(e instanceof HcmApiError ? e.message : String(e?.message || e));
    } finally {
      setPersonLookupModelsLoading(false);
    }
  }, [directCall, personLookupModelsLoading, t, token, usePresetToken]);

  const searchPerson = useCallback(async () => {
    const name = personLookupName.trim();
    if (!name) {
      setPersonLookupError(t('hcm.personLookupNeedName'));
      return;
    }
    if (!personLookupModelId) {
      setPersonLookupError(t('hcm.personLookupNoModel'));
      return;
    }
    if (!usePresetToken && !token.trim()) {
      setPersonLookupError(t('hcm.configRequired'));
      return;
    }
    setPersonLookupLoading(true);
    setPersonLookupError('');
    setPersonLookupResults([]);
    setPersonLookupSearched(false);
    try {
      const response = await directCall('hcm.model.list', {
        model: personLookupModelId,
        filter_str: null,
        filter_dict: buildPersonLookupFilter(name),
        page_index: 1,
        page_size: 100,
        biz_type: 'list',
      }, personLookupModelId);
      setPersonLookupResults(parseHcmList(response));
      setPersonLookupSearched(true);
    } catch (e: any) {
      setPersonLookupError(e instanceof HcmApiError ? e.message : String(e?.message || e));
    } finally {
      setPersonLookupLoading(false);
    }
  }, [directCall, personLookupModelId, personLookupName, t, token, usePresetToken]);

  useEffect(() => {
    setPersonLookupModels([]);
    setPersonLookupModelId('');
    setPersonLookupResults([]);
    setPersonLookupSearched(false);
    setPersonLookupError('');
    if (personLookupOpen) void loadPersonLookupModels();
  }, [targetUrl]);

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

  // 拉取台帐 select 字段的候选值（实体类型 / 接口）：hcm.model.count + group_by + count_by。
  // 失败静默（下拉回退为可输入的文本框）。仅对同步台帐模型调用。
  const loadSyncOptions = useCallback(
    async (modelId: string) => {
      const wanted = ['u_entity_type', 'u_interface_code'];
      const entries = await Promise.all(
        wanted.map(async (f) => {
          try {
            const resp = await directCall(
              'hcm.model.count',
              { model: modelId, filter_dict: {}, group_by: [f], count_by: ['*'] },
              modelId
            );
            return [f, parseGroupCount(resp, f)] as const;
          } catch {
            return [f, [] as { value: string; count: number }[]] as const;
          }
        })
      );
      setSyncSelectOptions(Object.fromEntries(entries));
    },
    [directCall]
  );

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
      setQuickFilterField('');
      setSyncFilter({});
      setSyncSelectOptions({});
      setDataExportStatus('');
      setDataExportBusy('');
      setDataLogStatus('');
      setDataJsonQuery('');
      setDataMeta({});
      setDataStruct([]);
      setDataStructOpen(true);
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
        // 台帐模型：异步拉实体类型/接口候选值（不阻塞 meta 主流程）
        if (isSyncLedgerModel(m?.fields || [])) void loadSyncOptions(obj.id);
      } catch (e: any) {
        setMetaError(e instanceof HcmApiError ? e.message : String(e?.message || e));
      } finally {
        setMetaLoading(false);
      }
    },
    [directCall, metaKind, loadSyncOptions]
  );

  // 选中对象「数据」查询：调用 hcm.model.list 拉取该对象的记录（非元数据）。
  // 支持完整请求体 JSON 编辑、SQL/Profile 调试开关、响应元信息。
  const loadData = useCallback(
    async (obj: HcmObjectItem, pg: number = dataPage, payloadOverride?: Record<string, any>) => {
      if (!usePresetToken && !token.trim()) {
        setDataError(t('hcm.configRequired'));
        return;
      }
      let payload: Record<string, any>;
      if (payloadOverride) {
        // 调用方已合并好完整请求体（结构化筛选 / 文本搜索）直接传入：
        // setState 是异步的，若这里仍读 dataPayload 会拿到本渲染周期内的旧闭包值。
        payload = payloadOverride;
        setDataPayloadError('');
      } else {
        try {
          payload = dataPayload.trim() ? JSON.parse(dataPayload) : {};
          setDataPayloadError('');
        } catch (e: any) {
          setDataPayloadError(t('hcm.payloadParseFailed', { msg: e?.message || String(e) }));
          return;
        }
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

  // 按当前模型的元数据构造字段候选：优先展示显式允许筛选的字段；
  // 某些自定义模型未标记 is_filter 时，回退为该模型全部字段供 field-targeted filter_dict 使用。
  // 筛选字段候选：来自当前对象的元数据字段（hcm.model.meta.fields）——
  // 优先标记 is_filter 的字段，未标记时回退为该模型全部字段。
  // 供快速筛选下拉 + 结构化筛选联想共用。
  const filterCandidates = useMemo<HcmFilterCandidate[]>(
    () => metaFilterCandidates(meta?.fields || []),
    [meta]
  );
  const resolvedQuickFilterField = useMemo(() => {
    if (filterCandidates.some((c) => c.key === quickFilterField)) return quickFilterField;
    return pickDefaultFilterKey(filterCandidates);
  }, [filterCandidates, quickFilterField]);

  // 同步台帐（U_SyncRaw*）专用筛选：仅当模型具备 u_entity_type + u_source_name 时启用
  const syncFields = useMemo<SyncLedgerFilterField[]>(
    () => syncLedgerFieldsFor(meta?.fields || []),
    [meta]
  );
  const isSyncLedger = syncFields.length > 0;

  // 台帐筛选字段的展示名 / 占位符 / select 选项文案走 i18n。
  // 寻址规则（utils 层必须零运行时依赖，所以 key 在这里按字段键拼，字典里取不到就退回中文兜底）：
  //   展示名  → hcm.syncFields.<fieldKey>
  //   占位符  → hcm.syncPlaceholders.<fieldKey>
  //   静态选项 → labelKey（如 u_is_deleted 的 未删除/已删除）
  // t() 找不到键时会原样返回 key，用这一点判定「缺翻译」并回退。
  const syncFieldLabel = useCallback(
    (f: SyncLedgerFilterField): string => {
      const s = t(`hcm.syncFields.${f.key}`);
      return s === `hcm.syncFields.${f.key}` ? f.label : s;
    },
    [t]
  );
  const syncFieldPlaceholder = useCallback(
    (f: SyncLedgerFilterField): string => {
      const key = `hcm.syncPlaceholders.${f.key}`;
      const s = t(key);
      return s === key ? f.placeholder || '' : s;
    },
    [t]
  );
  const selectOptionLabel = useCallback(
    (opt: { label: string; labelKey?: string }): string => {
      if (!opt.labelKey) return opt.label;
      const s = t(opt.labelKey);
      return s === opt.labelKey ? opt.label : s;
    },
    [t]
  );

  // 结构化筛选（filter_dict）增删改
  const addStructRow = useCallback(() => {
    structIdRef.current += 1;
    setDataStruct((rows) => [
      ...rows,
      { id: structIdRef.current, field: '', operator: 'eq', value: '' },
    ]);
  }, []);
  const updateStructRow = useCallback(
    (id: number, patch: Partial<{ field: string; operator: HcmFilterOperator; value: string }>) => {
      setDataStruct((rows) => rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
    },
    []
  );
  const removeStructRow = useCallback((id: number) => {
    setDataStruct((rows) => rows.filter((r) => r.id !== id));
  }, []);
  const clearStruct = useCallback(() => setDataStruct([]), []);

  // 统一查询入口：把 UI 上可见的全部筛选（台帐字段 / 结构化条件 / 快捷关键字）
  // 组装成一个 filter_dict（多条件 AND）后**整体覆盖** payload.filter_dict 并查询。
  // 此前三个入口（查询数据 / 按条件查询 / 台帐查询）互相清空对方条件，
  // 是「筛选区混乱」的根源——现在只有一个查询按钮，改了什么就查什么。
  const applyDataQuery = useCallback(() => {
    if (!selected) return;
    if (dataStruct.some((row) => row.field.trim() && row.operator === 'contains' && !row.value.trim())) {
      setDataPayloadError(t('hcm.containsNeedValue'));
      return;
    }
    let payload: Record<string, any>;
    try {
      payload = dataPayload.trim() ? JSON.parse(dataPayload) : {};
      setDataPayloadError('');
    } catch (e: any) {
      setDataPayloadError(t('hcm.payloadParseFailed', { msg: e?.message || String(e) }));
      return;
    }
    const dict: Record<string, any> = {};
    if (isSyncLedger) Object.assign(dict, buildSyncLedgerFilterDict(syncFilter, syncFields));
    if (dataStruct.length) Object.assign(dict, buildHcmFilterDict(dataStruct));
    const kw = dataFilter.trim();
    if (kw && resolvedQuickFilterField) {
      Object.assign(dict, buildHcmFilterDict([
        { field: resolvedQuickFilterField, value: kw, operator: 'contains' },
      ]));
    }
    // structured filter owns filter_dict; generic search 只在「无可筛选字段的关键字」时兜底
    payload.filter_dict = dict;
    payload.filter_str = kw && !resolvedQuickFilterField ? kw : null;
    payload.query_str = null;
    payload.model = selected.id;
    if (!payload.page_size) payload.page_size = dataPageSize;
    if (!payload.biz_type) payload.biz_type = 'list';
    setDataPayload(JSON.stringify(payload, null, 2));
    setDataPage(1);
    loadData(selected, 1, payload);
  }, [
    selected, isSyncLedger, syncFilter, syncFields, dataStruct, dataFilter,
    resolvedQuickFilterField, dataPayload, dataPageSize, loadData, t,
  ]);

  // 切换到「数据」tab 时，若该对象尚未查询过则自动拉取一次
  const ensureData = useCallback(
    (obj: HcmObjectItem) => {
      if (!dataResult && !dataLoading) loadData(obj);
    },
    [dataResult, dataLoading, loadData]
  );

  // 「复制 / 导出当前筛选结果」采集：把命中的**全部行**（跨页拉全，非当前页）取回来。
  // 复制与导出共用这一步，保证两者拿到的是同一批数据。
  // 基准 payload 用最新一次 loadData 的 `dataPayload`（点过 Apply / Query Data / 按条件查询都会刷新它），
  // 所以不论是台帐筛选、结构化筛选、还是快速筛选——导出内容与「当前数据视图」完全一致。
  const collectCurrentResult = useCallback(
    async (
      onProgress: (text: string) => void
    ): Promise<{ rows: any[]; total: number; truncated: boolean; base: Record<string, any> } | null> => {
      if (!selected) return null;
      let base: Record<string, any>;
      try {
        base = dataPayload.trim() ? JSON.parse(dataPayload) : {};
        setDataPayloadError('');
      } catch {
        base = {}; // 高级请求体 JSON 写坏时不阻塞，退化为「只看当前 panel 勾选的筛选」
      }
      base.model = selected.id;
      if (!base.biz_type) base.biz_type = 'list';

      onProgress(t('hcm.countingRows'));
      // 1) 命中总数（count 很轻，作为状态文案提示）
      let matched = 0;
      try {
        const c = await directCall(
          'hcm.model.count',
          { model: selected.id, filter_dict: base.filter_dict || {}, count_by: ['*'] },
          selected.id
        );
        matched = parseTotalCount(c);
      } catch {
        matched = 0; // count 失败不致命：后面按实际拉取行数走
      }
      if (matched > 0) onProgress(t('hcm.fetchingRows', { n: matched }));

      // 2) 逐页拉全
      const { rows, total, truncated } = await fetchAllRows(
        async (pageIndex, pageSize) => {
          const { data } = await directCallRaw(
            'hcm.model.list',
            { ...base, page_index: pageIndex, page_size: pageSize },
            selected.id
          );
          return data;
        },
        {
          pageSize: SYNC_EXPORT_PAGE_SIZE,
          maxRows: SYNC_EXPORT_MAX_ROWS,
          // fetchAllRows 拿不到服务端总数时 total 传 0，此时只显示已拉取行数
          onProgress: (loaded, total) =>
            onProgress(
              total > 0
                ? t('hcm.fetchingProgress', { loaded, total })
                : t('hcm.fetchingProgress', { loaded, total: '—' })
            ),
        }
      );
      if (!rows.length) {
        onProgress(t('hcm.filterNoData'));
        return null;
      }
      return { rows, total, truncated, base };
    },
    [selected, dataPayload, directCall, directCallRaw, t]
  );

  // 把采集结果渲染成 CSV / JSON 文本。台帐模型默认剔除「原始报文」大列；
  // 其他模型没有这个列，「含原始报文」勾选就只是占位（不影响内容）。
  const renderCurrentResult = useCallback(
    (
      collected: { rows: any[]; total: number; base: Record<string, any> },
      opts: { withBom: boolean }
    ): string => {
      const fields = meta?.fields || [];
      const exclude = dataExportRaw ? [] : isSyncLedger ? ['u_raw_json'] : [];
      if (dataExportFormat === 'json') {
        return buildSyncLedgerJson({
          model: selected?.id || '',
          filter: collected.base.filter_dict || {},
          total: collected.total,
          rows: collected.rows,
          exclude,
        });
      }
      return buildSyncLedgerCsv(fields, collected.rows, { exclude, bom: opts.withBom });
    },
    [dataExportFormat, dataExportRaw, isSyncLedger, meta, selected]
  );

  // 导出当前筛选结果：拉全后落盘为 CSV / JSON（前端直接保存，无需后端写盘）
  const exportCurrentResult = useCallback(async () => {
    if (!selected || dataExportBusy) return;
    setDataExportBusy('export');
    try {
      const collected = await collectCurrentResult(setDataExportStatus);
      if (!collected) return;
      const text = renderCurrentResult(collected, { withBom: true });
      const ext = dataExportFormat === 'json' ? 'json' : 'csv';
      const mime =
        dataExportFormat === 'json' ? 'application/json;charset=utf-8' : 'text/csv;charset=utf-8';
      saveTextFile(text, exportFileName(selected.id, ext), mime);
      setDataExportStatus(
        t('hcm.exportedRows', { n: collected.rows.length }) +
          (collected.truncated
            ? t('hcm.exportTruncated', { n: SYNC_EXPORT_MAX_ROWS })
            : '')
      );
    } catch (e: any) {
      setDataExportStatus(t('hcm.exportFailed', { msg: e?.message || String(e) }));
    } finally {
      setDataExportBusy('');
    }
  }, [selected, dataExportBusy, dataExportFormat, collectCurrentResult, renderCurrentResult, t]);

  // 复制当前筛选结果：与导出同一批数据、同一格式，只是落到剪贴板而非文件。
  // 剪贴板是单串文本，超大数据会卡住渲染进程，故单独设一个更小的上限并明确告知。
  const copyCurrentResult = useCallback(async () => {
    if (!selected || dataExportBusy) return;
    setDataExportBusy('copy');
    try {
      const collected = await collectCurrentResult(setDataExportStatus);
      if (!collected) return;
      let rows = collected.rows;
      let note = '';
      if (rows.length > SYNC_COPY_MAX_ROWS) {
        rows = rows.slice(0, SYNC_COPY_MAX_ROWS);
        note = t('hcm.copyTruncated', {
          n: SYNC_COPY_MAX_ROWS,
          btn: t('hcm.exportResult'),
        });
      }
      const text = renderCurrentResult({ ...collected, rows }, { withBom: false });
      await writeClipboardText(text);
      setDataExportStatus(
        t('hcm.copiedRows', {
          n: rows.length,
          fmt: dataExportFormat.toUpperCase(),
          note,
        })
      );
    } catch (e: any) {
      setDataExportStatus(t('hcm.copyFailed', { msg: e?.message || String(e) }));
    } finally {
      setDataExportBusy('');
    }
  }, [selected, dataExportBusy, dataExportFormat, collectCurrentResult, renderCurrentResult, t]);

  // 下载网关临时文件（接口日志 / SQL Profile）。
  // 为什么不能直接 `<a href="/document/temp/download?...">`：
  //   ① 该路由挂在 HCM **网关** 上，本机 origin 没有它 → 404；
  //   ② 网关靠 cookie:token 认登录态，跨 origin 跳转不会带 cookie → 「请登录后操作」。
  // 因此统一走后端 /api/hcm/temp-download 代理（后端带 token 代取），前端拿 Blob 落盘。
  const downloadTempFile = useCallback(
    async (index: string, name: string) => {
      if (!index || dataLogBusy) return;
      setDataLogBusy(true);
      setDataLogStatus(t('hcm.downloading'));
      try {
        const tk = await ensureFreshToken();
        let res: Response;
        try {
          res = await fetch('/api/hcm/temp-download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              index,
              name,
              token: usePresetToken ? '' : tk.trim(),
              target: targetUrl,
            }),
          });
        } catch (e) {
          reportLocalApiNetworkError('/api/hcm/temp-download', e);
          throw e;
        }
        if (!res.ok) {
          const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
          const detail =
            typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
          throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
        }
        const blob = await res.blob();
        // 文件名：优先用传入的 name，其次按 index 拼（无扩展名时补 .log）
        const fname = name || (index.includes('.') ? index : `${index}.log`);
        saveBlob(blob, fname);
        setDataLogStatus(
          t('hcm.downloadedFile', {
            name: fname,
            kb: Math.max(1, Math.round(blob.size / 1024)),
          })
        );
      } catch (e: any) {
        setDataLogStatus(t('hcm.downloadFailed', { msg: e?.message || String(e) }));
      } finally {
        setDataLogBusy(false);
      }
    },
    [dataLogBusy, ensureFreshToken, targetUrl, usePresetToken, t]
  );

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

  // 双击对象：在新窗口打开「对象详情 + 元数据文件」合并窗口（同一模型只有一个窗口）。
  // 合并窗口由 HcmModelDetail 渲染，包含 字段 / list / info / view / JSON元数据 / 元数据文件 多个 tab。
  // 双击对象：在新窗口打开「对象详情 + 元数据文件」合并窗口（同一模型只有一个窗口）。
  // 合并窗口由 HcmModelDetail 渲染，包含 字段 / list / info / view / JSON元数据 / 元数据文件 多个 tab。
  const openDetailWindow = useCallback(
    (obj: HcmObjectItem) => {
      const url = `/web/?hcm-model=${encodeURIComponent(obj.id)}&hcm-detail=1`;
      // 通过 URL 携带 token + 网关 target：规避跨窗口 localStorage 不可靠（新窗口初始为 about:blank，
      // 直接写 w.localStorage 会抛 SecurityError 被吞掉 → 详情页读不到 token 报 configRequired）。
      // openHcmWindow 自动从 store 注入 hcm-token，这里只显式带上网关 target。
      openHcmWindow(url, `hcm-detail-${obj.id}`, 'width=1280,height=860,menubar=no,toolbar=no,location=no', {
        'hcm-target': targetUrl,
      });
    },
    [targetUrl]
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
      {/* 连接条：默认一行摘要（环境 + 刷新 + 设置开关 + 错误定位）。
          完整表单（代理地址/服务器/Token/显示/配置Token）折叠在内——
          环境与令牌是低频设置，常驻会和查询主操作抢注意力
         （曾经整条 7 个控件 + 2 个紫色主按钮杵在首屏） */}
      <div className="hcm-connect">
        <div className="hcm-connect-main">
          <span className="hcm-connect-env" title={envsError || t('hcm.serverEnvHint')}>
            <i className={`hcm-connect-dot${currentEnv ? ' on' : ''}`} aria-hidden="true" />
            <span className="hcm-connect-name">
              {currentEnv ? currentEnv.name : t('hcm.serverEnvSelect')}
            </span>
            {currentEnv && <span className="hcm-connect-url">{currentEnv.server_url}</span>}
            {usePresetToken && currentEnv?.has_preset_token && (
              <span className="hcm-connect-badge">{t('hcm.presetOn')}</span>
            )}
          </span>
          <button className="btn btn-sm" onClick={loadList} disabled={loading}>
            {loading ? t('hcm.loading') : t('hcm.refresh')}
          </button>
          <button
            className={`btn btn-sm ${personLookupOpen ? 'btn-active' : ''}`}
            onClick={() => {
              const opening = !personLookupOpen;
              setPersonLookupOpen(opening);
              if (opening && personLookupModels.length === 0) void loadPersonLookupModels();
            }}
            aria-expanded={personLookupOpen}
          >
            {t('hcm.personLookupTitle')}
          </button>
          <button
            className={`btn btn-sm hcm-connect-toggle${configOpen ? ' is-open' : ''}`}
            onClick={() => setConfigOpen((v) => !v)}
            aria-expanded={configOpen}
          >
            <span className="hcm-connect-chevron" aria-hidden="true" />
            {t('hcm.connectEnvToken')}
          </button>
          <span className="spacer" />
          {/* 云函数错误定位：常驻入口，避免只能靠拼 URL 打开 */}
          {/* <button
            className="btn btn-sm btn-ghost"
            onClick={() =>
              openHcmWindow('/web/?hcm-cf-err=1', '_blank', 'width=1100,height=900', {
                'hcm-target': targetUrl,
              })
            }
            title={t('hcm.cfErrLauncherHint')}
          >
            {t('hcm.cfErrLauncher')}
          </button> */}
        </div>

        {personLookupOpen && (
          <section className="hcm-person-lookup" aria-label={t('hcm.personLookupTitle')}>
            <div className="hcm-person-lookup-heading">
              <strong>{t('hcm.personLookupTitle')}</strong>
              <span>{t('hcm.personLookupHint')}</span>
            </div>
            <div className="hcm-person-lookup-form">
              {personLookupModels.length > 1 && (
                <select
                  className="sel"
                  value={personLookupModelId}
                  onChange={(e) => {
                    setPersonLookupModelId(e.target.value);
                    setPersonLookupResults([]);
                    setPersonLookupSearched(false);
                  }}
                  aria-label={t('hcm.personLookupModel')}
                >
                  {personLookupModels.map((model) => (
                    <option key={model.id} value={model.id}>{model.name || model.id}</option>
                  ))}
                </select>
              )}
              <input
                className="hcm-search"
                value={personLookupName}
                onChange={(e) => {
                  setPersonLookupName(e.target.value);
                  setPersonLookupSearched(false);
                  setPersonLookupResults([]);
                }}
                onKeyDown={(e) => { if (e.key === 'Enter') void searchPerson(); }}
                placeholder={t('hcm.personLookupPlaceholder')}
                spellCheck={false}
                aria-label={t('hcm.personLookupPlaceholder')}
              />
              <button
                className="btn btn-sm btn-primary"
                onClick={() => void searchPerson()}
                disabled={personLookupLoading || personLookupModelsLoading || !personLookupModelId}
              >
                {personLookupModelsLoading
                  ? t('hcm.personLookupLoadingModels')
                  : personLookupLoading ? t('hcm.loading') : t('hcm.personLookupSearch')}
              </button>
            </div>
            {personLookupError && <div className="hcm-error" role="alert">{personLookupError}</div>}
            {personLookupSearched && !personLookupLoading && !personLookupError && (
              <div className="hcm-person-lookup-summary">
                {t('hcm.personLookupCount', { n: personLookupResults.length })}
              </div>
            )}
            {personLookupSearched && !personLookupLoading && !personLookupError && !personHasAppointment && (
              <div className="hcm-person-lookup-summary" role="status">
                {personLookupResults.length > 0
                  ? t('hcm.personLookupNoAppointment')
                  : t('hcm.personLookupNoResults')}
              </div>
            )}
            {personLookupResults.length > 0 && (
              <div className="hcm-person-lookup-results">
                {personLookupResults.map((row, index) => {
                  const raw = row.u_raw_json;
                  let detail: string;
                  if (typeof raw === 'string') {
                    try { detail = JSON.stringify(JSON.parse(raw), null, 2); }
                    catch { detail = raw; }
                  } else {
                    detail = JSON.stringify(raw ?? row, null, 2);
                  }
                  return (
                    <details className="hcm-person-result" key={`${row.id ?? row.u_source_code ?? index}-${index}`}>
                      <summary>
                        <span>{entityTypeLabel(String(row.u_entity_type || '')) || t('hcm.personLookupRecord')}</span>
                        <span>{String(row.u_source_name || '')}</span>
                        {row.u_source_code && <code>{String(row.u_source_code)}</code>}
                      </summary>
                      <pre>{detail}</pre>
                    </details>
                  );
                })}
              </div>
            )}
          </section>
        )}

        {configOpen && (
          <div className="hcm-config hcm-connect-body">
            <label className="hcm-config-readonly">
              <span>{t('hcm.baseUrl')}</span>
              <input
                value={baseUrl}
                readOnly
                title={t('hcm.directHint')}
                spellCheck={false}
              />
            </label>
            <label>
              <span>{t('hcm.serverEnv')}</span>
              <select
                className="sel"
                value={selectedEnv}
                onChange={(e) => setSelectedEnv(e.target.value)}
                title={envsError || t('hcm.serverEnvHint')}
              >
                <option value="">{t('hcm.serverEnvSelect')}</option>
                {envs.map((ev) => (
                  <option key={ev.key} value={ev.key}>
                    {ev.name}（{ev.server_url}{ev.domain ? ` / ${ev.domain}` : ''}）{ev.has_preset_token ? '●' : ''}
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
            {/* 开关态用 btn-active（浅底主色字）而不是实心紫：toggle 不是主操作 */}
            <button
              className={`btn btn-sm ${usePresetToken ? 'btn-active' : ''}`}
              onClick={() => setUsePresetToken((v) => !v)}
              title={t('hcm.usePresetHint')}
              disabled={!currentEnv?.has_preset_token}
            >
              {usePresetToken ? t('hcm.presetOn') : t('hcm.presetOff')}
            </button>
          </div>
        )}
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
              className={advOpen ? 'btn btn-sm btn-active' : 'btn btn-sm btn-ghost'}
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
                <button className="btn btn-sm" onClick={() => setAdvOpen(false)}>
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
                    role="button"
                    tabIndex={0}
                    aria-label={it.id}
                    onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openDetailWindow(it); } }}
                  >
                    <td className="hcm-cell-ellipsis" title={it.name}>{it.name}</td>
                    <td className="hcm-cell-ellipsis" title={it.description || ''}>{it.description || '—'}</td>
                    <td className="hcm-mono hcm-cell-ellipsis" title={it.class_}>{it.class_}</td>
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
            <button
              className="btn btn-sm hcm-pager-btn"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
              aria-label={t('hcm.prevPage')}
              title={t('hcm.prevPage')}
            >
              ‹
            </button>
            <span className="hcm-pager-page">
              {page} / {totalPages}
            </span>
            <button
              className="btn btn-sm hcm-pager-btn"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
              aria-label={t('hcm.nextPage')}
              title={t('hcm.nextPage')}
            >
              ›
            </button>
          </div>
        </div>

        {/* 右：字段 / JSON 元数据 */}
        <div className="hcm-detail">
          {!selected && (
            <EmptyState className="hcm-empty-state hcm-detail-empty" title={t('hcm.pickHint')} hint={t('hcm.dblToViewNew')} />
          )}
          {selected && (
            <>
              {/* 头部一行搞定：名称 + id 小徽标（此前 id 单独占一行，纯浪费纵向空间） */}
              <div className="hcm-detail-head">
                <div className="hcm-detail-title">{selected.name}</div>
                <code className="hcm-detail-id" title={selected.id}>{selected.id}</code>
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
                  title={t('hcm.tabDataHint')}
                >
                  {t('hcm.tabData')}
                </button>
                {/* 元数据显示维度：list / info / view / all（查询所有 JSON 节点）。
                    四个切维按钮收进一个分段控件（.hcm-kind-group），只保留一个边框，
                    不再和左侧视图 tab 一样一人一颗按钮，减少一排 8 个钮的噪声 */}
                <span className="hcm-kind-group">
                  <button
                    className={metaKind === 'list' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                    onClick={() => selected && loadMeta(selected, 'list')}
                    disabled={!selected}
                    title={t('hcm.kindListHint')}
                  >
                    list
                  </button>
                  <button
                    className={metaKind === 'info' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                    onClick={() => selected && loadMeta(selected, 'info')}
                    disabled={!selected}
                    title={t('hcm.kindInfoHint')}
                  >
                    info
                  </button>
                  <button
                    className={metaKind === 'view' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                    onClick={() => selected && loadMeta(selected, 'view')}
                    disabled={!selected}
                    title={t('hcm.kindViewHint')}
                  >
                    view
                  </button>
                  <button
                    className={metaKind === 'all' ? 'btn btn-sm btn-active' : 'btn btn-sm'}
                    onClick={() => selected && loadMeta(selected, 'all')}
                    disabled={!selected}
                    title={t('hcm.kindAllHint')}
                  >
                    all
                  </button>
                </span>
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

              {/* 选中对象「数据」查询：拉取记录并以 JSON 展示 + 复制 / 导出 */}
              {rightTab === 'data' && (
                <div className="hcm-data">
                  {/* 顶部工具条：左组「快速筛选（字段+值+查询）」，右组「复制/导出」，
                      两组各自成簇、右组贴右，避免 8 个控件平铺在一行里互相打架。
                      导出仍对当前筛选条件命中的**全部行**生效。 */}
                  <div className="hcm-data-export">
                    <div className="hcm-export-group">
                    <select
                      className="hcm-struct-op"
                      value={resolvedQuickFilterField}
                      onChange={(e) => setQuickFilterField(e.target.value)}
                      aria-label={t('hcm.quickFilterField')}
                      disabled={filterCandidates.length === 0}
                      title={t('hcm.quickFilterField')}
                    >
                      {filterCandidates.map((c) => (
                        <option key={c.key} value={c.key}>
                          {c.label ? `${c.label} (${c.key})` : c.key}
                        </option>
                      ))}
                    </select>
                    <input
                      className="hcm-search hcm-quick-filter"
                      value={dataFilter}
                      onChange={(e) => setDataFilter(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && selected) {
                          setDataPage(1);
                          applyDataQuery();
                        }
                      }}
                      placeholder={t('hcm.dataFilterPlaceholder')}
                      spellCheck={false}
                    />
                    <button
                      className="btn btn-sm btn-primary hcm-query-btn"
                      onClick={applyDataQuery}
                      disabled={!selected || dataLoading}
                      title={t('hcm.queryAllHint')}
                    >
                      {dataLoading ? t('hcm.loading') : t('hcm.queryData')}
                    </button>
                    </div>
                  </div>

                  {/* 筛选开关条：三个可折叠筛选区块（台帐 / 结构化 / 高级）+ 数据 JSON 搜索
                      全部压到同一行；展开体依次渲染在下方，收起时完全不占高度，
                      把纵向空间尽量留给数据内容（此前三个区块各自占一整行头部）。 */}
                  <div className="hcm-filterbar">
                    {isSyncLedger && (
                      <button
                        className={`hcm-chip${syncPanelOpen ? ' active' : ''}`}
                        onClick={() => setSyncPanelOpen((v) => !v)}
                        aria-expanded={syncPanelOpen}
                        title={syncPanelOpen ? t('hcm.syncCollapse') : t('hcm.syncExpand')}
                      >
                        {syncPanelOpen ? '▾' : '▸'} {t('hcm.syncFilter')}
                        {Object.keys(syncFilter).length > 0 ? ` · ${Object.keys(syncFilter).length}` : ''}
                      </button>
                    )}
                    <button
                      className={`hcm-chip${dataStructOpen ? ' active' : ''}`}
                      onClick={() => setDataStructOpen((v) => !v)}
                      disabled={!selected}
                      aria-expanded={dataStructOpen}
                      title={t('hcm.structFilterHint')}
                    >
                      {dataStructOpen ? '▾' : '▸'} {t('hcm.structFilter')}
                      {dataStruct.length > 0 ? ` · ${dataStruct.length}` : ''}
                    </button>
                    <button
                      className={`hcm-chip${dataAdvOpen ? ' active' : ''}`}
                      onClick={() => setDataAdvOpen((v) => !v)}
                      disabled={!selected}
                      aria-expanded={dataAdvOpen}
                      title={t('hcm.requestBodyHint')}
                    >
                      {dataAdvOpen ? '▾' : '▸'} {t('hcm.advanced')}
                    </button>
                  </div>

                  {/* 台帐筛选体（仅同步台帐模型）：字段网格 + 清空；查询统一走顶部按钮 */}
                  {isSyncLedger && syncPanelOpen && (
                    <div className="hcm-panel-body">
                      {/* 字段语义说明挂在容器 tooltip 上，不再占一行可视空间 */}
                      <div className="hcm-sync-grid" title={t('hcm.syncHint')}>
                        {syncFields.map((f) => {
                          const dyn = syncSelectOptions[f.key] || [];
                          const opts: { value: string; label: string; count?: number }[] =
                            f.kind !== 'select'
                              ? []
                              : dyn.length
                                ? dyn.map((o) => ({
                                    value: o.value,
                                    label: f.key === 'u_entity_type' ? entityTypeLabel(o.value) : o.value,
                                    count: o.count,
                                  }))
                                : staticSyncSelectOptions(f.key).map((o) => ({
                                    value: o.value,
                                    label: selectOptionLabel(o),
                                  }));
                          const value = syncFilter[f.key] || '';
                          const setVal = (v: string) => setSyncFilter((s) => ({ ...s, [f.key]: v }));
                          return (
                            <label className="hcm-sync-item" key={f.key}>
                              <span className="hcm-sync-label">{syncFieldLabel(f)}</span>
                              {f.kind === 'select' && opts.length > 0 ? (
                                <select className="sel" value={value} onChange={(e) => setVal(e.target.value)}>
                                  <option value="">{t('hcm.metaAll')}</option>
                                  {opts.map((o) => (
                                    <option key={o.value} value={o.value}>
                                      {o.label}
                                      {typeof o.count === 'number' ? ` · ${o.count}` : ''}
                                    </option>
                                  ))}
                                </select>
                              ) : (
                                <input
                                  value={value}
                                  onChange={(e) => setVal(e.target.value)}
                                  placeholder={syncFieldPlaceholder(f)}
                                  spellCheck={false}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') applyDataQuery();
                                  }}
                                />
                              )}
                            </label>
                          );
                        })}
                      </div>
                      <div className="hcm-panel-actions">
                        <button
                          className="btn btn-sm"
                          onClick={() => setSyncFilter({})}
                          disabled={Object.keys(syncFilter).length === 0}
                        >
                          {t('hcm.structClear')}
                        </button>
                      </div>
                    </div>
                  )}

                  {/* 结构化筛选体（filter_dict）：参考对象字段，多条件 AND 组合。
                      提示放在筛选 chip 的 tooltip 里；查询统一走顶部「查询数据」。 */}
                  {dataStructOpen && (
                    <div className="hcm-panel-body">
                      {dataStruct.length === 0 && (
                        <div className="hcm-struct-empty">{t('hcm.structEmpty')}</div>
                      )}
                      {dataStruct.map((r) => (
                        <div className="hcm-struct-row" key={r.id}>
                          <StructFieldInput
                            value={r.field}
                            onChange={(field) => updateStructRow(r.id, { field })}
                            options={filterCandidates}
                            placeholder={t('hcm.structFieldPlaceholder')}
                          />
                          <select
                            className="hcm-struct-op"
                            value={r.operator}
                            onChange={(e) => updateStructRow(r.id, { operator: e.target.value as HcmFilterOperator })}
                            aria-label={t('hcm.structOperator')}
                          >
                            <option value="eq">{t('hcm.structExact')}</option>
                            <option value="contains">{t('hcm.structContains')}</option>
                          </select>
                          <input
                            className="hcm-struct-value"
                            value={r.value}
                            onChange={(e) => updateStructRow(r.id, { value: e.target.value })}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') applyDataQuery();
                            }}
                            placeholder={t('hcm.structValuePlaceholder')}
                            spellCheck={false}
                          />
                          <button
                            className="btn btn-sm hcm-struct-del"
                            onClick={() => removeStructRow(r.id)}
                            title={t('hcm.structRemove')}
                            aria-label={t('hcm.structRemove')}
                          >
                            ×
                          </button>
                        </div>
                      ))}
                      <div className="hcm-panel-actions">
                        <button className="btn btn-sm" onClick={addStructRow} disabled={!selected}>
                          {t('hcm.structAdd')}
                        </button>
                        <button className="btn btn-sm" onClick={clearStruct} disabled={!dataStruct.length}>
                          {t('hcm.structClear')}
                        </button>
                      </div>
                    </div>
                  )}
                  {/* 字段联想已改为 StructFieldInput 自绘弹层（datalist 弹出列表无法被 CSS 定制） */}

                  {/* 高级请求体体：直接编辑 hcm.model.list 请求 JSON + 调试开关 */}
                  {dataAdvOpen && (
                    <div className="hcm-panel-body">
                      <div className="hcm-panel-hint" title={t('hcm.requestBodyHint')}>{t('hcm.requestBodyHint')}</div>
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

                  {dataLoading && <div className="hcm-loading">{t('hcm.loading')}…</div>}
                  {dataError && <div className="hcm-error">{dataError}</div>}

                  {!dataLoading && !dataError && dataResult && (
                    <>
                      {/* 视图内查找：只作用于下面这段已加载的 JSON（高亮 + 命中数）。
                          与上方「字段 + 关键字 + 查询」的服务端查询是两回事，
                          贴着内容放，避免两个搜索框在相邻两行里分不清 */}
                      <div className="hcm-json-find">
                        <input
                          className="hcm-search hcm-json-search"
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
                      {/* 数据正文：flex:1 + 滚动，占满工具条/筛选条/页脚之外的全部高度 */}
                      <pre className="hcm-json">
                        {dataView.lines.map((ln, i) => (
                          <Fragment key={i}>
                            {hcmHighlightLine(ln, dataJsonQuery)}
                            {'\n'}
                          </Fragment>
                        ))}
                      </pre>
                      {/* SQL 日志（勾选「输出 SQL 日志」后网关在 meta 带回 sql_trace） */}
                      {Array.isArray(dataMeta.sql_trace) && dataMeta.sql_trace.length > 0 && (
                        <details className="hcm-api-sqltrace">
                          <summary>
                            {t('hcm.apiSqlTrace', { n: dataMeta.sql_trace.length })}
                          </summary>
                          <div className="hcm-api-sqltrace-body">
                            {dataMeta.sql_trace.map((s: string, i: number) => {
                              const m = /^\[(\d+(?:\.\d+)?)\]\s*([\s\S]*)$/.exec(s);
                              return (
                                <div key={i} className="hcm-api-sqltrace-item">
                                  {m ? (
                                    <>
                                      <span className="hcm-api-sqltrace-ms" title={t('hcm.apiSqlTraceMs')}>
                                        [{m[1]}ms]
                                      </span>
                                      <span className="hcm-api-sqltrace-sql">{m[2]}</span>
                                    </>
                                  ) : (
                                    <span className="hcm-api-sqltrace-sql">{s}</span>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </details>
                      )}
                      {/* 页脚：左侧状态（条数/耗时/接口日志下载），右侧翻页 —— 省掉单独一行元信息 */}
                      <div className="hcm-pager hcm-pager--split">
                        <span className="hcm-data-meta hcm-mono">
                          {/* 计数包一层 span：flex 容器里的裸文本节点是匿名 item，
                              窄面板会被压成 min-content 逐字换行（Records: / 5498 / · …） */}
                          <span className="hcm-meta-count">
                            {t('hcm.dataCount')}: {dataTotal}
                          </span>
                          {typeof dataMeta.duration_ms === 'number' && (
                            <span className="hcm-meta-duration">
                              {' '}
                              · {t('hcm.duration')}: {(dataMeta.duration_ms / 1000).toFixed(3)}s
                            </span>
                          )}
                          {dataMeta.profile_index && (
                            <button
                              type="button"
                              className="hcm-meta-link hcm-link-btn"
                              disabled={dataLogBusy}
                              onClick={() => downloadTempFile(String(dataMeta.profile_index), '')}
                            >
                              {t('hcm.downloadProfile')}
                            </button>
                          )}
                          {dataMeta.log_index && (
                            <button
                              type="button"
                              className="hcm-meta-link hcm-link-btn"
                              disabled={dataLogBusy}
                              onClick={() => downloadTempFile(String(dataMeta.log_index), '')}
                            >
                              {t('hcm.downloadLog')}
                            </button>
                          )}
                          {dataLogStatus && <span className="hcm-meta-duration"> · {dataLogStatus}</span>}
                        </span>
                        {/* 导出操作组：与「记录数/耗时」同处页脚左簇——导出作用于当前筛选命中的
                            全部行，跟计数放在一起语义连贯；原先挤在筛选 chip 行导致中部拥挤。 */}
                        <div className="hcm-export-group hcm-export-actions">
                          {dataExportStatus && (
                            <span className="hcm-data-export-status">{dataExportStatus}</span>
                          )}
                          <select
                            className="sel"
                            value={dataExportFormat}
                            onChange={(e) => setDataExportFormat(e.target.value === 'json' ? 'json' : 'csv')}
                            title={t('hcm.exportFormat')}
                            disabled={dataExporting}
                            aria-label={t('hcm.exportFormat')}
                          >
                            <option value="csv">CSV</option>
                            <option value="json">JSON</option>
                          </select>
                          {isSyncLedger && (
                            <label className="hcm-sync-raw" title={t('hcm.exportRawHint')}>
                              <input
                                type="checkbox"
                                checked={dataExportRaw}
                                onChange={(e) => setDataExportRaw(e.target.checked)}
                                disabled={dataExporting}
                              />
                              {t('hcm.exportRaw')}
                            </label>
                          )}
                          <button
                            className="btn btn-sm"
                            onClick={copyCurrentResult}
                            disabled={!selected || dataExporting}
                            title={t('hcm.copyResultHint')}
                          >
                            {dataExportBusy === 'copy' ? t('hcm.copying') : t('hcm.copyResult')}
                          </button>
                          <button
                            className="btn btn-sm"
                            onClick={exportCurrentResult}
                            disabled={!selected || dataExporting}
                            title={t('hcm.exportResultHint')}
                          >
                            {dataExportBusy === 'export' ? t('hcm.exporting') : t('hcm.exportResult')}
                          </button>
                        </div>
                        <div className="hcm-pager-nav">
                          <button
                            className="btn btn-sm hcm-pager-btn"
                            aria-label={t('hcm.prevPage')}
                            title={t('hcm.prevPage')}
                            disabled={dataPage <= 1}
                            onClick={() => {
                              const p = Math.max(1, dataPage - 1);
                              setDataPage(p);
                              if (selected) loadData(selected, p);
                            }}
                          >
                            ‹
                          </button>
                          <span className="hcm-pager-page">
                            {dataPage} / {Math.max(1, Math.ceil(dataTotal / dataPageSize))}
                          </span>
                          <button
                            className="btn btn-sm hcm-pager-btn"
                            aria-label={t('hcm.nextPage')}
                            title={t('hcm.nextPage')}
                            disabled={dataPage >= Math.ceil(dataTotal / dataPageSize)}
                            onClick={() => {
                              const p = dataPage + 1;
                              setDataPage(p);
                              if (selected) loadData(selected, p);
                            }}
                          >
                            ›
                          </button>
                          <span className="hcm-pager-sep" />
                          <label className="hcm-data-pagesize">
                            <span>{t('hcm.dataPageSize')}</span>
                            <input
                              className="input input-sm"
                              type="number"
                              min={1}
                              max={1000}
                              value={Number.isFinite(dataPageSize) ? dataPageSize : ''}
                              onChange={(e) => {
                                // 不用 `parseInt(v) || 20` 静默回退：让非法值可见，
                                // 失焦时统一钳制并提示（max 也不再形同虚设）
                                const raw = e.target.value;
                                setDataPageSize(raw === '' ? NaN : Number(raw));
                              }}
                              onBlur={() => {
                                const n = Math.trunc(Number(dataPageSize));
                                const v = !Number.isFinite(n) || n < 1 ? 20 : Math.min(1000, n);
                                if (v !== dataPageSize) {
                                  setDataPageSize(v);
                                  addToast(t('hcm.dataPageSizeClamped', { v }), 'warn');
                                }
                                if (selected) {
                                  setDataPage(1);
                                  loadData(selected, 1);
                                }
                              }}
                            />
                          </label>
                        </div>
                      </div>
                    </>
                  )}

                  {!dataLoading && !dataError && !dataResult && (
                    <EmptyState className="hcm-empty-state" title={t('hcm.pickHint')} hint={t('hcm.dblToViewNew')} />
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
