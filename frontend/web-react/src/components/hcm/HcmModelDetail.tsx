import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useT } from '../../i18n';
import { HcmApiError } from '../../api/hcm/client';
import type { HcmFieldMeta, HcmModelMeta } from '../../api/hcm/types';
import { HcmMetaFileBrowser } from './HcmMetaFileBrowser';

const LS_TOKEN = 'hcm.token';

// 所有连接统一走后端直连 /api/hcm/direct（相对路径），由后端直连 HCM 网关并解密返回明文。
const DIRECT_ENDPOINT = '/api/hcm/direct';

// HTML 转义，避免 JSON 内容破坏高亮标记或注入
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// 正则特殊字符转义，使搜索关键字可安全用于 RegExp
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

type DetailTab = 'fields' | 'list' | 'info' | 'view' | 'meta' | 'files';

// 按展示维度从完整 meta 中裁剪出对应 JSON 节点
function sliceByKind(meta: HcmModelMeta, kind: Exclude<DetailTab, 'fields' | 'meta'>): Record<string, any> {
  if (kind === 'list') {
    // 列表视图：标记 is_list / is_list_display 的字段 + 列表相关属性
    const fields = (meta.fields || []).filter((f) => f.is_list || f.is_list_display);
    return {
      meta_key: 'list',
      model: meta.model,
      persistence_table: meta.persistence_table ?? null,
      property: meta.property ?? {},
      fields,
    };
  }
  if (kind === 'info') {
    // 详情视图：标记 is_info 的字段 + 详情相关属性
    const fields = (meta.fields || []).filter((f) => f.is_info);
    return {
      meta_key: 'info',
      model: meta.model,
      description: meta.description ?? null,
      property: meta.property ?? {},
      fields,
    };
  }
  // view：表单视图 → 子对象 / 操作 / 校验 / 规则等视图相关节点
  return {
    meta_key: 'view',
    model: meta.model,
    childrens: meta.childrens ?? [],
    action: meta.action ?? [],
    rules: meta.rules ?? [],
    validators: meta.validators ?? [],
    plugins: meta.plugins ?? [],
    include: meta.include ?? [],
    extend: meta.extend ?? [],
  };
}

export function HcmModelDetail() {
  const { t } = useT();
  const urlParams = useMemo(() => new URLSearchParams(window.location.search), []);
  const modelId = useMemo(
    () => urlParams.get('hcm-model') || urlParams.get('hcm-meta') || '',
    [urlParams]
  );

  // 初始 tab：?hcm-tab 可指定（如 files）；仅有 ?hcm-meta 时默认定位到「元数据文件」；否则默认字段表。
  const initialTab = useMemo<DetailTab>(() => {
    const req = urlParams.get('hcm-tab');
    if (req === 'files' || req === 'list' || req === 'info' || req === 'view' || req === 'meta') return req;
    if (urlParams.has('hcm-meta')) return 'files';
    return 'fields';
  }, [urlParams]);

  const [token, setToken] = useState(() => localStorage.getItem(LS_TOKEN) || '');
  const [tab, setTab] = useState<DetailTab>(initialTab);
  const [meta, setMeta] = useState<HcmModelMeta | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // 各 JSON 区块独立搜索关键字
  const [qFields, setQFields] = useState('');
  const [qList, setQList] = useState('');
  const [qInfo, setQInfo] = useState('');
  const [qView, setQView] = useState('');
  const [qMeta, setQMeta] = useState('');

  // token 自动持久化：每次变更（非空）写入 localStorage，避免漏存。
  useEffect(() => {
    if (token.trim()) localStorage.setItem(LS_TOKEN, token.trim());
  }, [token]);

  // 统一走后端直连：POST /api/hcm/direct，后端直连 HCM 网关并解密返回明文。
  const directCall = useCallback(async (apiName: string, params: Record<string, any>, model = '') => {
    const res = await fetch(DIRECT_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_name: apiName, params, model, token: token.trim() }),
    });
    const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
    if (!res.ok) {
      const detail =
        typeof data?.detail === 'string' ? data.detail : JSON.stringify(data?.detail ?? data);
      throw new HcmApiError(detail || `HTTP ${res.status}`, res.status);
    }
    return data?.data;
  }, [token]);

  const load = useCallback(async () => {
    if (!modelId) {
      setError(t('hcm.detailNoModel'));
      return;
    }
    if (!token.trim()) {
      setError(t('hcm.configRequired'));
      return;
    }
    setLoading(true);
    setError('');
    try {
      const m = await directCall('hcm.model.meta', { model: modelId }, modelId);
      setMeta(m);
    } catch (e: any) {
      setError(e instanceof HcmApiError ? e.message : String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, [directCall, modelId, token, t]);

  useEffect(() => {
    load();
  }, [load]);

  // 通用 JSON 高亮搜索：始终展示完整 JSON，命中关键字以 <mark data-mid> 高亮（不裁剪整行）。
  const makeJsonView = (data: any, q: string) => {
    const raw = JSON.stringify(data, null, 2);
    const kw = q.trim();
    if (!kw) return { html: escapeHtml(raw), text: raw, matched: 0 };
    const lower = kw.toLowerCase();
    const lines = raw.split('\n');
    let matched = 0;
    const html = lines
      .map((ln) => {
        const has = ln.toLowerCase().includes(lower);
        // 行内高亮命中片段（大小写不敏感、转义后注入 <mark data-mid>）
        const escaped = escapeHtml(ln);
        const re = new RegExp(`(${escapeRegExp(kw)})`, 'gi');
        if (!has) return escaped;
        matched += 1;
        return escaped.replace(re, (m) => `<mark data-mid="${matched}">${m}</mark>`);
      })
      .join('\n');
    return { html, text: raw, matched };
  };

  const jsonList = useMemo(
    () => (meta ? makeJsonView(sliceByKind(meta, 'list'), qList) : null),
    [meta, qList]
  );
  const jsonInfo = useMemo(
    () => (meta ? makeJsonView(sliceByKind(meta, 'info'), qInfo) : null),
    [meta, qInfo]
  );
  const jsonView = useMemo(
    () => (meta ? makeJsonView(sliceByKind(meta, 'view'), qView) : null),
    [meta, qView]
  );
  const jsonMeta = useMemo(
    () => (meta ? makeJsonView(meta, qMeta) : null),
    [meta, qMeta]
  );

  const copyJson = (text: string) => {
    navigator.clipboard?.writeText(text).catch(() => {});
  };

  // 保存某维度的 JSON 为独立文件（如 AICognitionModelDomain.meta.list.json）。
  // 直接触发浏览器下载，不弹确认框、不新开预览标签；保存后展示文件名作为路径反馈。
  const [savedPath, setSavedPath] = useState('');
  const saveKindJson = useCallback(
    (kind: Exclude<DetailTab, 'fields'>) => {
      if (!meta) return;
      const data = kind === 'meta' ? meta : sliceByKind(meta, kind);
      const json = JSON.stringify(data, null, 2);
      const fname = `${modelId || 'model'}.meta.${kind}.json`;
      const blob = new Blob([json], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      // 仅触发下载，文件名标准化为 <model>.meta.<kind>.json
      const a = document.createElement('a');
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      // 浏览器不允许 JS 读取真实绝对路径，展示文件名作为“已保存路径”反馈
      setSavedPath(fname);
    },
    [meta, modelId]
  );

  return (
    <div className="hcm-detail-page">
      <div className="hcm-detail-head">
        <div className="hcm-detail-title">{modelId || t('hcm.detailNoModel')}</div>
        {meta?.description && <div className="hcm-detail-sub2">{meta.description}</div>}
        <div className="hcm-detail-sub hcm-mono">{meta?.model || modelId}</div>
        <label className="hcm-detail-token">
          <span>{t('hcm.token')}</span>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={t('hcm.tokenPlaceholder')}
            spellCheck={false}
          />
          <button className="btn btn-sm btn-primary" onClick={load} disabled={loading}>
            {loading ? t('hcm.loading') : t('hcm.refresh')}
          </button>
        </label>
        {error && <div className="hcm-error">{error}</div>}
        {loading && <div className="hcm-loading">{t('hcm.loadingMeta')}…</div>}
      </div>

      <div className="hcm-subtabs">
        <button className={tab === 'fields' ? 'btn btn-sm btn-active' : 'btn btn-sm'} onClick={() => setTab('fields')}>
          {t('hcm.tabFields')}
        </button>
        <button className={tab === 'list' ? 'btn btn-sm btn-active' : 'btn btn-sm'} onClick={() => setTab('list')}>
          list JSON
        </button>
        <button className={tab === 'info' ? 'btn btn-sm btn-active' : 'btn btn-sm'} onClick={() => setTab('info')}>
          info JSON
        </button>
        <button className={tab === 'view' ? 'btn btn-sm btn-active' : 'btn btn-sm'} onClick={() => setTab('view')}>
          view JSON
        </button>
        <button className={tab === 'meta' ? 'btn btn-sm btn-active' : 'btn btn-sm'} onClick={() => setTab('meta')}>
          {t('hcm.tabJson')}
        </button>
        <button className={tab === 'files' ? 'btn btn-sm btn-active' : 'btn btn-sm'} onClick={() => setTab('files')}>
          {t('hcm.tabMetaFiles')}
        </button>
        <span className="hcm-kind-sep" />
        {/* 保存 JSON 为独立按钮：仅当前查看维度可保存，直接下载不弹确认 */}
        {tab !== 'fields' && (
          <button className="btn btn-sm btn-primary" onClick={() => saveKindJson(tab)}>
            {t('hcm.saveJson')}
          </button>
        )}
        <button className="btn btn-sm" onClick={load} disabled={loading}>
          {t('hcm.refresh')}
        </button>
        <span className="hcm-kind-sep" />
        <button
          className="btn btn-sm"
          onClick={() =>
            window.open(
              `/web/?hcm-cf-err=1&hcm-loc-model=${encodeURIComponent(modelId)}`,
              '_blank',
              'width=1100,height=820'
            )
          }
          title={t('hcm.cfErrLauncherHint')}
        >
          {t('hcm.cfErrLauncher')}
        </button>
      </div>

      {savedPath && (
        <div className="hcm-saved-path">
          {t('hcm.savedPath')}: <span className="hcm-mono">{savedPath}</span>
        </div>
      )}

      <div className="hcm-detail-body">
        {/* 字段表 */}
        {tab === 'fields' && meta && (
          <FieldsBlock
            fields={meta.fields || []}
            q={qFields}
            setQ={setQFields}
            total={meta.fields?.length || 0}
            t={t}
          />
        )}

        {/* list JSON */}
        {tab === 'list' && jsonList && (
          <JsonBlock html={jsonList.html} text={jsonList.text} matched={jsonList.matched} q={qList} setQ={setQList} onCopy={copyJson} t={t} />
        )}

        {/* info JSON */}
        {tab === 'info' && jsonInfo && (
          <JsonBlock html={jsonInfo.html} text={jsonInfo.text} matched={jsonInfo.matched} q={qInfo} setQ={setQInfo} onCopy={copyJson} t={t} />
        )}

        {/* view JSON */}
        {tab === 'view' && jsonView && (
          <JsonBlock html={jsonView.html} text={jsonView.text} matched={jsonView.matched} q={qView} setQ={setQView} onCopy={copyJson} t={t} />
        )}

        {/* 元数据 JSON（完整） */}
        {tab === 'meta' && jsonMeta && (
          <JsonBlock html={jsonMeta.html} text={jsonMeta.text} matched={jsonMeta.matched} q={qMeta} setQ={setQMeta} onCopy={copyJson} t={t} />
        )}

        {/* 元数据文件浏览器：内嵌 HcmMetaFileBrowser（合并「查看元数据」独立窗口） */}
        {tab === 'files' && (
          <div className="hcm-detail-files">
            <HcmMetaFileBrowser embedded />
          </div>
        )}
      </div>
    </div>
  );
}

function FieldsBlock({
  fields, q, setQ, total, t,
}: {
  fields: HcmFieldMeta[]; q: string; setQ: (v: string) => void; total: number; t: (k: string) => string;
}) {
  const qw = q.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      qw
        ? fields.filter((f) =>
            [f.key, f.name, f.type, f.description].filter(Boolean).some((v) => String(v).toLowerCase().includes(qw))
          )
        : fields,
    [fields, qw]
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const [cur, setCur] = useState(0);

  useEffect(() => {
    setCur(0);
  }, [qw]);

  const goto = useCallback(
    (idx: number) => {
      if (filtered.length === 0) return;
      const n = filtered.length;
      const clamped = ((idx - 1 + n) % n) + 1;
      const root = scrollRef.current;
      const row = root?.querySelector<HTMLElement>(`tr[data-rid="${clamped}"]`);
      if (row) {
        row.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setCur(clamped);
      }
    },
    [filtered.length]
  );

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter' || !qw) return;
    e.preventDefault();
    if (filtered.length === 0) return;
    if (e.shiftKey) goto(cur <= 1 ? filtered.length : cur - 1);
    else goto(cur === 0 ? 1 : cur >= filtered.length ? 1 : cur + 1);
  };

  return (
    <>
      <div className="hcm-fields-toolbar">
        <input
          className="hcm-search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t('hcm.fieldSearch')}
          spellCheck={false}
        />
        {qw ? (
          <span className="hcm-count">
            {t('hcm.fieldMatch')}: {filtered.length}/{total}
            {filtered.length > 0 && `  (${cur === 0 ? 1 : cur}/${filtered.length})`}
          </span>
        ) : (
          <span className="hcm-count">{t('hcm.fieldTotal')}: {total}</span>
        )}
      </div>
      <div className="hcm-fields" ref={scrollRef}>
        <table className="hcm-table">
          <thead>
            <tr>
              <th>{t('hcm.fKey')}</th><th>{t('hcm.fName')}</th><th>{t('hcm.fType')}</th>
              <th>{t('hcm.fRequired')}</th><th>{t('hcm.fLen')}</th><th>{t('hcm.fDesc')}</th>
              <th>list</th><th>info</th><th>view</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((f: HcmFieldMeta, i: number) => {
              const rid = i + 1;
              return (
                <tr key={f.key} data-rid={rid} className={cur === rid ? 'hcm-row-active' : ''}>
                  <td className="hcm-mono">{f.key}</td>
                  <td>{f.name}</td>
                  <td className="hcm-mono">{f.type}</td>
                  <td>{f.is_required ? '✓' : ''}</td>
                  <td>{f.length ?? ''}</td>
                  <td>{f.description || '—'}</td>
                  <td>{f.is_list || f.is_list_display ? '✓' : ''}</td>
                  <td>{f.is_info ? '✓' : ''}</td>
                  <td>{f.is_blur ? '✓' : ''}</td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr><td colSpan={9} className="hcm-empty">{t('hcm.noData')}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}

function JsonBlock({
  html, text, matched, q, setQ, onCopy, t,
}: {
  html: string; text: string; matched: number; q: string; setQ: (v: string) => void;
  onCopy: (s: string) => void; t: (k: string) => string;
}) {
  const preRef = useRef<HTMLPreElement>(null);
  // 当前激活的命中项序号（1-based）。搜索词变化时归位到 1。
  const [cur, setCur] = useState(0);
  const kw = q.trim();

  // 搜索词变化重置激活项为 0（未定位）
  useEffect(() => {
    setCur(0);
  }, [kw]);

  // 跳转到指定命中项：滚动可见 + 高亮当前 mark
  const goto = useCallback(
    (idx: number) => {
      if (!preRef.current || matched === 0) return;
      const clamped = ((idx - 1 + matched) % matched) + 1; // 1..matched 循环
      const el = preRef.current.querySelector<HTMLElement>(`mark[data-mid="${clamped}"]`);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
        setCur(clamped);
      }
    },
    [matched]
  );

  // 同步 active 高亮类：当 cur 变化时在 DOM 上标记当前命中项
  useEffect(() => {
    const root = preRef.current;
    if (!root) return;
    root.querySelectorAll('mark.active').forEach((m) => m.classList.remove('active'));
    if (cur > 0) {
      root.querySelector(`mark[data-mid="${cur}"]`)?.classList.add('active');
    }
  }, [cur, html]);

  // 回车 → 下一项；Shift+回车 → 上一项
  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter' || !kw) return;
    e.preventDefault();
    if (matched === 0) return;
    if (e.shiftKey) goto(cur <= 1 ? matched : cur - 1);
    else goto(cur === 0 ? 1 : cur >= matched ? 1 : cur + 1);
  };

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
        <button className="btn btn-sm hcm-copy-btn" onClick={() => onCopy(text)}>{t('hcm.copyJson')}</button>
      </div>
      <pre ref={preRef} className="hcm-json hcm-json-block" dangerouslySetInnerHTML={{ __html: html }} />
    </>
  );
}
