/* ============================================================
   云函数日志（CF）
   CF 账号/环境配置、登录、日志查询/导出、剪贴板转文件、结果渲染。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
// ===== CF 云函数日志 =====
const CF_CFG_KEY = 'jgg-cf-cfg';
// 预置环境（可直接切换，地址/账号密码用户填全）
// CF 账号列表：运行时从后端 /api/cf/accounts 加载。
// 来源为本地配置文件 cf_accounts.local.json（已被 .gitignore 忽略，含真实
// 账号密码，绝不进 git）。前端仅用于自动填充，密码不会写回任何仓库。
let CF_ACCOUNTS = [];

async function loadCfAccounts() {
  try {
    const data = await api('/api/cf/accounts');
    CF_ACCOUNTS = Array.isArray(data.accounts) ? data.accounts : [];
  } catch (e) {
    CF_ACCOUNTS = [];
  }
  const sel = document.getElementById('cf-env-select');
  if (!sel) return;
  // 强制清掉除「选择环境…」和「自定义」以外的所有 option（包括旧 HTML 缓存里的
  // 硬编码占位项），保证下拉框只反映本地配置文件 + 自定义入口。
  const PRESERVED = new Set(['', 'custom']);
  Array.from(sel.options).forEach(o => {
    if (!PRESERVED.has(o.value)) o.remove();
  });
  // 然后注入本地账号配置文件的选项
  CF_ACCOUNTS.forEach(acc => {
    const opt = document.createElement('option');
    opt.value = acc.name;
    opt.textContent = acc.name;
    opt.dataset.dyn = '1';
    sel.insertBefore(opt, sel.querySelector('option[value="custom"]'));
  });
  // 自动匹配当前已填写的服务器地址
  const curUrl = (document.getElementById('cf-server-url').value || '').trim();
  if (curUrl) {
    const m = CF_ACCOUNTS.find(a => a.server_url && curUrl === a.server_url);
    if (m) sel.value = m.name;
  }
  // 给运维/排查一个明显信号
  if (CF_ACCOUNTS.length === 0) {
    console.warn('[CF] 本地 cf_accounts.local.json 未读到任何账号，下拉框只有「自定义」可选');
  } else {
    console.info(`[CF] 已从本地配置加载 ${CF_ACCOUNTS.length} 个账号：${CF_ACCOUNTS.map(a => a.name).join('、')}`);
  }
}

function switchCfEnv(key) {
  if (!key || key === 'custom') return;
  const acc = CF_ACCOUNTS.find(a => a.name === key);
  if (!acc) return;
  document.getElementById('cf-server-url').value = acc.server_url || '';
  document.getElementById('cf-username').value = acc.username || '';
  document.getElementById('cf-password').value = acc.password || '';
  toast(`已切换到「${acc.name}」环境，账号密码已预填`, 'info');
  saveCfCfg();
}

function loadCfCfg() {
  try {
    const cfg = JSON.parse(localStorage.getItem(CF_CFG_KEY) || '{}');
    document.getElementById('cf-server-url').value = cfg.server_url || '';
    document.getElementById('cf-username').value = cfg.mobile || cfg.username || '';
    document.getElementById('cf-password').value = cfg.password || '';
    document.getElementById('cf-token').value = cfg.token || '';
    document.getElementById('cf-proxy').value = cfg.proxy || '';
    document.getElementById('cf-log-type').value = cfg.log_type || '';
    document.getElementById('cf-page-size').value = cfg.page_size || 200;
    document.getElementById('cf-page-index').value = cfg.page_index || 1;
    // 自动选中匹配的本地配置环境（选项由 loadCfAccounts 注入）
    const sel = document.getElementById('cf-env-select');
    const url = cfg.server_url || '';
    if (sel) {
      const m = CF_ACCOUNTS.find(a => a.server_url && url && url === a.server_url);
      sel.value = m ? m.name : 'custom';
    }
  } catch (_) {}
}

function saveCfCfg() {
  const cfg = {
    server_url: document.getElementById('cf-server-url').value.trim(),
    mobile: document.getElementById('cf-username').value.trim(),
    password: document.getElementById('cf-password').value.trim(),
    token: document.getElementById('cf-token').value.trim(),
    proxy: document.getElementById('cf-proxy').value.trim(),
    log_type: document.getElementById('cf-log-type').value.trim(),
    page_size: parseInt(document.getElementById('cf-page-size').value) || 200,
    page_index: parseInt(document.getElementById('cf-page-index').value) || 1,
  };
  try { localStorage.setItem(CF_CFG_KEY, JSON.stringify(cfg)); } catch (_) {}
}

let CF_CURRENT_CAPTCHA_ID = '';
let CF_CURRENT_IMAGE_CODE_INDEX = '';
let CF_LAST_LOG_RESULT = null;  // 上次查询结果，供导出 JSON 用
let CF_SORT_DIR = 'desc';        // 'asc' | 'desc' 按 create_time 排序（客户端）

async function fetchCfCaptcha() {
  const serverUrl = document.getElementById('cf-server-url').value.trim();
  const proxy = document.getElementById('cf-proxy').value.trim();
  const imgEl = document.getElementById('cf-captcha-img');
  const statusEl = document.getElementById('cf-query-status');
  if (!serverUrl) {
    toast('请先填写服务器地址', 'warn');
    return;
  }
  imgEl.style.opacity = '0.4';
  imgEl.style.cursor = 'progress';
  try {
    const res = await apiPost('/api/cf/captcha', { server_url: serverUrl, proxy });
    CF_CURRENT_CAPTCHA_ID = res.captcha_id || '';
    CF_CURRENT_IMAGE_CODE_INDEX = res.image_code_index || '';
    imgEl.src = res.image || '';
    imgEl.style.opacity = '1';
    imgEl.style.cursor = 'pointer';
    document.getElementById('cf-captcha-code').value = '';
  } catch (e) {
    CF_CURRENT_CAPTCHA_ID = '';
    CF_CURRENT_IMAGE_CODE_INDEX = '';
    imgEl.style.opacity = '1';
    imgEl.style.cursor = 'pointer';
    imgEl.removeAttribute('src');
    if (statusEl) {
      statusEl.textContent = `获取验证码失败：${e.message}`;
      statusEl.className = 'cf-query-status error';
    } else {
      toast('获取验证码失败：' + e.message, 'error');
    }
  }
}

async function exportCfLogs() {
  const statusEl = document.getElementById('cf-query-status');
  const btn = document.getElementById('cf-btn-export-json');
  if (!CF_LAST_LOG_RESULT || !CF_LAST_LOG_RESULT.rows || !CF_LAST_LOG_RESULT.rows.length) {
    statusEl.textContent = '请先查询日志并确保有结果，再导出';
    statusEl.className = 'cf-query-status error';
    return;
  }
  const r = CF_LAST_LOG_RESULT;
  if (btn) { btn.disabled = true; btn.textContent = '导出中…'; }
  try {
    const res = await apiPost('/api/cf/logs/export', {
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
      // 复制文件路径到剪贴板，便于直接粘贴
      let copied = false;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(res.path);
          copied = true;
        }
      } catch (_) {}
      if (!copied) {
        // fallback：临时 textarea + execCommand
        try {
          const ta = document.createElement('textarea');
          ta.value = res.path;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.focus();
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
          copied = true;
        } catch (_) {}
      }
      statusEl.textContent = copied
        ? `已导出 ${res.count} 条，文件路径已复制到剪贴板：${res.path}`
        : `已导出 ${res.count} 条 → ${res.path}（复制到剪贴板失败，请手动复制路径）`;
      statusEl.className = 'cf-query-status success';
      try { console.log('CF 日志导出路径:', res.path); } catch (_) {}
    } else {
      throw new Error('未返回文件路径');
    }
  } catch (e) {
    statusEl.textContent = `导出失败：${e.message}`;
    statusEl.className = 'cf-query-status error';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '导出 JSON'; }
  }
}


async function saveClipboardToFile() {
  const statusEl = document.getElementById('cf-query-status');
  const btn = document.getElementById('cf-btn-clipboard-save');

  // 1) 读取系统剪贴板文本（统一入口见上方 readClipboardText：Electron / Tauri / 浏览器）
  let text = '';
  try {
    text = await readClipboardText();
  } catch (e) {
    statusEl.textContent = `读取剪贴板失败：${e.message}（请先复制文本，并点击本窗口使其获得焦点，再重试）`;
    statusEl.className = 'cf-query-status error';
    return;
  }
  if (!text || !text.trim()) {
    statusEl.textContent = '剪贴板内容为空，请先复制一些文本再点击';
    statusEl.className = 'cf-query-status error';
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  statusEl.textContent = '正在保存剪贴板内容到文件…';
  statusEl.className = 'cf-query-status';
  try {
    const res = await apiPost('/api/cf/clipboard-save', { text });
    if (res.path) {
      // 复制文件路径到剪贴板，便于直接粘贴
      let copied = false;
      try {
        await writeClipboardText(res.path);
        copied = true;
      } catch (_) {}
      statusEl.textContent = `已保存剪贴板内容（${res.size} 字符）→ ${res.path}${copied ? '（路径已复制到剪贴板）' : ''}`;
      statusEl.className = 'cf-query-status success';
      log(`剪贴板转文件成功：${res.path}`);
    } else {
      throw new Error('未返回文件路径');
    }
  } catch (ex) {
    statusEl.textContent = `保存失败：${ex.message}`;
    statusEl.className = 'cf-query-status error';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📋 剪贴板转文件'; }
  }
}

function toggleCfCfg() {
  const body = document.getElementById('cf-cfg-body');
  const btn = document.getElementById('cf-cfg-toggle');
  const visible = body.style.display !== 'none';
  body.style.display = visible ? 'none' : '';
  btn.textContent = visible ? '配置 ▸' : '配置 ▾';
}

async function cfLogin() {
  const serverUrl = document.getElementById('cf-server-url').value.trim();
  const mobile = document.getElementById('cf-username').value.trim();
  const password = document.getElementById('cf-password').value.trim();
  const proxy = document.getElementById('cf-proxy').value.trim();
  const imageCode = document.getElementById('cf-captcha-code').value.trim();
  const statusEl = document.getElementById('cf-query-status');

  if (!serverUrl || !mobile || !password) {
    statusEl.textContent = '请填写服务器地址、手机号和密码';
    statusEl.className = 'cf-query-status error';
    return;
  }
  // 图片验证码改为可选：默认不强制，仅当用户填了验证码或后端要求(need_img_valid)时才需要
  // 若用户填了验证码却没拉取过验证码图片，提示先刷新
  if (imageCode && !CF_CURRENT_CAPTCHA_ID) {
    statusEl.textContent = '请先点击「刷新」获取验证码图片再输入';
    statusEl.className = 'cf-query-status error';
    return;
  }

  const btn = document.getElementById('cf-btn-login');
  btn.disabled = true;
  btn.textContent = '登录中…';
  statusEl.textContent = '正在登录…';

  try {
    const res = await apiPost('/api/cf/login', {
      server_url: serverUrl,
      mobile,
      password,
      proxy,
      image_code: imageCode,
      image_code_index: CF_CURRENT_IMAGE_CODE_INDEX,
      captcha_id: CF_CURRENT_CAPTCHA_ID,
    });
    // 登录成功
    if (res.token) {
      document.getElementById('cf-token').value = res.token;
      CF_CURRENT_CAPTCHA_ID = '';
      CF_CURRENT_IMAGE_CODE_INDEX = '';
      document.getElementById('cf-captcha-img').removeAttribute('src');
      document.getElementById('cf-captcha-code').value = '';
      saveCfCfg();
      statusEl.textContent = '登录成功，Token 已获取并保存';
      statusEl.className = 'cf-query-status success';
      return;
    }
    // 后端透传的登录被拒：success=false
    if (res && res.ok === false) {
      const needImg = res.need_img_valid === true;
      const msg = res.message || '登录失败';
      if (needImg) {
        statusEl.textContent = `${msg}（需要图片验证码，请输入后重新登录）`;
        statusEl.className = 'cf-query-status error';
        // 自动拉取验证码图片供用户填写
        try { await fetchCfCaptcha(); } catch (_) {}
      } else {
        statusEl.textContent = `登录失败：${msg}`;
        statusEl.className = 'cf-query-status error';
      }
      return;
    }
    throw new Error('未获取到 token');
  } catch (ex) {
    statusEl.textContent = `登录失败：${ex.message}`;
    statusEl.className = 'cf-query-status error';
    // 仅在已有验证码会话时刷新（避免无验证码部署被狂刷）
    if (CF_CURRENT_CAPTCHA_ID) {
      try { await fetchCfCaptcha(); } catch (_) {}
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '登录获取 Token';
  }
}

async function queryCfLogs() {
  const serverUrl = document.getElementById('cf-server-url').value.trim();
  const token = document.getElementById('cf-token').value.trim();
  const logType = document.getElementById('cf-log-type').value.trim();
  const pageSize = parseInt(document.getElementById('cf-page-size').value) || 200;
  const pageIndex = parseInt(document.getElementById('cf-page-index').value) || 1;
  const proxy = document.getElementById('cf-proxy').value.trim();
  const statusEl = document.getElementById('cf-query-status');
  const resultsEl = document.getElementById('cf-results');

  if (!token) {
    statusEl.textContent = '请先配置 Token（可点击「登录获取 Token」或手动填写）';
    statusEl.className = 'cf-query-status error';
    return;
  }
  // 注意：log_type 允许留空（后端按空 filter 查询全部 dynamic_log），不再强制必填

  saveCfCfg();

  const btn = document.getElementById('cf-btn-query');
  btn.disabled = true;
  btn.textContent = '查询中…';
  statusEl.textContent = proxy ? `正在查询（代理：${proxy}）…` : '正在查询（直连）…';
  statusEl.className = 'cf-query-status';
  resultsEl.innerHTML = '<div class="empty-hint">加载中…</div>';

  try {
    const res = await apiPost('/api/cf/logs', {
      server_url: serverUrl,
      token,
      log_type: logType,
      page_index: pageIndex,
      page_size: pageSize,
      proxy,
    });

    // 解析返回数据：兼容 CF OpenAPI {data: {list/total}} / {result: {...}} / 裸 {list/total}
    const payload = res.data || res.result || res;
    const rows = payload.list || payload.data || payload.items || res.list || res.data || [];
    const total = payload.total ?? payload.count ?? payload.row_count ?? res.total ?? rows.length;

    // 保存查询结果供「导出 JSON」使用
    CF_LAST_LOG_RESULT = {
      server_url: serverUrl,
      log_type: logType,
      auth_method: res.method || '',
      page_index: pageIndex,
      page_size: pageSize,
      total,
      rows,
      raw: res,
    };
    const exportBtn = document.getElementById('cf-btn-export-json');
    if (exportBtn) exportBtn.disabled = rows.length === 0;

    statusEl.textContent = `查询成功，共 ${total} 条`;
    statusEl.className = 'cf-query-status success';

    // 清空上次搜索词，确保新查询整页可见
    const searchInputEl = document.getElementById('cf-search-input');
    if (searchInputEl) searchInputEl.value = '';

    if (!rows.length) {
      resultsEl.innerHTML = '<div class="empty-hint">未找到匹配的日志记录</div>';
      const sb = document.getElementById('cf-search-bar');
      if (sb) sb.style.display = 'none';
      return;
    }

    // 渲染结果（含排序 + 客户端实时搜索过滤；详见 renderCfResults）
    renderCfResults();

  } catch (ex) {
    statusEl.textContent = `查询失败：${ex.message}`;
    statusEl.className = 'cf-query-status error';
    resultsEl.innerHTML = '<div class="empty-hint">查询失败</div>';
    const exportBtn = document.getElementById('cf-btn-export-json');
    if (exportBtn) exportBtn.disabled = true;
  } finally {
    btn.disabled = false;
    btn.textContent = '查询日志';
  }
}

// CF 日志结果渲染（排序 + 客户端实时搜索过滤，不重新请求服务器）
function _cfTime(row) {
  return String(row.create_time || row.createTime || row.created_at || '');
}
function _cfContent(row) {
  const c = row.content || row.message || row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c) : String(c);
}
function _cfContentFull(row) {
  const c = row.content || row.message || row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c, null, 2) : String(c);
}
function _cfLogType(row, fallback) {
  // 优先取记录自身的 log_type（CF dynamic_log 每条记录都带该字段）；
  // 查询时指定了 log_type 则作为兜底；都没有则标「未知」
  return row.log_type || row.logType || fallback || '(未知)';
}
// 拉取剩余分页，合并为全量日志集（供「按时间排序所有日志」使用，避免只排当前页）
async function ensureAllCfLogs() {
  const base = CF_LAST_LOG_RESULT;
  if (!base || !base.rows) return;
  const total = base.total || 0;
  if (total === 0 || base.rows.length >= total) return;  // 已全量或未知总量
  const statusEl = document.getElementById('cf-query-status');
  const token = document.getElementById('cf-token').value.trim();
  const proxy = document.getElementById('cf-proxy').value.trim();
  const pageSize = base.page_size || 200;
  let nextPage = Math.floor(base.rows.length / pageSize) + 1;
  if (nextPage < 2) nextPage = 2;
  try {
    while (base.rows.length < total && nextPage <= 500) {
      statusEl.textContent = `正在加载全部日志用于排序…（${base.rows.length}/${total}）`;
      statusEl.className = 'cf-query-status';
      const res = await apiPost('/api/cf/logs', {
        server_url: base.server_url,
        token,
        log_type: base.log_type,
        page_index: nextPage,
        page_size: pageSize,
        proxy,
      });
      const payload = res.data || res.result || res;
      const pageRows = payload.list || payload.data || payload.items || res.list || res.data || [];
      if (!pageRows.length) break;
      base.rows = base.rows.concat(pageRows);
      nextPage += 1;
    }
  } catch (e) {
    statusEl.textContent = `加载全部日志失败：${e.message}（已对当前已加载 ${base.rows.length} 条排序）`;
    statusEl.className = 'cf-query-status error';
  }
}

function renderCfResults() {
  const base = CF_LAST_LOG_RESULT;
  const resultsEl = document.getElementById('cf-results');
  const searchBar = document.getElementById('cf-search-bar');
  if (!base || !base.rows) return;

  const all = base.rows.slice();
  // 是否已加载全部日志（用于排序/计数提示）
  const isFull = (base.total || 0) > 0 && base.rows.length >= base.total;
  // 排序（按时间）
  all.sort((a, b) => {
    const ta = _cfTime(a), tb = _cfTime(b);
    const cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
    return CF_SORT_DIR === 'asc' ? cmp : -cmp;
  });

  // 客户端实时过滤（内容 + 时间）
  const q = (document.getElementById('cf-search-input').value || '').trim();
  const caseSensitive = document.getElementById('cf-search-case').checked;
  let filtered = all;
  if (q) {
    const needle = caseSensitive ? q : q.toLowerCase();
    filtered = all.filter(r => {
      const content = caseSensitive ? _cfContent(r) : _cfContent(r).toLowerCase();
      const time = caseSensitive ? _cfTime(r) : _cfTime(r).toLowerCase();
      const type = caseSensitive ? _cfLogType(r, base.log_type) : _cfLogType(r, base.log_type).toLowerCase();
      return content.includes(needle) || time.includes(needle) || type.includes(needle);
    });
  }

  // 显示搜索栏 + 更新计数 / 排序按钮文案
  if (searchBar) searchBar.style.display = '';
  const cntEl = document.getElementById('cf-search-count');
  if (cntEl) cntEl.innerHTML = q
    ? `匹配 <b>${filtered.length}</b> / ${isFull ? '全部' : '本页'} ${all.length}`
    : (isFull ? `共 ${all.length} 条` : `本页 ${all.length} / 共 ${base.total} 条`);
  const sortBtn = document.getElementById('cf-btn-sort-time');
  if (sortBtn) sortBtn.textContent = CF_SORT_DIR === 'asc' ? '时间 ↑' : '时间 ↓';

  if (!filtered.length) {
    resultsEl.innerHTML = `<div class="empty-hint">${q ? '没有匹配当前搜索条件的日志' : '未找到匹配的日志记录'}</div>`;
    return;
  }

  const logType = base.log_type || '';
  let html = `
    <div class="cf-result-meta">
      <span class="cf-result-count">${all.length} 条结果</span>
      <span>${isFull ? '已加载全部（本地排序）' : `第 ${base.page_index || 1} 页`}</span>
      ${logType ? `<span>log_type: ${esc(logType)}</span>` : '<span>全部 log_type</span>'}
    </div>
    <table class="cf-log-table">
      <thead>
        <tr>
          <th style="width:48px">#</th>
          <th style="width:150px">类型</th>
          <th style="width:170px">时间</th>
          <th>内容</th>
        </tr>
      </thead>
      <tbody>
  `;
  filtered.forEach((row, i) => {
    const createTime = _cfTime(row);
    const content = _cfContent(row);
    const contentFull = _cfContentFull(row);
    const logTypeVal = _cfLogType(row, base.log_type);
    const idx = i + 1;
    html += `
      <tr class="cf-log-row" data-idx="${i}">
        <td>${idx}</td>
        <td class="cf-log-type" title="${esc(logTypeVal)}">${esc(logTypeVal)}</td>
        <td class="cf-log-time">${esc(createTime)}</td>
        <td class="cf-log-content">${esc(content)}</td>
      </tr>
      <tr class="cf-log-detail-row" id="cf-detail-${i}" style="display:none">
        <td colspan="4">
          <div class="cf-log-meta">类型：${esc(logTypeVal)} ｜ ID：${esc(row.id != null ? row.id : (row._id || ''))} ｜ 时间：${esc(createTime)}</div>
          <div class="cf-log-content-full">${esc(contentFull)}</div>
        </td>
      </tr>
    `;
  });
  html += '</tbody></table>';

  // 服务端分页（按 total 计算；搜索为当前页内过滤）
  // 已加载全部日志（本地排序）时不再显示服务端翻页，避免与本地全量视图冲突
  const pageSize = base.page_size || 200;
  const pageIndex = base.page_index || 1;
  const total = base.total != null ? base.total : all.length;
  const totalPages = Math.ceil(total / pageSize);
  if (totalPages > 1 && !isFull) {
    html += '<div class="cf-pagination">';
    if (pageIndex > 1) {
      html += `<button class="btn btn-sm" onclick="document.getElementById('cf-page-index').value=${pageIndex - 1};queryCfLogs()">上一页</button>`;
    }
    html += `<span style="font-size:12px;color:var(--muted)">${pageIndex} / ${totalPages}</span>`;
    if (pageIndex < totalPages) {
      html += `<button class="btn btn-sm" onclick="document.getElementById('cf-page-index').value=${pageIndex + 1};queryCfLogs()">下一页</button>`;
    }
    html += '</div>';
  }
  resultsEl.innerHTML = html;

  // 绑定行点击展开/收起
  document.querySelectorAll('.cf-log-row').forEach(tr => {
    tr.onclick = () => {
      const idx = tr.dataset.idx;
      const detail = document.getElementById(`cf-detail-${idx}`);
      if (detail) {
        const visible = detail.style.display !== 'none';
        detail.style.display = visible ? 'none' : '';
        tr.classList.toggle('expanded', !visible);
      }
    };
  });
}

// CF 事件绑定
document.getElementById('cf-env-select').addEventListener('change', (e) => {
  switchCfEnv(e.target.value);
});
document.getElementById('cf-cfg-toggle').onclick = function() {
  toggleCfCfg();
  // 不自动拉验证码 — 只有用户手动点「刷新」或「登录获取Token」才拉
};
document.getElementById('cf-btn-save').onclick = saveCfCfg;
document.getElementById('cf-btn-login').onclick = async () => {
  // 点登录前先确保有验证码
  const imgEl = document.getElementById('cf-captcha-img');
  if (!imgEl.getAttribute('src')) {
    try { await fetchCfCaptcha(); } catch (e) { return; }
  }
  cfLogin();
};
document.getElementById('cf-btn-query').onclick = queryCfLogs;
document.getElementById('cf-btn-export-json').onclick = exportCfLogs;
document.getElementById('cf-btn-clipboard-save').onclick = saveClipboardToFile;
document.getElementById('cf-btn-refresh-captcha').onclick = fetchCfCaptcha;
document.getElementById('cf-captcha-img').addEventListener('click', fetchCfCaptcha);
document.getElementById('cf-log-type').addEventListener('keydown', e => {
  if (e.key === 'Enter') queryCfLogs();
});
// 云函数日志搜索栏：实时过滤 + 时间排序
document.getElementById('cf-search-input').addEventListener('input', renderCfResults);
document.getElementById('cf-search-case').addEventListener('change', renderCfResults);
document.getElementById('cf-btn-sort-time').addEventListener('click', async () => {
  CF_SORT_DIR = CF_SORT_DIR === 'asc' ? 'desc' : 'asc';
  const btn = document.getElementById('cf-btn-sort-time');
  if (btn) btn.disabled = true;
  try {
    // 排序前先拉取剩余分页，确保排序覆盖「所有日志」而非仅当前页
    await ensureAllCfLogs();
    renderCfResults();
  } finally {
    if (btn) btn.disabled = false;
  }
});
loadCfAccounts().then(loadCfCfg);

