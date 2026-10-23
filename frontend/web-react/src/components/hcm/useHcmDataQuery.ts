/**
 * HCM 数据查询共享 hook（主面板 / 表格全屏 / 独立窗口共用）。
 *
 * 为什么抽出来
 * ------------
 * 独立窗口原先只能吃主窗口传过来的一份数据快照 —— 一旦快照因 sessionStorage
 * 配额、或用户在窗口里想换模型/翻页/重新查询就直接没数据（实测打开就是 0 行）。
 * 这里把「调 /api/hcm/direct + 分页 + 搜索过滤」收敛成一份实现，窗口自己也能查。
 *
 * 语义约定
 * --------
 * - **分页**：page_index 从 1 开始；page_size 由调用方控制。服务端返回
 *   ``{list, count}``，我们同时维护 ``list``（当前页）与 ``total``（全量条数）。
 * - **搜索**：默认在**服务端**用 filter_dict 做字段精确过滤（HCM 的能力），
 *   同时提供**客户端**全字段模糊过滤给表格用 —— 后者不需要重打接口，
 *   适合「先拿一页再就地筛」的场景。两者不混用，调用方按需选。
 * - **模型列表**：来自 ``hcm.paas.object.list``；失败时退化为只用当前模型，
 *   不让下拉框直接空掉。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { HcmApiError } from '../../api/hcm/client';

export type HcmRow = Record<string, unknown>;

export type DataQueryState = {
  rows: HcmRow[];
  total: number;
  loading: boolean;
  error: string;
  /** 实际页号（1 基）与页大小 */
  page: number;
  pageSize: number;
  /** 最近一次查询的元信息（耗时等） */
  meta: Record<string, unknown>;
};

const EMPTY: DataQueryState = {
  rows: [], total: 0, loading: false, error: '', page: 1, pageSize: 20, meta: {},
};

/** 把平台返回的各种形态统一成 {list, total}。 */
export function normalizeList(data: unknown): { list: HcmRow[]; total: number } {
  if (Array.isArray(data)) {
    const list = data.filter((x) => x && typeof x === 'object') as HcmRow[];
    return { list, total: list.length };
  }
  if (data && typeof data === 'object') {
    const d = data as Record<string, unknown>;
    if (Array.isArray(d.list)) {
      const list = d.list.filter((x) => x && typeof x === 'object') as HcmRow[];
      return { list, total: Number(d.count ?? d.total ?? list.length) || list.length };
    }
    if (Array.isArray(d.data)) {
      return normalizeList(d.data);
    }
    const inner = d.data as Record<string, unknown> | undefined;
    if (inner && Array.isArray(inner.list)) {
      return normalizeList(inner);
    }
  }
  return { list: [], total: 0 };
}

export type UseHcmDataQueryOptions = {
  targetUrl: string;
  /** 常驻 token（'' 表示走后端预设 token） */
  token: string;
  usePresetToken: boolean;
};

/** 直接打 /api/hcm/direct，返回 {data, meta}。 */
export async function callHcmDirect(
  apiName: string,
  params: Record<string, unknown>,
  model: string,
  opts: { target: string; token: string; usePreset: boolean; sqlDebug?: boolean; profileDebug?: boolean },
): Promise<{ data: unknown; meta: Record<string, unknown> }> {
  let res: Response;
  try {
    res = await fetch('/api/hcm/direct', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        api_name: apiName,
        params,
        model,
        token: opts.usePreset ? '' : (opts.token || '').trim(),
        target: opts.target,
        sql_debug: !!opts.sqlDebug,
        profile_debug: !!opts.profileDebug,
      }),
    });
  } catch (e) {
    throw e instanceof Error ? e : new Error(String(e));
  }
  const payload = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
  if (!res.ok) {
    const detail =
      typeof payload?.detail === 'string' ? payload.detail : JSON.stringify(payload?.detail ?? payload);
    throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
  }
  // 后端约定：{ok, data, meta, error}；业务失败时 ok:false + error
  if (payload && payload.ok === false) {
    const err = payload.error;
    const msg =
      (err && typeof err === 'object' && (err.errmsg || err.detail)) ||
      (typeof err === 'string' ? err : '') ||
      `请求失败 (HTTP ${res.status})`;
    const e = new HcmApiError(String(msg), res.status);
    (e as HcmApiError & { raw?: unknown }).raw = err;
    throw e;
  }
  return { data: payload?.data, meta: payload?.meta || {} };
}

/** 查询某个模型的一页数据。 */
export function useHcmDataQuery({ targetUrl, token, usePresetToken }: UseHcmDataQueryOptions) {
  const [state, setState] = useState<DataQueryState>(EMPTY);
  const [model, setModel] = useState('');
  const [pageSize, setPageSize] = useState(50);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const load = useCallback(
    async (
      modelId: string,
      page = 1,
      opts: { pageSize?: number; filterDict?: Record<string, unknown> } = {},
    ) => {
      const id = (modelId || '').trim();
      if (!id) return;
      const ps = Math.max(1, Math.min(opts.pageSize ?? pageSize, 1000));
      setModel(id);
      setState((s) => ({ ...s, loading: true, error: '' }));
      try {
        const { data, meta } = await callHcmDirect(
          'hcm.model.list',
          {
            model: id,
            page_index: Math.max(1, page),
            page_size: ps,
            biz_type: 'list',
            ...(opts.filterDict && Object.keys(opts.filterDict).length
              ? { filter_dict: opts.filterDict }
              : {}),
          },
          id,
          { target: targetUrl, token, usePreset: usePresetToken },
        );
        if (!alive.current) return;
        const { list, total } = normalizeList(data);
        setState({
          rows: list,
          total,
          loading: false,
          error: '',
          page: Math.max(1, page),
          pageSize: ps,
          meta: meta || {},
        });
      } catch (e) {
        if (!alive.current) return;
        const msg = e instanceof HcmApiError ? e.message : String((e as Error)?.message || e);
        setState((s) => ({ ...s, loading: false, error: msg, rows: [], total: 0 }));
      }
    },
    [targetUrl, token, usePresetToken, pageSize],
  );

  /** 切页：保留当前模型与页大小。 */
  const goPage = useCallback(
    (page: number) => {
      if (!model) return;
      void load(model, page, { pageSize });
    },
    [model, pageSize, load],
  );

  const changePageSize = useCallback(
    (ps: number) => {
      const v = Math.max(1, Math.min(Math.trunc(ps) || 50, 1000));
      setPageSize(v);
      if (model) void load(model, 1, { pageSize: v });
    },
    [model, load],
  );

  const reload = useCallback(() => {
    if (model) void load(model, state.page, { pageSize });
  }, [model, state.page, pageSize, load]);

  return { state, model, pageSize, load, goPage, changePageSize, reload };
}

/** 模型下拉列表：优先 paas.object.list，失败则退化为当前模型一项。 */
export function useHcmModels(
  opts: UseHcmDataQueryOptions & { fallbackModel?: string },
) {
  const { targetUrl, token, usePresetToken, fallbackModel } = opts;
  const [models, setModels] = useState<{ id: string; description?: string }[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!targetUrl) return;
    setLoading(true);
    try {
      const { data } = await callHcmDirect(
        'hcm.paas.object.list',
        { page_index: 1, page_size: 2000 },
        '',
        { target: targetUrl, token, usePreset: usePresetToken },
      );
      const { list } = normalizeList(data);
      const items = list
        .map((r) => ({
          id: String((r as Record<string, unknown>).id ?? (r as Record<string, unknown>).name ?? ''),
          description: String((r as Record<string, unknown>).description ?? ''),
        }))
        .filter((m) => m.id);
      setModels(items);
    } catch {
      // 模型列表拿不到不影响查数据：至少保留当前模型可选
      setModels(fallbackModel ? [{ id: fallbackModel }] : []);
    } finally {
      setLoading(false);
    }
  }, [targetUrl, token, usePresetToken, fallbackModel]);

  useEffect(() => { void load(); }, [load]);

  return { models, loading, reload: load };
}
