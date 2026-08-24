import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiGet, apiPost } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { readClipboardText, writeClipboardText } from '../utils/clipboard';
import type { CfAccount, CfLogsRow } from '../api/types';

const CF_CFG_KEY = 'jgg-cf-cfg';

interface CfCfg {
  server_url: string;
  username: string;
  password: string;
  token: string;
  proxy: string;
  log_type: string;
  page_size: number;
  page_index: number;
}

interface CfLastResult {
  server_url: string;
  log_type: string;
  auth_method: string;
  page_index: number;
  page_size: number;
  total: number;
  rows: CfLogsRow[];
  raw: any;
  localPage: number;
}

function cfTime(row: CfLogsRow): string {
  return String(row.create_time || row.createTime || row.created_at || '');
}
function cfContent(row: CfLogsRow): string {
  const c = row.content ?? row.message ?? row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c) : String(c);
}
function cfContentFull(row: CfLogsRow): string {
  const c = row.content ?? row.message ?? row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c, null, 2) : String(c);
}
function cfLogType(row: CfLogsRow, fallback: string): string {
  return row.log_type || row.logType || fallback || '(未知)';
}

export function CfPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const { t } = useT();

  const [accounts, setAccounts] = useState<CfAccount[]>([]);
  const [env, setEnv] = useState('');
  const [cfg, setCfg] = useState<CfCfg>({
    server_url: '',
    username: '',
    password: '',
    token: '',
    proxy: '',
    log_type: '',
    page_size: 200,
    page_index: 1,
  });
  const [captcha, setCaptcha] = useState<{ captcha_id: string; image_code_index: string; image: string }>({
    captcha_id: '',
    image_code_index: '',
    image: '',
  });
  const [imageCode, setImageCode] = useState('');
  const [result, setResult] = useState<CfLastResult | null>(null);
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');
  const [search, setSearch] = useState('');
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [status, setStatus] = useState<{ text: string; cls: string }>({ text: '', cls: '' });
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [cfgOpen, setCfgOpen] = useState(false);
  const [tokenMap, setTokenMap] = useState<Record<string, any>>({});
  const [autoLogin, setAutoLogin] = useState<{ running: boolean; msg: string }>({ running: false, msg: '' });

  const cfgRef = useRef(cfg);
  cfgRef.current = cfg;
  const resultRef = useRef(result);
  resultRef.current = result;

  const setBusy = (k: string, v: boolean) =>
    setLoading((m) => ({ ...m, [k]: v }));

  const saveCfg = useCallback((override?: Partial<CfCfg>) => {
    try {
      localStorage.setItem(CF_CFG_KEY, JSON.stringify({ ...cfgRef.current, ...(override || {}) }));
    } catch {
      /* ignore */
    }
  }, []);

  const loadCfg = useCallback(() => {
    try {
      const raw = localStorage.getItem(CF_CFG_KEY);
      if (raw) setCfg((c) => ({ ...c, ...JSON.parse(raw) }));
    } catch {
      /* ignore */
    }
  }, []);

  const loadAccounts = useCallback(async () => {
    try {
      const d = await apiGet<{ accounts?: CfAccount[] }>('/api/cf/accounts');
      const list = Array.isArray(d.accounts) ? d.accounts : [];
      setAccounts(list);
      if (list.length === 0) {
        pushLog('[CF] 本地 cf_accounts.local.json 未读到任何账号，下拉框只有「自定义」可选', 'warning');
      } else {
        pushLog(`[CF] 已从本地配置加载 ${list.length} 个账号：${list.map((a) => a.name).join('、')}`);
      }
    } catch {
      setAccounts([]);
    }
  }, [pushLog]);

  const loadTokens = useCallback(async (): Promise<Record<string, any>> => {
    try {
      const d = await apiGet<{ tokens?: Array<{
        server_url: string; has_token: boolean; token_masked: string;
        need_captcha: boolean; last_error: string; name: string;
      }> }>('/api/cf/tokens');
      const map: Record<string, any> = {};
      (d.tokens || []).forEach((tk) => { map[tk.server_url] = tk; });
      setTokenMap(map);
      return map;
    } catch {
      setTokenMap({});
      return {};
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadAccounts();
      loadCfg();
      await loadTokens();
    })();
  }, [loadAccounts, loadCfg, loadTokens]);

  const switchEnv = (key: string) => {
    setEnv(key);
    // 清空上一个环境的登录态残留（token/验证码），避免用旧环境的 token 去查询新环境
    const clearLoginState = () => {
      setImageCode('');
      setCaptcha({ captcha_id: '', image_code_index: '', image: '' });
    };
    if (!key || key === 'custom') {
      setCfg((c) => ({ ...c, token: '' }));
      clearLoginState();
      saveCfg({ token: '' });
      return;
    }
    const acc = accounts.find((a) => a.name === key);
    if (!acc) return;
    setCfg((c) => ({
      ...c,
      server_url: acc.server_url || '',
      username: acc.username || '',
      password: acc.password || '',
      token: '', // 切换环境清空 token，查询时由后端按新 server_url 复用自动获取的缓存 cookie
    }));
    clearLoginState();
    saveCfg({
      server_url: acc.server_url || '',
      username: acc.username || '',
      password: acc.password || '',
      token: '',
    });
    addToast(`已切换到「${acc.name}」环境，账号密码已预填`, 'info');
  };

  const fetchCaptcha = async () => {
    const serverUrl = cfg.server_url.trim();
    const proxy = cfg.proxy.trim();
    if (!serverUrl) {
      addToast(t('cf.serverUrlFirst'), 'warn');
      return;
    }
    setBusy('captcha', true);
    try {
      const res = await apiPost<{ captcha_id?: string; image_code_index?: string; image?: string }>(
        '/api/cf/captcha',
        { server_url: serverUrl, proxy }
      );
      setCaptcha({
        captcha_id: res.captcha_id || '',
        image_code_index: res.image_code_index || '',
        image: res.image || '',
      });
      setImageCode('');
      setStatus({ text: '', cls: '' });
    } catch (e: any) {
      setCaptcha({ captcha_id: '', image_code_index: '', image: '' });
      setStatus({ text: `获取验证码失败：${e.message}`, cls: 'error' });
    } finally {
      setBusy('captcha', false);
    }
  };

  const login = async () => {
    const serverUrl = cfg.server_url.trim();
    const mobile = cfg.username.trim();
    const password = cfg.password.trim();
    const proxy = cfg.proxy.trim();
    if (!serverUrl || !mobile || !password) {
      setStatus({ text: t('cf.fillServerMobilePwd'), cls: 'error' });
      return;
    }
    if (imageCode && !captcha.captcha_id) {
      setStatus({ text: t('cf.refreshCaptchaFirst'), cls: 'error' });
      return;
    }
    setBusy('login', true);
    setStatus({ text: t('cf.loggingIn'), cls: '' });
    try {
      const res = await apiPost<{
        token?: string;
        ok?: boolean;
        message?: string;
        need_img_valid?: boolean;
      }>('/api/cf/login', {
        server_url: serverUrl,
        mobile,
        password,
        proxy,
        image_code: imageCode,
        image_code_index: captcha.image_code_index,
        captcha_id: captcha.captcha_id,
      });
      if (res.token) {
        setCfg((c) => ({ ...c, token: res.token || '' }));
        setCaptcha({ captcha_id: '', image_code_index: '', image: '' });
        setImageCode('');
        saveCfg({ token: res.token || '' });
        setStatus({ text: t('cf.loginSuccess'), cls: 'success' });
        return;
      }
      if (res && res.ok === false) {
        if (res.need_img_valid) {
          setStatus({ text: `${res.message || '登录失败'}（需要图片验证码，请输入后重新登录）`, cls: 'error' });
          try { await fetchCaptcha(); } catch { /* ignore */ }
        } else {
          setStatus({ text: `登录失败：${res.message || ''}`, cls: 'error' });
        }
        return;
      }
      throw new Error('未获取到 token');
    } catch (ex: any) {
      setStatus({ text: `登录失败：${ex.message}`, cls: 'error' });
      if (captcha.captcha_id) {
        try { await fetchCaptcha(); } catch { /* ignore */ }
      }
    } finally {
      setBusy('login', false);
    }
  };

  const autoGetTokens = async () => {
    setAutoLogin({ running: true, msg: t('cf.autoGetting') });
    try {
      const res = await apiPost<{ total?: number; success?: number; results?: any[] }>(
        '/api/cf/auto-login',
        { proxy: cfg.proxy.trim() }
      );
      const map = await loadTokens();
      const su = cfg.server_url.trim();
      if (su && map[su]?.has_token) {
        addToast(`已自动获取「${map[su].name}」的 Token`, 'success');
      }
      setStatus({
        text: `自动获取完成：${res.success ?? 0}/${res.total ?? 0} 个账号成功（需验证码的请手动登录）`,
        cls: (res.success ?? 0) > 0 ? 'success' : 'warning',
      });
    } catch (e: any) {
      setStatus({ text: `自动获取失败：${e.message}`, cls: 'error' });
    } finally {
      setAutoLogin({ running: false, msg: '' });
    }
  };

  const ensureAllLogs = useCallback(async (base: CfLastResult) => {
    const total = base.total || 0;
    if (total === 0 || base.rows.length >= total) return;
    const token = cfgRef.current.token;
    const proxy = cfgRef.current.proxy.trim();
    const fetchSize = Math.max(base.page_size || 200, 1000);
    let nextPage = Math.floor(base.rows.length / fetchSize) + 1;
    if (nextPage < 2) nextPage = 2;
    try {
      while (base.rows.length < total && nextPage <= 500) {
        setStatus({ text: `正在加载全部日志用于排序…（${base.rows.length}/${total}）`, cls: '' });
        const res = await apiPost<any>('/api/cf/logs', {
          server_url: base.server_url,
          token,
          log_type: base.log_type,
          page_index: nextPage,
          page_size: fetchSize,
          proxy,
        });
        const payload = res.data || res.result || res;
        const pageRows =
          payload.list || payload.data || payload.items || res.list || res.data || [];
        if (!pageRows.length) break;
        base.rows = base.rows.concat(pageRows);
        setResult({ ...base });
        nextPage += 1;
      }
    } catch (e: any) {
      setStatus({ text: `加载全部日志失败：${e.message}（已对当前已加载 ${base.rows.length} 条排序）`, cls: 'error' });
    }
  }, []);

  const queryLogs = async () => {
    const serverUrl = cfg.server_url.trim();
    const token = cfg.token.trim();
    const logType = cfg.log_type.trim();
    const pageSize = cfg.page_size || 200;
    const pageIndex = cfg.page_index || 1;
    const proxy = cfg.proxy.trim();
    const cached = tokenMap[cfg.server_url.trim()];
    if (!token && !(cached && cached.has_token)) {
      setStatus({ text: t('cf.tokenFirst'), cls: 'error' });
      return;
    }
    setBusy('query', true);
    setStatus({ text: proxy ? `正在查询（代理：${proxy}）…` : '正在查询（直连）…', cls: '' });
    try {
      const res = await apiPost<any>('/api/cf/logs', {
        server_url: serverUrl,
        token,
        log_type: logType,
        page_index: pageIndex,
        page_size: pageSize,
        proxy,
      });
      const payload = res.data || res.result || res;
      const rows =
        payload.list || payload.data || payload.items || res.list || res.data || [];
      const total =
        payload.total ?? payload.count ?? payload.row_count ?? res.total ?? rows.length;

      const base: CfLastResult = {
        server_url: serverUrl,
        log_type: logType,
        auth_method: res.method || '',
        page_index: pageIndex,
        page_size: pageSize,
        total,
        rows,
        raw: res,
        localPage: 1,
      };
      setResult(base);
      setExpanded(null);
      setSearch('');
      setStatus({ text: `查询成功，共 ${total} 条`, cls: 'success' });
      try {
        await ensureAllLogs(base);
      } catch {
        /* 拉取失败时降级：对已加载的数据排序并提示 */
      }
    } catch (ex: any) {
      setStatus({ text: `查询失败：${ex.message}`, cls: 'error' });
      setResult(null);
    } finally {
      setBusy('query', false);
    }
  };

  const exportLogs = async () => {
    const r = resultRef.current;
    if (!r || !r.rows || !r.rows.length) {
      setStatus({ text: t('cf.exportNeedData'), cls: 'error' });
      return;
    }
    setBusy('export', true);
    try {
      const res = await apiPost<{ path?: string; count?: number }>('/api/cf/logs/export', {
        server_url: r.server_url,
        log_type: r.log_type,
        auth_method: r.auth_method,
        page_index: r.page_index,
        page_size: r.page_size,
        total: r.total,
        rows: r.rows,
        raw: r.raw,
      });
      if (res.path) {
        let copied = false;
        try {
          await writeClipboardText(res.path);
          copied = true;
        } catch {
          /* ignore */
        }
        setStatus({
          text: copied
            ? `已导出 ${res.count} 条，文件路径已复制到剪贴板：${res.path}`
            : `已导出 ${res.count} 条 → ${res.path}（复制到剪贴板失败，请手动复制路径）`,
          cls: 'success',
        });
        pushLog(`CF 日志导出路径: ${res.path}`);
      } else {
        throw new Error('未返回文件路径');
      }
    } catch (e: any) {
      setStatus({ text: `导出失败：${e.message}`, cls: 'error' });
    } finally {
      setBusy('export', false);
    }
  };

  const clipboardSave = async () => {
    let text = '';
    try {
      text = await readClipboardText();
    } catch (e: any) {
      setStatus({ text: `读取剪贴板失败：${e.message}（请先复制文本，并点击本窗口使其获得焦点，再重试）`, cls: 'error' });
      return;
    }
    if (!text || !text.trim()) {
      setStatus({ text: '剪贴板内容为空，请先复制一些文本再点击', cls: 'error' });
      return;
    }
    setBusy('clipboard', true);
    setStatus({ text: '正在保存剪贴板内容到文件…', cls: '' });
    try {
      const res = await apiPost<{ path?: string; size?: number }>('/api/cf/clipboard-save', { text });
      if (res.path) {
        let copied = false;
        try {
          await writeClipboardText(res.path);
          copied = true;
        } catch {
          /* ignore */
        }
        setStatus({
          text: `已保存剪贴板内容（${res.size} 字符）→ ${res.path}${copied ? '（路径已复制到剪贴板）' : ''}`,
          cls: 'success',
        });
        pushLog(`剪贴板转文件成功：${res.path}`);
      } else {
        throw new Error('未返回文件路径');
      }
    } catch (ex: any) {
      setStatus({ text: `保存失败：${ex.message}`, cls: 'error' });
    } finally {
      setBusy('clipboard', false);
    }
  };

  const toggleSort = async () => {
    const next = sortDir === 'asc' ? 'desc' : 'asc';
    setSortDir(next);
    setBusy('sort', true);
    try {
      if (resultRef.current) await ensureAllLogs(resultRef.current);
    } finally {
      setBusy('sort', false);
    }
  };

  // 排序 + 客户端实时过滤 + 本地分页
  const view = useMemo(() => {
    if (!result) return { rows: [] as CfLogsRow[], isFull: false, all: 0, total: 0, totalPages: 1, localPage: 1 };
    const all = result.rows.slice();
    const isFull = (result.total || 0) > 0 && result.rows.length >= result.total;
    all.sort((a, b) => {
      const ta = cfTime(a);
      const tb = cfTime(b);
      const cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });
    const q = search.trim();
    let filtered = all;
    if (q) {
      const needle = caseSensitive ? q : q.toLowerCase();
      filtered = all.filter((r) => {
        const content = caseSensitive ? cfContent(r) : cfContent(r).toLowerCase();
        const time = caseSensitive ? cfTime(r) : cfTime(r).toLowerCase();
        const type = caseSensitive ? cfLogType(r, result.log_type) : cfLogType(r, result.log_type).toLowerCase();
        return content.includes(needle) || time.includes(needle) || type.includes(needle);
      });
    }
    const pageSize = result.page_size || 200;
    let display = filtered;
    let localPage = result.localPage || 1;
    let totalPages = 1;
    if (isFull && filtered.length > pageSize) {
      totalPages = Math.ceil(filtered.length / pageSize);
      if (localPage > totalPages) localPage = totalPages;
      display = filtered.slice((localPage - 1) * pageSize, localPage * pageSize);
    }
    return { rows: display, isFull, all: filtered.length, total: result.total, totalPages, localPage };
  }, [result, sortDir, search, caseSensitive]);

  const goLocalPage = (p: number) => {
    if (result) setResult({ ...result, localPage: p });
  };

  return (
    <div className="cf-panel">
      {/* ===== 配置卡片：标题 + 环境切换 + 可折叠配置体 ===== */}
      <div className="card-soft cf-cfg-card">
        <div className="panel-header">
          <h2 className="section-title">{t('cf.title')}</h2>
          <div className="cf-env-switcher">
            <select
              className="sel"
              value={env}
              onChange={(e) => switchEnv(e.target.value)}
            >
              <option value="">{t('cf.selectEnv')}</option>
              {accounts.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
              <option value="custom">{t('cf.custom')}</option>
            </select>
          </div>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setCfgOpen((v) => !v)}
            title={t('cf.toggleConfig')}
          >
            {t('cf.config')} {cfgOpen ? '▾' : '▸'}
          </button>
        </div>

        {cfgOpen && (
          <div className="cf-cfg-body">
            <div className="cf-cfg-row">
              <div className="cf-cfg-field">
                <label>{t('cf.serverUrl')}</label>
                <input
                  className="input"
                  placeholder={t('cf.serverUrlPlaceholder')}
                  value={cfg.server_url}
                  onChange={(e) => setCfg({ ...cfg, server_url: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
            </div>
            <div className="cf-cfg-row">
              <div className="cf-cfg-field">
                <label>{t('cf.mobile')}</label>
                <input
                  className="input"
                  placeholder={t('cf.mobilePlaceholder')}
                  value={cfg.username}
                  onChange={(e) => setCfg({ ...cfg, username: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
              <div className="cf-cfg-field">
                <label>{t('cf.password')}</label>
                <input
                  className="input"
                  type="password"
                  placeholder={t('cf.passwordPlaceholder')}
                  value={cfg.password}
                  onChange={(e) => setCfg({ ...cfg, password: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
              <div className="cf-cfg-field cf-cfg-field--token">
                <label>{t('cf.token')}</label>
                <input
                  className="input"
                  placeholder={t('cf.tokenPlaceholder')}
                  value={cfg.token}
                  onChange={(e) => setCfg({ ...cfg, token: e.target.value })}
                  onBlur={() => saveCfg()}
                />
                {(() => {
                  const tk = tokenMap[cfg.server_url.trim()];
                  if (!tk) return null;
                  if (tk.has_token)
                    return <span className="cf-token-hint ok">✓ {t('cf.tokenCached')}</span>;
                  if (tk.need_captcha)
                    return <span className="cf-token-hint warn">{t('cf.tokenNeedCaptcha')}</span>;
                  if (tk.last_error)
                    return <span className="cf-token-hint err">{t('cf.tokenLoginFailed')}：{tk.last_error}</span>;
                  return null;
                })()}
              </div>
            </div>
            <div className="cf-cfg-row">
              <div className="cf-cfg-field cf-cfg-field--full">
                <label>{t('cf.proxy')}</label>
                <input
                  className="input"
                  placeholder="http://127.0.0.1:7890"
                  value={cfg.proxy}
                  onChange={(e) => setCfg({ ...cfg, proxy: e.target.value })}
                  onBlur={() => saveCfg()}
                />
              </div>
            </div>
            <div className="cf-cfg-row cf-captcha-row">
              <div className="cf-cfg-field cf-cfg-field--captcha">
                <label>{t('cf.captcha')}</label>
                <div className="cf-captcha-img-wrap">
                  {captcha.image ? (
                    <img
                      className="cf-captcha-img"
                      src={captcha.image}
                      alt={t('cf.captcha')}
                      title={t('cf.refresh')}
                      onClick={fetchCaptcha}
                    />
                  ) : (
                    <div
                      className="cf-captcha-img empty"
                      onClick={fetchCaptcha}
                      title={t('cf.getCaptcha')}
                    >
                      {t('cf.getCaptcha')}
                    </div>
                  )}
                  <button
                    className="btn btn-ghost btn-xs"
                    onClick={fetchCaptcha}
                    disabled={loading.captcha}
                  >
                    🔄 {t('cf.refresh')}
                  </button>
                </div>
              </div>
              <div className="cf-cfg-field">
                <label>{t('cf.captchaInput')}</label>
                <input
                  className="input"
                  placeholder={t('cf.captchaInputPlaceholder')}
                  maxLength={8}
                  autoComplete="off"
                  value={imageCode}
                  onChange={(e) => setImageCode(e.target.value)}
                />
              </div>
            </div>
            <div className="cf-cfg-actions">
              <button
                className="btn btn-sm btn-primary"
                onClick={login}
                disabled={loading.login}
              >
                {loading.login ? t('cf.loggingInShort') : t('cf.loginToken')}
              </button>
              <button
                className="btn btn-sm"
                onClick={autoGetTokens}
                disabled={autoLogin.running}
                title={t('cf.autoGetTokenHint')}
              >
                {autoLogin.running ? `⏳ ${t('cf.autoGetting')}` : `🔑 ${t('cf.autoGetToken')}`}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ===== 查询卡片 ===== */}
      <div className="card-soft cf-query-card">
        <div className="cf-query-row">
          <div className="cf-cfg-field cf-cfg-field--main">
            <label>{t('cf.logType')}</label>
            <input
              className="input"
              placeholder="salary_seal_delay_payment_vvv1"
              value={cfg.log_type}
              onChange={(e) => setCfg({ ...cfg, log_type: e.target.value })}
              onBlur={() => saveCfg()}
              onKeyDown={(e) => e.key === 'Enter' && queryLogs()}
            />
          </div>
          <div className="cf-cfg-field cf-cfg-field--w100">
            <label>{t('cf.pageSize')}</label>
            <input
              className="input input-sm"
              type="number"
              min={1}
              max={12000}
              value={cfg.page_size}
              onChange={(e) => setCfg({ ...cfg, page_size: parseInt(e.target.value) || 200 })}
              onBlur={() => saveCfg()}
            />
          </div>
          <div className="cf-cfg-field cf-cfg-field--w80">
            <label>{t('cf.pageIndex')}</label>
            <input
              className="input input-sm"
              type="number"
              min={1}
              value={cfg.page_index}
              onChange={(e) => setCfg({ ...cfg, page_index: parseInt(e.target.value) || 1 })}
              onBlur={() => saveCfg()}
            />
          </div>
          <button
            className="btn btn-primary cf-query-btn"
            onClick={queryLogs}
            disabled={loading.query}
          >
            {loading.query ? t('cf.querying') : t('cf.query')}
          </button>
          <button
            className="btn cf-query-btn"
            onClick={exportLogs}
            disabled={loading.export || !result?.rows.length}
          >
            {t('cf.export')}
          </button>
          <button
            className="btn btn-ghost cf-query-btn"
            onClick={clipboardSave}
            disabled={loading.clipboard}
          >
            📋 {t('cf.clipboardToFile')}
          </button>
        </div>
        {status.text && <div className={`cf-query-status ${status.cls}`}>{status.text}</div>}
      </div>

      {/* ===== 日志搜索 / 过滤工具栏 ===== */}
      {result && (
        <div className="cf-search-bar card-soft">
          <input
            className="input cf-search-input"
            placeholder={t('cf.searchPlaceholder')}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              if (resultRef.current) setResult({ ...resultRef.current, localPage: 1 });
            }}
          />
          <label className="cf-search-case" title={t('cf.caseSensitive')}>
            <input
              type="checkbox"
              checked={caseSensitive}
              onChange={(e) => setCaseSensitive(e.target.checked)}
            />{' '}
            {t('cf.caseSensitive')}
          </label>
          <button
            className="btn btn-sm btn-ghost cf-btn-sort-time"
            onClick={toggleSort}
            disabled={loading.sort}
          >
            {t('cf.time')} {sortDir === 'asc' ? '↑' : '↓'}
          </button>
          <span className="cf-search-count">
            {search
              ? `匹配 ${view.all} / ${view.isFull ? '全部' : '本页'} ${result.rows.length}`
              : view.isFull
              ? `共 ${view.all} 条`
              : `本页 ${view.all} / 共 ${result.total} 条`}
          </span>
        </div>
      )}

      {!result && <div className="empty-hint">{t('cf.startHint')}</div>}

      {result && view.rows.length === 0 && (
        <div className="empty-hint">{t('cf.noMatch')}</div>
      )}

      {/* ===== 日志结果表 ===== */}
      {result && view.rows.length > 0 && (
        <div className="cf-results">
          <div className="cf-result-meta">
            <span className="cf-result-count">
              {search
                ? `匹配 ${view.all} 条`
                : view.isFull
                ? `共 ${view.all} 条`
                : `本页 ${view.all} / 共 ${result.total} 条`}
            </span>
            {view.isFull && <span>{t('cf.fullLoaded')}</span>}
          </div>
          <div className="table-scroll">
            <table className="cf-log-table">
              <thead>
                <tr>
                  <th style={{ width: 48 }}>#</th>
                  <th style={{ width: 150 }}>{t('cf.colType')}</th>
                  <th style={{ width: 170 }}>{t('cf.colTime')}</th>
                  <th>{t('cf.colContent')}</th>
                </tr>
              </thead>
              <tbody>
                {view.rows.map((row, i) => {
                  const createTime = cfTime(row);
                  const content = cfContent(row);
                  const contentFull = cfContentFull(row);
                  const logTypeVal = cfLogType(row, result.log_type);
                  const globalIdx = result.rows.length - view.rows.length + i;
                  return (
                    <FragmentRow
                      key={globalIdx}
                      idx={i + 1}
                      type={logTypeVal}
                      time={createTime}
                      content={content}
                      contentFull={contentFull}
                      rowId={
                        row.id != null
                          ? String(row.id)
                          : row._id != null
                          ? String(row._id)
                          : ''
                      }
                      expanded={expanded === globalIdx}
                      onToggle={() => setExpanded(expanded === globalIdx ? null : globalIdx)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
          {view.isFull && view.totalPages > 1 && (
            <div className="cf-pagination">
              {view.localPage > 1 && (
                <button className="btn btn-sm" onClick={() => goLocalPage(view.localPage - 1)}>
                  {t('cf.prevPage')}
                </button>
              )}
              <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                {view.localPage} / {view.totalPages}（本地，第 1 页为最新）
              </span>
              {view.localPage < view.totalPages && (
                <button className="btn btn-sm" onClick={() => goLocalPage(view.localPage + 1)}>
                  {t('cf.nextPage')}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FragmentRow(props: {
  idx: number;
  type: string;
  time: string;
  content: string;
  contentFull: string;
  rowId: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { idx, type, time, content, contentFull, rowId, expanded, onToggle } = props;
  return (
    <>
      <tr className="cf-log-row" onClick={onToggle} style={{ cursor: 'pointer' }}>
        <td>{idx}</td>
        <td className="cf-log-type" title={type}>
          {type}
        </td>
        <td className="cf-log-time">{time}</td>
        <td className="cf-log-content">{content}</td>
      </tr>
      {expanded && (
        <tr className="cf-log-detail-row">
          <td colSpan={4}>
            <div className="cf-log-meta">
              类型：{type} ｜ ID：{rowId} ｜ 时间：{time}
            </div>
            <div className="cf-log-content-full">{contentFull}</div>
          </td>
        </tr>
      )}
    </>
  );
}
