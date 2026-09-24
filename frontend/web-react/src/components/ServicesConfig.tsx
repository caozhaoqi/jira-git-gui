import { useCallback, useEffect, useRef, useState } from 'react';
import { api, apiGet } from '../api/client';

/* ============================================================
   独立全屏「首选项」页 —— 迁移自 web/services-config.html

   能力对齐原生：云函数 / HCM 服务账号的增删改查、HCM 代理配置
   （base_url / token / hosts 白名单）、Jira 系统配置（PAT / Cookie）。

   与原生页的差异（有意为之）：
   - React 节点渲染，不再手拼 innerHTML；
   - 复用统一 API 客户端（api / apiGet），错误分类与主界面一致；
   - 主题复用同一 localStorage `jgg-theme`，启动时跟随 app 当前主题；
     页面本身不提供主题/返回按钮（独立窗口场景无意义），
     与主界面天然一致——这正是迁移的原因（原页固定浅色且要重建
     冻结后端才能更新）。
   ============================================================ */

interface CfItem {
  index: number;
  name: string;
  type: '云函数' | 'HCM';
  server_url: string;
  username: string;
  has_password: boolean;
}
interface CfResp {
  items: CfItem[];
  path?: string;
}
interface HcmResp {
  base_url?: string;
  hosts?: string[];
  path?: string;
  has_token?: boolean;
}
interface JiraResp {
  jira_url?: string;
  username?: string;
  mode?: string;
  path?: string;
  has_pat?: boolean;
  has_cookie?: boolean;
}

type TabKey = 'cf' | 'hcm' | 'jira';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'cf', label: '云函数 / HCM 服务' },
  { key: 'hcm', label: 'HCM 代理' },
  { key: 'jira', label: 'Jira 配置' },
];

export function ServicesConfig() {
  // 主题跟随 app（同一 localStorage `jgg-theme`）：本视图不引 useAppStore，
  // 需自行把 html.dark 类挂上（与 store 的 applyThemeClass 同一套约定）。
  useEffect(() => {
    let dark = false;
    try {
      dark = localStorage.getItem('jgg-theme') === 'dark';
    } catch { /* ignore */ }
    document.documentElement.classList.toggle('dark', dark);
  }, []);

  const [tab, setTab] = useState<TabKey>('cf');
  const [toast, setToast] = useState<{ msg: string; kind: 'ok' | 'err' } | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);

  const showToast = useCallback((msg: string, kind: 'ok' | 'err' = 'ok') => {
    setToast({ msg, kind });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 2200);
  }, []);

  /* ---------------- 云函数 / HCM 服务账号 ---------------- */
  const [cfItems, setCfItems] = useState<CfItem[]>([]);
  const [cfPath, setCfPath] = useState('');
  const [cfEdit, setCfEdit] = useState<number | null>(null);
  const [cf, setCf] = useState({ name: '', type: '云函数', server_url: '', username: '', password: '' });

  const loadCf = useCallback(async () => {
    try {
      const d = await apiGet<CfResp>('/api/services/cloud-functions');
      setCfItems(d.items || []);
      setCfPath(d.path || '');
    } catch (e: any) {
      showToast('加载失败：' + (e?.message || e), 'err');
    }
  }, [showToast]);

  const resetCfForm = useCallback(() => {
    setCfEdit(null);
    setCf({ name: '', type: '云函数', server_url: '', username: '', password: '' });
  }, []);

  const editCf = useCallback(
    (idx: number) => {
      const it = cfItems.find((x) => x.index === idx);
      if (!it) return;
      setCfEdit(idx);
      setCf({ name: it.name, type: it.type, server_url: it.server_url, username: it.username, password: '' });
    },
    [cfItems]
  );

  const saveCf = useCallback(async () => {
    const payload = {
      name: cf.name.trim(),
      type: cf.type,
      server_url: cf.server_url.trim(),
      username: cf.username.trim(),
      password: cf.password,
    };
    if (!payload.name && !payload.server_url) {
      showToast('名称与服务器地址至少填一项', 'err');
      return;
    }
    try {
      if (cfEdit === null) {
        await api('/api/services/cloud-functions', { method: 'POST', body: JSON.stringify(payload) });
        showToast('已添加服务');
      } else {
        await api(`/api/services/cloud-functions/${cfEdit}`, { method: 'PUT', body: JSON.stringify(payload) });
        showToast(`已更新服务 #${cfEdit}`);
      }
      resetCfForm();
      loadCf();
    } catch (e: any) {
      showToast('保存失败：' + (e?.message || e), 'err');
    }
  }, [cf, cfEdit, resetCfForm, loadCf, showToast]);

  const delCf = useCallback(
    async (idx: number) => {
      if (!window.confirm('确定删除该服务配置？')) return;
      try {
        await api(`/api/services/cloud-functions/${idx}`, { method: 'DELETE' });
        showToast('已删除');
        loadCf();
      } catch (e: any) {
        showToast('删除失败：' + (e?.message || e), 'err');
      }
    },
    [loadCf, showToast]
  );

  /* ---------------- HCM 代理配置 ---------------- */
  const [hcmPath, setHcmPath] = useState('');
  const [hcmHasToken, setHcmHasToken] = useState(false);
  const [hcm, setHcm] = useState({ base_url: '', token: '', hosts: '' });

  const loadHcm = useCallback(async () => {
    try {
      const d = await apiGet<HcmResp>('/api/services/hcm-config');
      setHcm({ base_url: d.base_url || '', token: '', hosts: (d.hosts || []).join('\n') });
      setHcmPath(d.path || '');
      setHcmHasToken(!!d.has_token);
    } catch (e: any) {
      showToast('加载失败：' + (e?.message || e), 'err');
    }
  }, [showToast]);

  const saveHcm = useCallback(async () => {
    const payload = {
      base_url: hcm.base_url.trim(),
      token: hcm.token,
      hosts: hcm.hosts.split('\n').map((s) => s.trim()).filter(Boolean),
    };
    try {
      await api('/api/services/hcm-config', { method: 'POST', body: JSON.stringify(payload) });
      showToast('HCM 配置已保存并即时生效');
      loadHcm();
    } catch (e: any) {
      showToast('保存失败：' + (e?.message || e), 'err');
    }
  }, [hcm, loadHcm, showToast]);

  /* ---------------- Jira 系统配置 ---------------- */
  const [jiraPath, setJiraPath] = useState('');
  const [jiraState, setJiraState] = useState({ has_pat: false, has_cookie: false });
  const [jira, setJira] = useState({ jira_url: '', username: '', mode: 'pat', pat: '', cookie: '' });

  const loadJira = useCallback(async () => {
    try {
      const d = await apiGet<JiraResp>('/api/services/jira-config');
      setJira({
        jira_url: d.jira_url || '',
        username: d.username || '',
        mode: d.mode === 'cookie' ? 'cookie' : 'pat',
        pat: '',
        cookie: '',
      });
      setJiraPath(d.path || '');
      setJiraState({ has_pat: !!d.has_pat, has_cookie: !!d.has_cookie });
    } catch (e: any) {
      showToast('加载失败：' + (e?.message || e), 'err');
    }
  }, [showToast]);

  const saveJira = useCallback(async () => {
    const payload = {
      jira_url: jira.jira_url.trim(),
      username: jira.username.trim(),
      mode: jira.mode,
      pat: jira.pat,
      cookie: jira.cookie.trim(),
    };
    try {
      await api('/api/services/jira-config', { method: 'POST', body: JSON.stringify(payload) });
      showToast('Jira 配置已保存并即时生效');
      loadJira();
    } catch (e: any) {
      showToast('保存失败：' + (e?.message || e), 'err');
    }
  }, [jira, loadJira, showToast]);

  /* 首次进入按当前页签加载；切页签时懒加载对应配置 */
  useEffect(() => {
    if (tab === 'cf') loadCf();
    else if (tab === 'hcm') loadHcm();
    else loadJira();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  return (
    <div className="svc">
      <header className="svc-head">
        <span className="svc-title">首选项 · 系统配置</span>
        <div className="svc-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={'svc-tab' + (tab === t.key ? ' active' : '')}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </header>

      <main className="svc-body">
        {tab === 'cf' && (
          <section className="svc-panel card-soft">
            <div className="table-scroll">
              {cfItems.length === 0 ? (
                <div className="empty-hint">暂无服务配置，请在下方添加。</div>
              ) : (
                <table className="svc-table">
                  <thead>
                    <tr>
                      <th>名称</th><th>类型</th><th>服务器地址</th><th>账号</th><th>密码</th><th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cfItems.map((it) => (
                      <tr key={it.index}>
                        <td>{it.name}</td>
                        <td>
                          {it.type === 'HCM' ? <span className="chip chip-hcm">HCM</span> : <span className="chip">云函数</span>}
                        </td>
                        <td><code>{it.server_url}</code></td>
                        <td>{it.username}</td>
                        <td>
                          {it.has_password
                            ? <span className="svc-ok">已设置</span>
                            : <span className="svc-muted">未设置</span>}
                        </td>
                        <td>
                          <div className="svc-actions">
                            <button className="btn btn-ghost btn-sm" onClick={() => editCf(it.index)}>编辑</button>
                            <button className="btn btn-danger btn-sm" onClick={() => delCf(it.index)}>删除</button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <p className="svc-meta">配置文件：{cfPath || '—'}</p>

            <div className="svc-add">
              <h3>{cfEdit === null ? '添加服务' : `编辑服务 #${cfEdit}`}</h3>
              <p className="svc-hint">服务器地址填 IP 或域名（含 http:// 或 https://）。密码留空表示不修改（编辑时）。</p>
              <div className="svc-form">
                <label className="svc-field">
                  <span>名称 / 备注</span>
                  <input className="input" value={cf.name} placeholder="如：21qor-公有云 黑龙江农信社"
                    onChange={(e) => setCf({ ...cf, name: e.target.value })} />
                </label>
                <label className="svc-field">
                  <span>类型</span>
                  <select className="sel" value={cf.type} onChange={(e) => setCf({ ...cf, type: e.target.value })}>
                    <option value="云函数">云函数</option>
                    <option value="HCM">HCM 对象</option>
                  </select>
                </label>
                <label className="svc-field svc-full">
                  <span>服务器地址（IP / 域名）</span>
                  <input className="input" value={cf.server_url} placeholder="https://21qor.hcmcloud.cn 或 http://10.1.38.184"
                    onChange={(e) => setCf({ ...cf, server_url: e.target.value })} />
                </label>
                <label className="svc-field">
                  <span>账号 / 用户名</span>
                  <input className="input" value={cf.username} placeholder="如 16602629614"
                    onChange={(e) => setCf({ ...cf, username: e.target.value })} />
                </label>
                <label className="svc-field">
                  <span>密码</span>
                  <input className="input" type="password" autoComplete="new-password" value={cf.password}
                    placeholder="留空表示不修改" onChange={(e) => setCf({ ...cf, password: e.target.value })} />
                </label>
              </div>
              <div className="svc-bar">
                <button className="btn btn-primary" onClick={saveCf}>保存</button>
                {cfEdit !== null && <button className="btn btn-ghost" onClick={resetCfForm}>取消编辑</button>}
              </div>
            </div>
          </section>
        )}

        {tab === 'hcm' && (
          <section className="svc-panel card-soft">
            <div className="svc-form">
              <label className="svc-field svc-full">
                <span>代理目标网关 base_url（IP / 域名）</span>
                <input className="input" value={hcm.base_url} placeholder="http://73.2.3.27"
                  onChange={(e) => setHcm({ ...hcm, base_url: e.target.value })} />
              </label>
              <label className="svc-field svc-full">
                <span>登录态 Token（Cookie 中的 token 值，留空表示不修改）</span>
                <input className="input" type="password" autoComplete="new-password" value={hcm.token}
                  placeholder="留空则不修改现有 token" onChange={(e) => setHcm({ ...hcm, token: e.target.value })} />
              </label>
              <label className="svc-field svc-full">
                <span>平台域名白名单 hosts（每行一个）</span>
                <textarea className="input" rows={4} value={hcm.hosts}
                  placeholder={'21qor.hcmcloud.cn\nhcm.ptacn.com'}
                  onChange={(e) => setHcm({ ...hcm, hosts: e.target.value })} />
              </label>
            </div>
            <div className="svc-bar">
              <button className="btn btn-primary" onClick={saveHcm}>保存 HCM 配置</button>
            </div>
            <p className="svc-meta">
              配置文件：{hcmPath || '—'} ｜ Token：{hcmHasToken ? '已设置' : '未设置'}
            </p>
          </section>
        )}

        {tab === 'jira' && (
          <section className="svc-panel card-soft">
            <div className="svc-form">
              <label className="svc-field svc-full">
                <span>Jira 地址（URL）</span>
                <input className="input" value={jira.jira_url} placeholder="https://jira.example.com"
                  onChange={(e) => setJira({ ...jira, jira_url: e.target.value })} />
              </label>
              <label className="svc-field">
                <span>账号 / 用户名</span>
                <input className="input" value={jira.username} placeholder="如 16602629614"
                  onChange={(e) => setJira({ ...jira, username: e.target.value })} />
              </label>
              <label className="svc-field">
                <span>认证方式</span>
                <select className="sel" value={jira.mode} onChange={(e) => setJira({ ...jira, mode: e.target.value })}>
                  <option value="pat">个人访问令牌 (PAT)</option>
                  <option value="cookie">会话 Cookie</option>
                </select>
              </label>
              <label className="svc-field svc-full">
                <span>个人访问令牌 PAT（留空表示不修改）</span>
                <input className="input" type="password" autoComplete="new-password" value={jira.pat}
                  placeholder="留空则不修改现有令牌" onChange={(e) => setJira({ ...jira, pat: e.target.value })} />
              </label>
              <label className="svc-field svc-full">
                <span>会话 Cookie（留空表示不修改）</span>
                <textarea className="input" rows={3} value={jira.cookie}
                  placeholder="留空则不修改现有 Cookie，如 JSESSIONID=...; atlassian.xsrf.token=..."
                  onChange={(e) => setJira({ ...jira, cookie: e.target.value })} />
              </label>
            </div>
            <div className="svc-bar">
              <button className="btn btn-primary" onClick={saveJira}>保存 Jira 配置</button>
            </div>
            <p className="svc-meta">
              配置文件：{jiraPath || '—'} ｜ PAT：{jiraState.has_pat ? '已设置' : '未设置'} ｜ Cookie：{jiraState.has_cookie ? '已设置' : '未设置'}
            </p>
          </section>
        )}
      </main>

      {toast && <div className={'svc-toast' + (toast.kind === 'err' ? ' err' : '')}>{toast.msg}</div>}
    </div>
  );
}
