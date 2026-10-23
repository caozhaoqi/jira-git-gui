/**
 * HCM 查询结果的独立表格窗口（``/web/?hcm-table=1&hcm-table-key=...``）。
 *
 * 为什么需要独立窗口
 * ------------------
 * 主面板纵向空间被工具条 / 筛选条 / 页脚吃掉大半；列多（实测几十列）行多
 *（实测近千条）时，面板内怎么调都不够看。独立窗口可以最大化、拖到另一块屏。
 *
 * 数据从哪来（重要）
 * ------------------
 * 早期实现只吃主窗口写在 sessionStorage 里的**一份快照**，于是：
 *   - 快照写入失败（配额）/ 窗口刷新后，打开就是「0 rows」（实测故障）；
 *   - 窗口内想换模型 / 翻页 / 重新查询，完全做不到。
 * 现在改为**窗口自己查**：URL 上的 model + target + token 足以直接打
 * ``/api/hcm/direct``；sessionStorage 快照降级为「首屏加速」（有就现用，
 * 没有就自己拉）。这样窗口是自洽的，刷新也不丢。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { HcmDataTable } from './HcmDataTable';
import { writeClipboardText } from '../../utils/clipboard';
import { useT } from '../../i18n';
import { useStandaloneTheme } from '../../utils/standaloneTheme';
import { useHcmToken } from '../../hooks/useHcmToken';
import { useHcmDataQuery, useHcmModels } from './useHcmDataQuery';

type Payload = {
  model?: string;
  rows?: Record<string, unknown>[];
  at?: string;
  target?: string;
  /** 主面板已加载的模型列表（独立窗口自己调 paas.object.list 未必可用） */
  models?: { id: string; description?: string }[];
};

function readPayload(key: string): Payload | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const d = JSON.parse(raw) as Payload;
    sessionStorage.removeItem(key);
    return d;
  } catch {
    return null;
  }
}

export function HcmTableWindow() {
  // 独立窗口不经过 store 初始化，主题类名要自己落地（否则暗色下是白窗）
  useStandaloneTheme();
  const { t } = useT();
  const qs = useMemo(() => new URLSearchParams(window.location.search), []);
  const { token, isExpired, refresh } = useHcmToken();

  const [model, setModel] = useState('');
  const [target, setTarget] = useState('');
  const [seedRows, setSeedRows] = useState<Record<string, unknown>[] | null>(null);
  const [booted, setBooted] = useState(false);
  const [missing, setMissing] = useState(false);
  /** 快照里带来的模型列表（优先于窗口自行拉取的结果） */
  const [seedModels, setSeedModels] = useState<{ id: string; description?: string }[]>([]);

  // 首屏：优先用快照（秒开），没有则按 URL 参数自己查
  useEffect(() => {
    const key = qs.get('hcm-table-key') || '';
    const qModel = qs.get('hcm-model') || '';
    const qTarget = qs.get('hcm-target') || '';
    const snap = key ? readPayload(key) : null;

    const m = snap?.model || qModel;
    const tg = snap?.target || qTarget;
    if (!m) { setMissing(true); setBooted(true); return; }

    setModel(m);
    setTarget(tg);
    if (snap?.rows?.length) setSeedRows(snap.rows);
    if (snap?.models?.length) setSeedModels(snap.models);
    document.title = `${m} · ${t('hcm.dataTableWindowTitle')}`;
    setBooted(true);
  }, [qs, t]);

  const { state, load, pageSize, changePageSize, goPage } = useHcmDataQuery({
    targetUrl: target,
    token,
    usePresetToken: false,
  });
  const { models: fetchedModels } = useHcmModels({
    targetUrl: target,
    token,
    usePresetToken: false,
    fallbackModel: model,
  });
  // 合并：快照 > 自拉 > 当前模型（保证下拉框永远有可选项）
  const models = useMemo(() => {
    const seen = new Set<string>();
    const out: { id: string; description?: string }[] = [];
    for (const src of [seedModels, fetchedModels]) {
      for (const m of src) {
        if (!m.id || seen.has(m.id)) continue;
        seen.add(m.id);
        out.push(m);
      }
    }
    if (model && !seen.has(model)) out.unshift({ id: model });
    out.sort((a, b) => a.id.localeCompare(b.id));
    return out;
  }, [seedModels, fetchedModels, model]);

  // 无快照时自动查一次（窗口自洽：刷新/直接打开也能出数据）
  useEffect(() => {
    if (!booted || !model || !target) return;
    if (seedRows?.length) return;        // 快照够用，先不打扰后端
    if (state.loading || state.rows.length || state.error) return;
    void load(model, 1, { pageSize: 50 });
  }, [booted, model, target, seedRows, state.loading, state.rows.length, state.error, load]);

  const ensureToken = useCallback(async (): Promise<void> => {
    if (!isExpired) return;
    await refresh(target);
  }, [isExpired, refresh, target]);

  const requery = useCallback(async () => {
    if (!model || !target) return;
    setSeedRows(null);
    await ensureToken();
    void load(model, 1, { pageSize });
  }, [model, target, pageSize, ensureToken, load]);

  const rows = seedRows?.length ? seedRows : state.rows;

  const copyTsv = () => {
    if (!rows.length) return;
    const colList: string[] = [];
    const seen = new Set<string>();
    for (const r of rows) for (const k of Object.keys(r)) if (!seen.has(k)) { seen.add(k); colList.push(k); }
    const esc = (v: unknown) => {
      if (v === null || v === undefined) return '';
      const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return /[\t\n"]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    writeClipboardText([
      colList.join('\t'),
      ...rows.map((r) => colList.map((c) => esc(r[c])).join('\t')),
    ].join('\n'));
  };

  if (!booted) return null;

  if (missing) {
    return (
      <div className="hcm-dt-window">
        <div className="hcm-dt-empty">
          <div className="hcm-dt-empty-icon" aria-hidden="true">▦</div>
          <div>{t('hcm.dataTableWindowMissing')}</div>
          <div className="hcm-dt-empty-hint">{t('hcm.dataTableWindowMissingHint')}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="hcm-dt-window">
      {/* 顶部：模型切换 + 环境 + 刷新/复制/关闭。窄屏换行不挤压 */}
      <div className="hcm-dt-window-head">
        <span className="hcm-dt-window-title">{t('hcm.dataTableWindowTitle')}</span>
        <label className="hcm-dt-model">
          <span className="hcm-dt-field-lbl">{t('hcm.dt.model')}</span>
          <select
            className="sel hcm-dt-model-sel"
            value={model}
            onChange={(e) => {
              const m = e.target.value;
              setModel(m);
              setSeedRows(null);
              if (m) void load(m, 1, { pageSize });
            }}
          >
            {models.length === 0 && model && <option value={model}>{model}</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}{m.description ? ` · ${m.description}` : ''}
              </option>
            ))}
          </select>
        </label>
        {target && (
          <span className="hcm-dt-window-meta hcm-mono" title={target}>
            {target.replace(/^https?:\/\//, '')}
          </span>
        )}
        <span className="spacer" />
        <button
          type="button"
          className="btn btn-sm hcm-dt-act"
          onClick={() => { void requery(); }}
          disabled={state.loading || !model}
          title={t('hcm.dt.refreshHint')}
        >
          {state.loading ? t('hcm.loading') : t('hcm.dt.refresh')}
        </button>
        <button
          type="button"
          className="btn btn-sm hcm-dt-act"
          onClick={copyTsv}
          disabled={!rows.length}
          title={t('hcm.dataCopyTsvHint')}
        >
          {t('hcm.dataCopyTsv')}
        </button>
        <button
          type="button"
          className="btn btn-sm hcm-dt-act"
          onClick={() => window.close()}
        >
          {t('common.close')}
        </button>
      </div>

      {state.error && (
        <div className="hcm-error hcm-dt-window-error" role="alert">{state.error}</div>
      )}

      <div className="hcm-dt-window-body">
        <HcmDataTable
          rows={rows}
          showIndex
          searchable
          emptyText={state.loading ? t('hcm.loading') : t('hcm.dataTableEmpty')}
          /* 不传 toolbarExtra：表格自带「行 · 列」统计，重复显示会打架 */

        />
      </div>

      {/* 服务端分页：窗口的数据由后端分页拿取（983 条不可能一次全拉）。
          表格内不再叠一个客户端分页，避免出现两条页码互相矛盾（实测踩过）。 */}
      {state.total > 0 && (
        <div className="hcm-dt-pager hcm-dt-pager--server">
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => goPage(1)}
            disabled={state.page <= 1 || state.loading}
            title={t('hcm.dt.firstPage')}
            aria-label={t('hcm.dt.firstPage')}
          >
            «
          </button>
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => goPage(Math.max(1, state.page - 1))}
            disabled={state.page <= 1 || state.loading}
            title={t('hcm.dt.prevPage')}
            aria-label={t('hcm.dt.prevPage')}
          >
            ‹
          </button>
          <span className="hcm-dt-page-info">
            {t('hcm.dt.pageInfo', {
              page: state.page,
              total: Math.max(1, Math.ceil(state.total / state.pageSize)),
            })}
          </span>
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => goPage(state.page + 1)}
            disabled={state.page >= Math.ceil(state.total / state.pageSize) || state.loading}
            title={t('hcm.dt.nextPage')}
            aria-label={t('hcm.dt.nextPage')}
          >
            ›
          </button>
          <button
            type="button"
            className="btn btn-sm hcm-dt-page-btn"
            onClick={() => goPage(Math.max(1, Math.ceil(state.total / state.pageSize)))}
            disabled={state.page >= Math.ceil(state.total / state.pageSize) || state.loading}
            title={t('hcm.dt.lastPage')}
            aria-label={t('hcm.dt.lastPage')}
          >
            »
          </button>
          <span className="hcm-dt-page-total hcm-mono">
            {t('hcm.dt.totalRecords', { n: state.total })}
          </span>
          <span className="spacer" />
          <label className="hcm-dt-pagesize">
            {t('hcm.dt.perPage')}
            <select
              className="sel"
              value={state.pageSize}
              onChange={(e) => changePageSize(Number(e.target.value))}
            >
              {[20, 50, 100, 200, 500].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>
        </div>
      )}
    </div>
  );
}
