import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useT } from '../../i18n';
import { HcmApiError } from '../../api/hcm/client';

const LS_TOKEN = 'hcm.token';
const DIRECT_ENDPOINT = '/api/hcm/direct';
// 元数据文件列表一次性拉取上限；超限时给出提示，可缩小 biz_type 范围。
const LIST_PAGE_SIZE = 500;
// HCM 网关对空/缺失 biz_type 只返回 1 条默认布局（无法一次拉全），
// “全部”改为按各已知 biz_type 分别拉取后合并去重。
const KNOWN_BIZ_TYPES = ['list', 'info', 'view', 'base', 'panel', 'dataset'];

// ---------- 复用 HcmModelDetail 的 JSON 高亮搜索能力 ----------
function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
// 文件名字段高亮搜索：命中子串包裹 <mark>，与 JSON 内容搜索高亮一致（先转义防注入）。
function highlightText(text: string, q: string): string {
  const kw = q.trim();
  const safeText = escapeHtml(text);
  if (!kw) return safeText;
  const safeKw = escapeRegExp(escapeHtml(kw));
  const re = new RegExp(`(${safeKw})`, 'gi');
  return safeText.replace(re, '<mark>$1</mark>');
}
function makeJsonView(data: any, q: string) {
  const raw = JSON.stringify(data, null, 2);
  const kw = q.trim();
  if (!kw) return { html: escapeHtml(raw), text: raw, matched: 0 };
  const lower = kw.toLowerCase();
  const lines = raw.split('\n');
  let matched = 0;
  const html = lines
    .map((ln) => {
      const has = ln.toLowerCase().includes(lower);
      const escaped = escapeHtml(ln);
      const re = new RegExp(`(${escapeRegExp(kw)})`, 'gi');
      if (!has) return escaped;
      matched += 1;
      return escaped.replace(re, (m) => `<mark data-mid="${matched}">${m}</mark>`);
    })
    .join('\n');
  return { html, text: raw, matched };
}

// 从 meta_key 解析 biz_type：Employee.meta.list.xxx.json → list
function parseBizType(metaKey: string): string {
  const idx = metaKey.indexOf('.meta.');
  if (idx < 0) return '—';
  const rest = metaKey.slice(idx + '.meta.'.length).replace(/\.json$/, '');
  const biz = rest.split('.')[0];
  return biz || '—';
}

type MetaFileItem = {
  name: string;
  id: string;
  type?: string; // SYSTEM / PROGRAM / MANUAL
  key?: string;
  update_time?: string | number;
  meta_key?: string;
};

export function HcmMetaFileBrowser({ embedded = false }: { embedded?: boolean }) {
  const { t } = useT();
  const urlParams = useMemo(() => new URLSearchParams(window.location.search), []);
  const initModel = urlParams.get('hcm-meta') || urlParams.get('hcm-model') || '';

  const [token, setToken] = useState(() => localStorage.getItem(LS_TOKEN) || '');
  const [model, setModel] = useState(initModel);
  const [bizType, setBizType] = useState(''); // '' = 全部
  const [srcType, setSrcType] = useState(''); // '' / SYSTEM / PROGRAM / MANUAL
  const [search, setSearch] = useState('');
  const [files, setFiles] = useState<MetaFileItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [selected, setSelected] = useState<MetaFileItem | null>(null);
  const [fileJson, setFileJson] = useState<any>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState('');
  const [qFile, setQFile] = useState('');

  // 运行真实数据
  const [runBody, setRunBody] = useState('');
  const [runSql, setRunSql] = useState(false);
  const [runProfile, setRunProfile] = useState(false);
  const [runLoading, setRunLoading] = useState(false);
  const [runError, setRunError] = useState('');
  const [runResult, setRunResult] = useState<any>(null);
  const [runMeta, setRunMeta] = useState<Record<string, any>>({});
  const [runCount, setRunCount] = useState(0);
  const [qRun, setQRun] = useState('');

  useEffect(() => {
    if (token.trim()) localStorage.setItem(LS_TOKEN, token.trim());
  }, [token]);

  // directCall：返回网关 result（data 字段）
  const directCall = useCallback(
    async (apiName: string, params: Record<string, any>, m = '') => {
      const res = await fetch(DIRECT_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_name: apiName, params, model: m, token: token.trim() }),
      });
      const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      if (!res.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
        throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
      }
      return data?.data;
    },
    [token]
  );

  // directCallRaw：返回完整 {data, meta}（运行真实数据时用）
  const directCallRaw = useCallback(
    async (
      apiName: string,
      params: Record<string, any>,
      m = '',
      opts: { sqlDebug?: boolean; profileDebug?: boolean } = {}
    ): Promise<{ data: any; meta: Record<string, any> }> => {
      const res = await fetch(DIRECT_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_name: apiName,
          params,
          model: m,
          token: token.trim(),
          sql_debug: opts.sqlDebug,
          profile_debug: opts.profileDebug,
        }),
      });
      const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      if (!res.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
        throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
      }
      return { data: data?.data, meta: data?.meta || {} };
    },
    [token]
  );

  const loadFiles = useCallback(async () => {
    if (!model.trim()) {
      setError(t('hcm.metaModelRequired'));
      return;
    }
    if (!token.trim()) {
      setError(t('hcm.configRequired'));
      return;
    }
    setLoading(true);
    setError('');
    try {
      // “全部”(bizType==='') 时 HCM 网关对空/缺失 biz_type 只返回 1 条默认布局，
      // 无法一次拉全；改为按各已知 biz_type 分别拉取后合并去重。
      // 选定具体 biz_type 时只查该类型（保持原行为）。
      const typesToQuery = bizType ? [bizType] : KNOWN_BIZ_TYPES;
      const fetchOne = async (bt: string) => {
        const res = await directCall('hcm.paas.object.layout.list', {
          model: null,
          filter_str: null,
          filter_dict: { model: model.trim(), biz_type: bt },
          query_str: null,
          page_index: 1,
          page_size: LIST_PAGE_SIZE,
          extra_property: {
            sorts: [],
            filter_params: {
              id: model.trim(),
              meta_params: '{"custom_biz_type":""}',
              main_object_str: 'hcm.paas.object.layout',
              base_object_str: 'hcm.paas.object.layout',
              key: 'hcm_model_viewer_list_layouts',
              page_index: 1,
              page_size: LIST_PAGE_SIZE,
              filter_str: null,
              show_fields_key: [],
            },
            only_list: false,
          },
          biz_type: bt,
        }, model.trim());
        return (res?.list || []) as any[];
      };
      const chunks = await Promise.all(typesToQuery.map(fetchOne));
      const seen = new Set<string>();
      const list: MetaFileItem[] = [];
      for (const chunk of chunks) {
        for (const it of chunk) {
          const key = it.id ?? it.name ?? it.key ?? JSON.stringify(it);
          if (seen.has(key)) continue;
          seen.add(key);
          list.push({
            name: it.name,
            id: it.id ?? it.name,
            type: it.type,
            key: it.key,
            update_time: it.update_time,
            meta_key: it.name,
          });
        }
      }
      setFiles(list);
      setTotal(list.length);
      if (list.length > LIST_PAGE_SIZE) {
        setError(t('hcm.metaTooMany').replace('{n}', String(list.length)));
      }
    } catch (e: any) {
      setError(e instanceof HcmApiError ? e.message : String(e?.message || e));
      setFiles([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [directCall, model, bizType, t]);

  useEffect(() => {
    if (initModel) loadFiles();
    // 仅在初次根据 URL 自动加载一次；后续由用户点刷新触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadFile = useCallback(
    async (item: MetaFileItem) => {
      setSelected(item);
      setFileLoading(true);
      setFileError('');
      setFileJson(null);
      setQFile('');
      setRunResult(null);
      setRunMeta({});
      setRunCount(0);
      setRunError('');
      try {
        const metaKey = item.meta_key || item.name;
        // hcm.paas.object.layout.list 返回的 name 带 .json 后缀，
        // 但 hcm.company.meta.load 通常按不带后缀的 meta_key 存取，否则返回 {}。
        const loadKey = metaKey.replace(/\.json$/i, '');
        const modelName = model.trim();
        const res = await directCall(
          'hcm.company.meta.load',
          { meta_key: loadKey },
          modelName // 让后端把 model 挂到 URL，多数 HCM 接口需要
        );
        // 网关返回 meta 内容为字符串（可能已二次解析），统一尝试 JSON.parse
        let parsed: any = res;
        if (typeof res === 'string') {
          try {
            parsed = JSON.parse(res);
          } catch {
            parsed = res;
          }
        }
        // 兜底：若 company meta 返回空对象且知道 model，尝试按模型维度元数据加载
        if (
          parsed != null &&
          typeof parsed === 'object' &&
          !Array.isArray(parsed) &&
          Object.keys(parsed).length === 0 &&
          modelName
        ) {
          try {
            const dotMeta = '.meta.';
            const mk = metaKey.replace(/\.json$/i, '');
            const subKey = mk.indexOf(dotMeta) >= 0 ? mk.slice(mk.indexOf(dotMeta) + dotMeta.length) : mk;
            const fallback = await directCall(
              'hcm.model.meta',
              { model: modelName, meta_key: subKey },
              modelName
            );
            if (fallback != null) parsed = fallback;
          } catch {
            /* fallback 失败保持原空对象 */
          }
        }
        setFileJson(parsed);
        // 预填运行请求体：尽量从 meta 中提取 filter_str / filter_dict
        let filter_str: string | null = null;
        let filter_dict: Record<string, any> = {};
        if (parsed && typeof parsed === 'object') {
          if (typeof parsed.filter_str === 'string') filter_str = parsed.filter_str;
          if (parsed.filter_dict && typeof parsed.filter_dict === 'object') filter_dict = parsed.filter_dict;
        }
        const payload = {
          model: modelName,
          filter_str,
          filter_dict,
          page_index: 1,
          page_size: 20,
          biz_type: parseBizType(metaKey) || 'list',
        };
        setRunBody(JSON.stringify(payload, null, 2));
      } catch (e: any) {
        setFileError(e instanceof HcmApiError ? e.message : String(e?.message || e));
      } finally {
        setFileLoading(false);
      }
    },
    [directCall, model]
  );

  const runQuery = useCallback(async () => {
    if (!selected) return;
    let payload: Record<string, any>;
    try {
      payload = runBody.trim() ? JSON.parse(runBody) : {};
      setRunError('');
    } catch (e: any) {
      setRunError(`JSON 解析失败：${e.message || e}`);
      return;
    }
    payload.model = model.trim();
    if (!payload.page_index) payload.page_index = 1;
    if (!payload.page_size) payload.page_size = 20;
    if (!payload.biz_type) payload.biz_type = parseBizType(selected.meta_key || selected.name) || 'list';

    setRunLoading(true);
    setRunError('');
    try {
      const { data, meta } = await directCallRaw(
        'hcm.model.list',
        payload,
        model.trim(),
        { sqlDebug: runSql, profileDebug: runProfile }
      );
      setRunResult(data);
      setRunMeta(meta || {});
      setRunCount(
        data?.total ?? data?.count ?? (Array.isArray(data?.list) ? data.list.length : 0)
      );
    } catch (e: any) {
      setRunError(e instanceof HcmApiError ? e.message : String(e?.message || e));
      setRunResult(null);
      setRunMeta({});
      setRunCount(0);
    } finally {
      setRunLoading(false);
    }
  }, [directCallRaw, model, runBody, runSql, runProfile, selected]);

  // 客户端过滤：biz_type 分组 + srcType + 搜索
  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = files.filter((f) => {
      if (srcType && f.type !== srcType) return false;
      if (bizType && parseBizType(f.meta_key || f.name) !== bizType) return false;
      if (q && !`${f.name} ${f.key || ''} ${f.type || ''}`.toLowerCase().includes(q)) return false;
      return true;
    });
    const map = new Map<string, MetaFileItem[]>();
    for (const f of filtered) {
      const b = parseBizType(f.meta_key || f.name);
      if (!map.has(b)) map.set(b, []);
      map.get(b)!.push(f);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [files, search, srcType, bizType]);

  const fileView = useMemo(() => (fileJson != null ? makeJsonView(fileJson, qFile) : null), [fileJson, qFile]);
  const runView = useMemo(() => (runResult != null ? makeJsonView(runResult, qRun) : null), [runResult, qRun]);

  const downloadJson = (text: string, fname: string) => {
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  const typeBadge = (type?: string) => {
    if (!type) return null;
    const cls = `hcm-type-badge hcm-type-${type.toLowerCase()}`;
    return <span className={cls}>{type}</span>;
  };

  return (
    <div className="hcm-detail-page">
      <div className="hcm-detail-head">
        <div className="hcm-detail-title">{t('hcm.metaBrowserTitle')}</div>
        <div className="hcm-detail-sub hcm-mono">{model || t('hcm.metaNoModel')}</div>
        <div className="hcm-meta-config">
          <label className="hcm-config-label">
            <span>{t('hcm.metaModel')}</span>
            <input value={model} onChange={(e) => setModel(e.target.value)} spellCheck={false} />
          </label>
          <label className="hcm-config-label">
            <span>{t('hcm.metaBizType')}</span>
            <select value={bizType} onChange={(e) => setBizType(e.target.value)}>
              <option value="">{t('hcm.metaAll')}</option>
              <option value="list">list</option>
              <option value="info">info</option>
              <option value="view">view</option>
              <option value="base">base</option>
              <option value="panel">panel</option>
              <option value="dataset">dataset</option>
            </select>
          </label>
          <label className="hcm-config-label">
            <span>{t('hcm.metaType')}</span>
            <select value={srcType} onChange={(e) => setSrcType(e.target.value)}>
              <option value="">{t('hcm.metaAll')}</option>
              <option value="SYSTEM">SYSTEM</option>
              <option value="PROGRAM">PROGRAM</option>
              <option value="MANUAL">MANUAL</option>
            </select>
          </label>
          {!embedded && (
            <label className="hcm-config-label hcm-config-token">
              <span>{t('hcm.token')}</span>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={t('hcm.tokenPlaceholder')}
                spellCheck={false}
              />
            </label>
          )}
          <button className="btn btn-sm btn-primary" onClick={loadFiles} disabled={loading}>
            {loading ? t('hcm.loading') : t('hcm.metaLoadFiles')}
          </button>
        </div>
        <div className="hcm-meta-subline">
          <span className="hcm-count">
            {t('hcm.metaFileCount')}: {files.length}/{total}
          </span>
          <input
            className="hcm-search hcm-search-inline"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('hcm.searchPlaceholder')}
            spellCheck={false}
          />
        </div>
        {error && <div className="hcm-error">{error}</div>}
      </div>

      <div className="hcm-split hcm-meta-split">
        {/* 左：文件列表（按 biz_type 分组） */}
        <div className="hcm-list">
          <div className="hcm-list-toolbar">
            <span className="hcm-count">{t('hcm.metaFileList')}</span>
            {search.trim() && (
              <span className="hcm-count">
                {t('hcm.fieldMatch')}: {grouped.reduce((n: number, [, its]) => n + its.length, 0)}
              </span>
            )}
          </div>
          <div className="hcm-list-body">
            {grouped.length === 0 && !loading && (
              <div className="hcm-empty">{files.length === 0 ? t('hcm.metaEmpty') : t('hcm.noData')}</div>
            )}
            {grouped.map(([biz, items]) => (
              <div key={biz} className="hcm-meta-group">
                <div className="hcm-meta-group-head">
                  <span className="hcm-meta-group-name">{biz}</span>
                  <span className="hcm-count">{items.length}</span>
                </div>
                <table className="hcm-table">
                  <tbody>
                    {items.map((f) => (
                      <tr
                        key={f.id}
                        className={selected?.id === f.id ? 'hcm-row-active' : ''}
                        onClick={() => loadFile(f)}
                      >
                        <td className="hcm-mono hcm-meta-name" title={f.name}>
                          <span dangerouslySetInnerHTML={{ __html: highlightText(f.name, search) }} />
                        </td>
                        <td className="hcm-meta-type">{typeBadge(f.type)}</td>
                        <td className="hcm-meta-time">{f.update_time || ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        </div>

        {/* 右：文件内容 + 运行真实数据 */}
        <div className="hcm-detail hcm-meta-detail">
          {!selected && <div className="hcm-empty hcm-detail-empty">{t('hcm.metaPickHint')}</div>}
          {selected && (
            <>
              <div className="hcm-detail-head hcm-meta-file-head">
                <div className="hcm-detail-title">{selected.name}</div>
                <div className="hcm-detail-sub hcm-mono">
                  meta_key: {selected.meta_key || selected.name}
                </div>
                <div className="hcm-meta-file-meta">
                  {typeBadge(selected.type)}
                  {selected.key && <span className="hcm-mono">key: {selected.key}</span>}
                  {selected.update_time && <span className="hcm-mono">{t('hcm.metaUpdateTime')}: {selected.update_time}</span>}
                </div>
              </div>

              {/* 文件 JSON */}
              <div className="hcm-meta-section">
                <div className="hcm-fields-toolbar">
                  <span className="hcm-meta-section-title">{t('hcm.metaFileContent')}</span>
                  {fileLoading && <span className="hcm-loading">{t('hcm.loading')}…</span>}
                  <button
                    className="btn btn-sm hcm-copy-btn"
                    onClick={() => fileJson != null && downloadJson(JSON.stringify(fileJson, null, 2), selected.name)}
                  >
                    {t('hcm.saveJson')}
                  </button>
                </div>
                {fileError && <div className="hcm-error">{fileError}</div>}
                {fileView && (
                  <JsonBlock
                    html={fileView.html}
                    text={fileView.text}
                    matched={fileView.matched}
                    q={qFile}
                    setQ={setQFile}
                    t={t}
                  />
                )}
              </div>

              {/* 运行真实数据 */}
              <div className="hcm-meta-section hcm-meta-run">
                <div className="hcm-fields-toolbar">
                  <span className="hcm-meta-section-title">{t('hcm.metaRealData')}</span>
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={runQuery}
                    disabled={runLoading}
                  >
                    {runLoading ? t('hcm.loading') : t('hcm.metaRunQuery')}
                  </button>
                  <label className="hcm-adv-checks hcm-meta-check">
                    <input type="checkbox" checked={runSql} onChange={(e) => setRunSql(e.target.checked)} />
                    {t('hcm.sqlDebug')}
                  </label>
                  <label className="hcm-adv-checks hcm-meta-check">
                    <input type="checkbox" checked={runProfile} onChange={(e) => setRunProfile(e.target.checked)} />
                    {t('hcm.profileDebug')}
                  </label>
                </div>
                <div className="hcm-adv-vertical">
                  <textarea
                    className="hcm-json-input"
                    value={runBody}
                    onChange={(e) => setRunBody(e.target.value)}
                    spellCheck={false}
                  />
                  <div className="hcm-meta-run-hint">{t('hcm.requestBodyHint')}</div>
                </div>
                {runError && <div className="hcm-error">{runError}</div>}
                {runResult != null && (
                  <div className="hcm-meta-run-result">
                    <div className="hcm-fields-toolbar">
                      <span className="hcm-count">
                        {t('hcm.dataCount')}: {runCount}
                        {runMeta?.duration_ms != null && (
                          <span className="hcm-meta-duration"> · {t('hcm.duration')}: {runMeta.duration_ms}ms</span>
                        )}
                      </span>
                      <button
                        className="btn btn-sm hcm-copy-btn"
                        onClick={() => downloadJson(JSON.stringify(runResult, null, 2), `${model || 'model'}.data.json`)}
                      >
                        {t('hcm.saveJson')}
                      </button>
                    </div>
                    {runView && (
                      <JsonBlock
                        html={runView.html}
                        text={runView.text}
                        matched={runView.matched}
                        q={qRun}
                        setQ={setQRun}
                        t={t}
                      />
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// 与 HcmModelDetail 同构的 JsonBlock（搜索高亮 + 复制）
function JsonBlock({
  html, text, matched, q, setQ, t,
}: {
  html: string; text: string; matched: number; q: string; setQ: (v: string) => void;
  t: (k: string) => string;
}) {
  const preRef = useRef<HTMLPreElement>(null);
  const [cur, setCur] = useState(0);
  const kw = q.trim();
  useEffect(() => {
    setCur(0);
  }, [kw]);
  const goto = useCallback(
    (idx: number) => {
      if (!preRef.current || matched === 0) return;
      const clamped = ((idx - 1 + matched) % matched) + 1;
      const el = preRef.current.querySelector<HTMLElement>(`mark[data-mid="${clamped}"]`);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setCur(clamped);
      }
    },
    [matched]
  );
  useEffect(() => {
    const root = preRef.current;
    if (!root) return;
    root.querySelectorAll('mark.active').forEach((m) => m.classList.remove('active'));
    if (cur > 0) root.querySelector(`mark[data-mid="${cur}"]`)?.classList.add('active');
  }, [cur, html]);
  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter' || !kw) return;
    e.preventDefault();
    if (matched === 0) return;
    if (e.shiftKey) goto(cur <= 1 ? matched : cur - 1);
    else goto(cur === 0 ? 1 : cur >= matched ? 1 : cur + 1);
  };
  const copyJson = (s: string) => navigator.clipboard?.writeText(s).catch(() => {});
  return (
    <>
      <div className="hcm-fields-toolbar">
        <input
          className="hcm-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t('hcm.jsonSearch')}
          spellCheck={false}
        />
        {kw && (
          <span className="hcm-count">
            {t('hcm.jsonMatch')}: {matched > 0 ? `${cur === 0 ? 1 : cur}/${matched}` : 0}
          </span>
        )}
        <button className="btn btn-sm hcm-copy-btn" onClick={() => copyJson(text)}>
          {t('hcm.copyJson')}
        </button>
      </div>
      <pre ref={preRef} className="hcm-json hcm-json-block" dangerouslySetInnerHTML={{ __html: html }} />
    </>
  );
}
