/* ============================================================
   核心：状态 / API / 工具 / 日志 / SSE / 主题
   全局 state、API 封装、HTML 转义、Diff 工具、Toast、日志、状态栏、SSE、进度条、switchTab、辅助函数、主题、剪贴板 helper。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
/**
 * Jira Git GUI — Web 前端逻辑
 *
 * 架构：
 * - REST API 调用（fetch）
 * - SSE 接收日志 / 进度 / 任务完成事件
 * - 纯 vanilla JS，无框架依赖
 */

// ===== 全局状态 =====
const state = {
  repos: [],
  selectedRepo: null,
  treeNodes: {},  // path -> {loaded, children, element}
  checkedPaths: new Set(),
  selectedFile: null,
  // 文件树工具栏
  treeSearch: { q: '', scope: 'filename', results: [] },
  treeSort: { key: 'name', dir: 'asc' },
  commits: [],
  selectedCommit: null,
  sse: null,
  maxWorkers: 4,
  qps: 6,
  repoMappings: {},  // repo_name -> local_dir，来自 .env 的 MERGE_REPO_* 映射
  k8s: { running: false, outDir: null, env: null,
         shell: { cwd: '/', connected: false, history: [], histIdx: 0 },
         files: { path: '/', selected: null, editPath: null, entries: [], sort: { key: 'name', dir: 'asc' } } },
};

// 统一用 location.origin：
// - Electron / Web：前后端同源（后端提供页面）；
// - Tauri：窗口加载的就是后端 URL（端口由 Rust 探测，可能是顺延后的备用端口），
//   location.origin 天然等于后端地址。不再硬编码 8787，避免端口顺延时连错。
const API = location.origin;

// ===== API 封装 =====
async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(`${API}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (e) {
    // 网络层错误：超时 vs 断网
    if (e && e.name === 'AbortError') {
      const err = new Error('请求超时，请检查网络或稍后重试');
      err.type = 'timeout'; throw err;
    }
    const err = new Error('网络连接失败，请检查网络或服务是否运行');
    err.type = 'network'; throw err;
  }
  // 只消费一次 body：先读 text 再尝试 parse JSON
  let text = '';
  try { text = await res.text(); } catch (_) {}
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { _raw: text }; }
  } else {
    data = {};
  }
  if (!res.ok) {
    // FastAPI HTTPException 默认 JSON 格式: {"detail": "..."}；CF 用 {errmsg}
    const detail =
      (data && typeof data.detail === 'string' && data.detail) ||
      (data && typeof data.detail === 'object' && JSON.stringify(data.detail)) ||
      (data && typeof data.message === 'string' && data.message) ||
      (data && typeof data.msg === 'string' && data.msg) ||
      (data && typeof data.errmsg === 'string' && data.errmsg) ||
      (data && typeof data.error === 'string' && data.error) ||
      (data && typeof data._raw === 'string' && data._raw.slice(0, 4000)) ||
      '';
    // 认证失效：401/403，或 CF 登录类 errcode(17003/17001/need_img_valid)
    const isAuth = res.status === 401 || res.status === 403 ||
      /未登录|登录失效|登录已过期|token\s*(失效|过期|无效)|认证失败|无权访问|17003|17001|need_img_valid/i.test(detail);
    if (isAuth) {
      const err = new Error(detail || '登录已失效，请重新登录');
      err.type = 'auth'; err.status = res.status; throw err;
    }
    const err = new Error(detail || `HTTP ${res.status} ${res.statusText || ''}`.trim());
    err.type = res.status >= 500 ? 'server' : 'business';
    err.status = res.status;
    throw err;
  }
  return data || {};
}

// 统一错误 -> toast：根据 ex.type 选择图标与操作，auth 类带「去连接设置」按钮
function toastApiError(ex, fallbackAction) {
  const msg = (ex && ex.message) ? ex.message : '操作失败';
  const type = (ex && ex.type) || 'error';
  const toastType = type === 'auth' ? 'warn' : (type === 'timeout' || type === 'network' ? 'warn' : 'error');
  const opts = {};
  if (type === 'auth') {
    opts.duration = 0; // 认证失效不自动消失，需用户处理
    opts.action = {
      label: '去连接设置',
      primary: true,
      onClick: () => { try { openConnectModal(); } catch (_) {} }
    };
  } else if (fallbackAction) {
    opts.action = fallbackAction;
  }
  return toast(msg, toastType, opts);
}

async function apiPost(path, body) {
  return api(path, { method: 'POST', body: JSON.stringify(body) });
}

async function apiDelete(path) {
  return api(path, { method: 'DELETE' });
}

// ===== 工具：HTML 转义 =====
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ===== Diff 工具（行级 LCS） =====
// 渲染纯文本的行级 diff（红绿行）。单文件超 5000 行会截断，避免大文件卡死。
const DIFF_MAX_LINES = 5000;

function renderDiff(oldText, newText) {
  const a = (oldText || '').split('\n');
  const b = (newText || '').split('\n');
  if (a.length > DIFF_MAX_LINES || b.length > DIFF_MAX_LINES) {
    return `<div class="diff-truncated">⚠ 文件过大（${a.length} → ${b.length} 行），仅渲染前 ${DIFF_MAX_LINES} 行差异</div>`
      + _renderDiffCore(a.slice(0, DIFF_MAX_LINES), b.slice(0, DIFF_MAX_LINES));
  }
  return _renderDiffCore(a, b);
}

function _renderDiffCore(a, b) {
  // 1) LCS 动态规划，O(m*n)；m/n 各 5000 行 ≈ 25MB 临时数组，可接受
  const m = a.length, n = b.length;
  const dp = new Array(m + 1);
  for (let i = 0; i <= m; i++) dp[i] = new Uint32Array(n + 1);
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  // 2) 回溯生成 diff ops
  const ops = []; // {type: 'eq'|'del'|'add', text: string}
  let i = 0, j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) { ops.push({ type: 'eq', text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ type: 'del', text: a[i] }); i++; }
    else { ops.push({ type: 'add', text: b[j] }); j++; }
  }
  while (i < m) { ops.push({ type: 'del', text: a[i++] }); }
  while (j < n) { ops.push({ type: 'add', text: b[j++] }); }
  // 3) 渲染
  const lines = [];
  let oldNo = 0, newNo = 0, adds = 0, dels = 0;
  for (const op of ops) {
    if (op.type === 'eq') { oldNo++; newNo++; }
    else if (op.type === 'del') { oldNo++; dels++; }
    else { newNo++; adds++; }
    const cls = op.type === 'add' ? 'diff-add' : op.type === 'del' ? 'diff-del' : 'diff-eq';
    const sign = op.type === 'add' ? '+' : op.type === 'del' ? '-' : ' ';
    const oldN = String(oldNo).padStart(4);
    const newN = String(newNo).padStart(4);
    lines.push(
      `<div class="diff-row ${cls}">`
      + `<span class="diff-gutter diff-gutter-old">${op.type === 'add' ? '' : oldN}</span>`
      + `<span class="diff-gutter diff-gutter-new">${op.type === 'del' ? '' : newN}</span>`
      + `<span class="diff-sign">${sign}</span>`
      + `<span class="diff-text">${escapeHtml(op.text || ' ')}</span>`
      + `</div>`
    );
  }
  return (
    `<div class="diff-stats">+${adds} -${dels}</div>`
    + `<div class="diff-body">${lines.join('')}</div>`
  );
}

// ===== 全局 Toast 通知 =====
let _toastStack = null;
function _getToastStack() {
  if (_toastStack) return _toastStack;
  _toastStack = document.createElement('div');
  _toastStack.className = 'toast-stack';
  _toastStack.id = 'toast-stack';
  document.body.appendChild(_toastStack);
  return _toastStack;
}
const _TOAST_ICONS = { success: '✓', warn: '!', error: '✕', info: 'i' };
function toast(message, type = 'info', opts = {}) {
  const stack = _getToastStack();
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  const icon = _TOAST_ICONS[type] || 'i';
  let actionsHtml = '';
  if (opts && opts.action && opts.action.label) {
    const cls = opts.action.primary ? ' primary' : '';
    actionsHtml = '<div class="toast-actions"><button class="' + cls + '">' + escapeHtml(opts.action.label) + '</button></div>';
  }
  el.innerHTML =
    '<span class="toast-icon">' + icon + '</span>' +
    '<div class="toast-body">' + escapeHtml(message) + actionsHtml + '</div>' +
    '<button class="toast-close" aria-label="关闭">×</button>';
  stack.appendChild(el);
  const close = () => {
    if (el.classList.contains('leaving')) return;
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector('.toast-close').addEventListener('click', close);
  if (opts && opts.action && opts.action.label) {
    el.querySelector('.toast-actions button').addEventListener('click', () => {
      try { opts.action.onClick && opts.action.onClick(); } catch (_) {}
      close();
    });
  }
  const duration = opts && opts.duration != null ? opts.duration : (type === 'error' ? 8000 : 3500);
  if (duration > 0) setTimeout(close, duration);
  return { close };
}

// withLoading: 异步操作统一反馈基线
// - btn: HTMLButtonElement 或选择器字符串
// - fn: async () => any（真正的操作逻辑）
// - opts: { loadingText, originalText, okToast: string|(r)=>string, failToast: bool }
async function withLoading(btn, fn, opts = {}) {
  if (typeof btn === 'string') btn = document.querySelector(btn);
  const okToast = opts.okToast === undefined ? true : opts.okToast;
  const failToast = opts.failToast !== false;
  let originalText = opts.originalText;
  if (btn && !originalText) originalText = btn.innerHTML;
  const loadingText = opts.loadingText || '<span class="caction-sparkle">⏳</span>处理中…';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = loadingText;
  }
  try {
    const r = await fn();
    if (okToast) {
      const msg = typeof okToast === 'function' ? okToast(r) : (typeof okToast === 'string' ? okToast : '操作成功');
      toast(msg, 'success');
    }
    return r;
  } catch (ex) {
    if (failToast) toastApiError(ex);
    throw ex;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalText || '';
    }
  }
}

// ===== 日志 =====
// 浏览器模式下 log 只到 UI；Electron 下也写入主进程文件日志。
// 同时 Electron 模式下会接收主进程/Python 日志回调 -> 回写到 UI。
function log(msg, level = 'info') {
  const el = document.getElementById('log-content');
  if (!el) return;  // DOM 未就绪时忽略
  const line = document.createElement('div');
  line.className = `log-line ${level}`;
  line.textContent = msg;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  // 限制日志条数
  while (el.children.length > 3000) el.removeChild(el.firstChild);

  // Electron：转发到主进程统一落盘
  if (window.electronAPI?.log) {
    try { window.electronAPI.log(level, msg); } catch (_) {}
  }
  // Tauri：转发到 Rust 主进程统一落盘（withGlobalTauri 注入的全局 API）
  if (window.__TAURI__?.core) {
    try { window.__TAURI__.core.invoke('log_message', { level, msg }).catch(() => {}); } catch (_) {}
  }
}

function clearLog() {
  const el = document.getElementById('log-content');
  if (el) el.innerHTML = '';
}

// ===== 状态栏 =====
function updateStatus() {
  api('/api/status').then(s => {
    const cookieLabel = s.cookie_set
      ? `Cookie 已配置${s.cookie_source ? '(' + s.cookie_source + ')' : ''}`
      : 'Cookie 未配置';
    const parts = [
      `模式 ${s.mode?.toUpperCase()}`,
      `仓库 ${s.repo_id || '-'}`,
      `分支 ${s.branch || '(默认)'}`,
      cookieLabel,
      `PAT ${s.pat_set ? '已配置' : '未配置'}`,
      `速率 ${s.qps}/秒`,
    ];
    document.getElementById('status-text').textContent = parts.join(' | ');

    // 状态指示点：凭证已配置 = 绿，否则 = 黄
    const dot = document.getElementById('status-dot');
    if (dot) {
      const ok = s.cookie_set || s.pat_set;
      dot.className = 'status-dot ' + (ok ? 'ok' : 'warn');
      dot.title = ok ? '后端已连接，凭证已配置' : '后端未配置凭证';
    }

    // 同步连接弹窗的值
    if (s.jira_url) document.getElementById('cfg-url').value = s.jira_url;
    if (s.username) document.getElementById('cfg-user').value = s.username;
    if (s.repo_id) document.getElementById('cfg-repo-id').value = s.repo_id;
    if (s.repo_name) document.getElementById('cfg-repo-name').value = s.repo_name;
    if (s.branch) document.getElementById('cfg-branch').value = s.branch;
    if (s.repo_id) document.getElementById('inp-repo-id').value = s.repo_id;
    if (s.repo_name) document.getElementById('inp-repo-name').value = s.repo_name;
    if (s.branch) document.getElementById('inp-branch').value = s.branch;
  });
}

// ===== SSE 事件 =====
function connectSSE() {
  if (state.sse) state.sse.close();
  state.sse = new EventSource(`${API}/api/events`);

  state.sse.addEventListener('log', e => {
    const d = JSON.parse(e.data);
    log(d.msg);
  });

  state.sse.addEventListener('progress', e => {
    const d = JSON.parse(e.data);
    const bar = document.getElementById('progress-bar');
    const lbl = document.getElementById('progress-label');
    bar.style.display = '';
    if (d.total > 0) {
      bar.max = d.total;
      bar.value = d.done;
      lbl.textContent = `${d.done}/${d.total} (${d.pct}%)`;
    } else {
      lbl.textContent = `已处理 ${d.done}…`;
    }
  });

  state.sse.addEventListener('clone_done', e => {
    const d = JSON.parse(e.data);
    log(`克隆结果：${d.msg}`);
    if (d.ok) {
      log(`本地路径：${d.path}`);
      loadTree('');
      switchTab('tree');
    }
    hideProgress();
  });

  state.sse.addEventListener('download_done', e => {
    const d = JSON.parse(e.data);
    log(`下载完成：成功 ${d.ok_count}（跳过 ${d.skipped}），失败 ${d.fail_count}。`);
    if (d.dest) log(`已保存到：${d.dest}`);
    if (d.fails) d.fails.forEach(f => log(`  ✗ ${f.path}: ${f.reason}`));
    if (d.total_fails > 20) log(`  （失败项共 ${d.total_fails} 个，仅显示前 20）`);
    hideProgress();
  });

  state.sse.addEventListener('ping', () => {});

  // ===== 差异扫描进度 =====
  state.sse.addEventListener('scan_stage', e => {
    const d = JSON.parse(e.data);
    clearTimeout(diffDoneTimer);  // 新阶段开始，取消之前 scan_done 的自动隐藏
    setDiffProgress({ visible: true, mode: 'indeterminate', stage: d.message, detail: '' });
    clearDiffErrors();
  });
  state.sse.addEventListener('scan_progress', e => {
    const d = JSON.parse(e.data);
    clearTimeout(diffDoneTimer);  // 扫描进行中，确保不被旧定时器隐藏
    setDiffProgress({
      visible: true, mode: 'indeterminate',
      stage: '扫描远程文件…',
      detail: d.message || `已扫描 ${d.done} 个文件`,
    });
  });
  state.sse.addEventListener('scan_done', e => {
    const d = JSON.parse(e.data);
    const sum = d.summary || {};
    setDiffProgress({
      mode: 'done', stage: '扫描完成',
      detail: sum.total != null ? `共 ${sum.total} 个文件` : '',
    });
    // 成功完成后延迟隐藏进度条（保留几秒供用户确认）
    clearTimeout(diffDoneTimer);
    diffDoneTimer = setTimeout(() => {
      setDiffProgress({ visible: false });
    }, 4000);
  });
  state.sse.addEventListener('scan_error', e => {
    const d = JSON.parse(e.data);
    clearTimeout(diffDoneTimer);  // 出错时不自动隐藏，让用户看清错误
    setDiffProgress({ visible: true, mode: 'error', stage: '扫描失败', detail: '' });
    addDiffError(d.message || '未知错误');
  });

  // ===== 批量合并进度 =====
  state.sse.addEventListener('merge_start', e => {
    const d = JSON.parse(e.data);
    clearTimeout(diffDoneTimer);  // 合并开始，取消 scan_done 的自动隐藏
    setDiffProgress({
      visible: true, mode: 'determinate', pct: 0,
      stage: `合并中 (0/${d.total})`,
      detail: `共 ${d.total} 个文件`,
    });
    clearDiffErrors();
  });
  state.sse.addEventListener('merge_progress', e => {
    const d = JSON.parse(e.data);
    clearTimeout(diffDoneTimer);  // 合并进行中，确保不被旧定时器隐藏
    setDiffProgress({
      mode: 'determinate', pct: d.pct,
      stage: `合并中 (${d.done}/${d.total})`,
      detail: (d.ok ? '✓ ' : '✗ ') + d.path,
    });
    // 实时更新「全部合并」按钮文字，让用户看到完成数
    const mergeBtn = document.getElementById('btn-diff-merge-all');
    if (mergeBtn && mergeBtn.disabled) {
      mergeBtn.textContent = `合并中 (${d.done}/${d.total})…`;
    }
    if (!d.ok && d.error) addDiffError(`${d.path}：${d.error}`);
  });
  state.sse.addEventListener('merge_done', e => {
    const d = JSON.parse(e.data);
    setDiffProgress({
      mode: d.fail_count > 0 ? 'error' : 'done',
      stage: d.fail_count > 0 ? `合并完成（${d.fail_count} 个失败）` : '合并完成',
      detail: `成功 ${d.ok_count}，失败 ${d.fail_count}`,
    });
    // 合并完成后也延迟隐藏，与 scan_done 行为一致
    clearTimeout(diffDoneTimer);
    diffDoneTimer = setTimeout(() => {
      setDiffProgress({ visible: false });
    }, 5000);
  });

  // ===== 网络看门狗告警 =====
  state.sse.addEventListener('network_warning', e => {
    const d = JSON.parse(e.data);
    const box = document.getElementById('network-warning');
    box.textContent = '⚠ ' + (d.message || '网络中断');
    box.style.display = '';
    box.className = 'network-warning ' + (d.level || 'error');
    log(d.message || '网络中断，任务已自动停止', 'error');
    // 5 秒后自动隐藏（若手动未关闭）
    clearTimeout(window.__netWarnTimer);
    window.__netWarnTimer = setTimeout(() => {
      box.style.display = 'none';
    }, 10000);
  });

  // ===== K8s 快照事件 =====
  state.sse.addEventListener('k8s_log', e => {
    const d = JSON.parse(e.data);
    appendK8sLog(d.msg);
  });

  state.sse.addEventListener('k8s_progress', e => {
    const d = JSON.parse(e.data);
    const wrap = document.getElementById('k8s-progress');
    const bar = document.getElementById('k8s-progress-bar');
    const lbl = document.getElementById('k8s-progress-label');
    if (wrap) wrap.style.display = '';
    if (bar) bar.value = d.pct || 0;
    if (lbl) lbl.textContent = `抓取日志 ${d.done}/${d.total} ${d.name || ''} (${d.pct || 0}%)`;
  });

  state.sse.addEventListener('k8s_done', e => {
    const d = JSON.parse(e.data);
    state.k8s.outDir = d.out_dir;
    renderK8sSummary(d.summary);
    renderK8sTable(d.records);
    const prog = document.getElementById('k8s-progress');
    if (prog) prog.style.display = 'none';
    document.getElementById('btn-k8s-report').style.display = '';
    document.getElementById('btn-k8s-dir').style.display = '';
    appendK8sLog(`快照完成：总数 ${d.summary.total}，异常 ${d.summary.high}，警告 ${d.summary.med}，日志 ${d.summary.logs}`, 'info');
  });

  state.sse.addEventListener('k8s_error', e => {
    const d = JSON.parse(e.data);
    appendK8sLog('错误：' + d.message, 'error');
    log('K8s 快照失败：' + d.message, 'error');
  });

  state.sse.addEventListener('k8s_finished', () => {
    setK8sRunning(false);
  });

  state.sse.onerror = () => {
    setTimeout(() => connectSSE(), 2000);
  };
}


// ===== 进度条 =====
function showProgress() {
  document.getElementById('progress-bar').style.display = '';
  document.getElementById('progress-label').textContent = '准备中…';
  document.getElementById('btn-cancel').style.display = '';
}

function hideProgress() {
  document.getElementById('btn-cancel').style.display = 'none';
  document.getElementById('progress-label').textContent = '完成';
  setTimeout(() => {
    document.getElementById('progress-bar').style.display = 'none';
    document.getElementById('progress-label').textContent = '';
  }, 2000);
}


// ===== 标签页切换 =====
function switchTab(name) {
  // 「文件树」「文件预览」已合并进「仓库」tab 的三栏布局，旧的切换请求重定向到 repo
  if (name === 'tree' || name === 'preview') name = 'repo';
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.tab-pane').forEach(p => {
    p.classList.toggle('active', p.id === `tab-${name}`);
  });
  // 操作条仅在与「仓库下载」相关的 tab 显示；K8s / 日志 等无关 tab 隐藏
  // （避免「克隆仓库 / PAT / 下载」等不相关操作出现在运维页面）
  const bar = document.getElementById('actionbar');
  if (bar) {
    const showBar = ['repo', 'commits', 'diff'].includes(name);
    bar.style.display = showBar ? '' : 'none';
  }
}


// ===== 辅助 =====
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

function fmtSize(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} K`;
  return `${(n / 1048576).toFixed(1)} M`;
}

function fmtMtime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return '';
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ===== 主题切换（浅色 / 深色） =====
function applyTheme(theme) {
  const body = document.body;
  if (theme === 'dark') body.classList.add('dark');
  else body.classList.remove('dark');
  const btn = document.getElementById('btn-theme');
  if (btn) btn.textContent = theme === 'dark' ? '☀ 主题' : '🌓 主题';
}
function toggleTheme() {
  const cur = document.body.classList.contains('dark') ? 'dark' : 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem('jgg-theme', next); } catch (_) {}
}


// ===== 剪贴板（三端统一） =====
// Electron 走 preload 暴露的原生 clipboard；Tauri 走 Rust clipboard-manager 插件；
// 纯 Web 回退浏览器 navigator.clipboard（受页面权限限制，需用户授权）。
async function readClipboardText() {
  if (window.electronAPI?.isElectron) {
    return await window.electronAPI.readClipboardText();
  }
  if (window.__TAURI__?.core) {
    return await window.__TAURI__.core.invoke('plugin:clipboard-manager|read_text');
  }
  if (navigator.clipboard && navigator.clipboard.readText) {
    return await navigator.clipboard.readText();
  }
  throw new Error('当前环境不支持剪贴板读取 API');
}

async function writeClipboardText(text) {
  if (window.electronAPI?.isElectron) {
    await window.electronAPI.writeClipboardText(text);
    return;
  }
  if (window.__TAURI__?.core) {
    await window.__TAURI__.core.invoke('plugin:clipboard-manager|write_text', { text });
    return;
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
  }
}
