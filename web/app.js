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
  commits: [],
  selectedCommit: null,
  sse: null,
  maxWorkers: 4,
  qps: 6,
  repoMappings: {},  // repo_name -> local_dir，来自 .env 的 MERGE_REPO_* 映射
  k8s: { running: false, outDir: null, env: null,
         shell: { cwd: '/', connected: false, history: [], histIdx: 0 },
         files: { path: '/', selected: null, editPath: null, entries: [] } },
};

// Tauri 模式下前端由 Tauri 本体提供，后端在 127.0.0.1:8787；
// Electron / Web 模式前后端同源，直接用 location.origin。
const API = window.__TAURI__ ? 'http://127.0.0.1:8787' : location.origin;

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
    // FastAPI HTTPException 默认 JSON 格式: {"detail": "..."}；HCM 用 {errmsg}
    const detail =
      (data && typeof data.detail === 'string' && data.detail) ||
      (data && typeof data.detail === 'object' && JSON.stringify(data.detail)) ||
      (data && typeof data.message === 'string' && data.message) ||
      (data && typeof data.msg === 'string' && data.msg) ||
      (data && typeof data.errmsg === 'string' && data.errmsg) ||
      (data && typeof data.error === 'string' && data.error) ||
      (data && typeof data._raw === 'string' && data._raw.slice(0, 4000)) ||
      '';
    // 认证失效：401/403，或 HCM 登录类 errcode(17003/17001/need_img_valid)
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
  // Tauri：转发到 Rust 主进程统一落盘
  if (window.__TAURI__) {
    try {
      import('@tauri-apps/api/core').then(m =>
        m.invoke('log_message', { level, msg })
      ).catch(() => {});
    } catch (_) {}
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

// ===== K8s 快照 =====
function appendK8sLog(msg, level = 'info') {
  const el = document.getElementById('k8s-log');
  if (el) {
    const line = document.createElement('div');
    line.className = 'log-line ' + level;
    line.textContent = msg;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
    while (el.children.length > 2000) el.removeChild(el.firstChild);
  }
  log(msg, level);  // 同时写入全局日志
}

function setK8sRunning(running) {
  state.k8s.running = running;
  document.getElementById('btn-k8s-run').style.display = running ? 'none' : '';
  document.getElementById('btn-k8s-cancel').style.display = running ? '' : 'none';
  if (!running) {
    const prog = document.getElementById('k8s-progress');
    if (prog) prog.style.display = 'none';
  }
}

function renderK8sSummary(s) {
  const el = document.getElementById('k8s-summary');
  el.style.display = '';
  el.innerHTML = `
    <div class="k8s-stat"><div class="n">${s.total}</div><div class="l">Pod 总数</div></div>
    <div class="k8s-stat ok"><div class="n">${s.ok}</div><div class="l">正常</div></div>
    <div class="k8s-stat med"><div class="n">${s.med}</div><div class="l">警告</div></div>
    <div class="k8s-stat high"><div class="n">${s.high}</div><div class="l">异常</div></div>
    <div class="k8s-stat"><div class="n">${s.logs}</div><div class="l">日志数</div></div>`;
}

function renderK8sTable(records) {
  const tb = document.getElementById('k8s-tbody');
  tb.innerHTML = '';
  document.getElementById('k8s-table-hint').textContent = `共 ${records.length} 个`;
  for (const r of records) {
    const tr = document.createElement('tr');
    tr.className = 'k8s-row sev-' + r.sev;
    const problemText = (r.problems || []).map(p => p[1]).join('; ') || r.reason || '—';
    tr.innerHTML = `
      <td class="k8s-name" title="${esc(r.name)}">${esc(r.name)}</td>
      <td>${esc(r.phase || '—')}</td>
      <td>${r.ready}/${r.total}</td>
      <td class="${r.restarts > 0 ? 'k8s-restarts' : ''}">${r.restarts}</td>
      <td>${esc(problemText)}</td>
      <td>${esc(r.node || '—')}</td>
      <td>${esc(r.host_ip || '—')}</td>
      <td>${esc(r.pod_ip || '—')}</td>
      <td>${esc(r.age || '—')}</td>
      <td><span class="k8s-badge sev-${r.sev}">${r.sev}</span></td>`;
    tr.onclick = () => viewK8sLog(r.name);
    tb.appendChild(tr);
  }
}

async function viewK8sLog(name) {
  state.k8s.lastPod = name;
  document.getElementById('k8s-log-name').textContent = name;
  const box = document.getElementById('k8s-log');
  box.textContent = '加载日志中…';
  try {
    const res = await fetch(`${API}/api/k8s/log?name=${encodeURIComponent(name)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    box.textContent = text || '（无日志）';
  } catch (ex) {
    box.textContent = '加载失败：' + ex.message;
  }
}

async function runK8s() {
  if (state.k8s.running) return;
  const cfg = {
    namespace: document.getElementById('k8s-namespace').value.trim(),
    selector: document.getElementById('k8s-selector').value.trim(),
    pod_filter: document.getElementById('k8s-filter').value.trim(),
    tail: parseInt(document.getElementById('k8s-tail').value) || 200,
    restart_threshold: parseInt(document.getElementById('k8s-restart').value) || 5,
    all_logs: document.getElementById('k8s-alllogs').checked,
    include_previous: document.getElementById('k8s-prev').checked,
    out_dir: document.getElementById('k8s-outdir').value.trim(),
    kubeconfig: document.getElementById('k8s-kubeconfig').value.trim(),
    env: state.k8s.env,
    log_level: document.getElementById('k8s-log-level').value,
  };
  // 清空上次结果
  document.getElementById('k8s-tbody').innerHTML =
    '<tr><td colspan="10" class="empty-hint">抓取中…</td></tr>';
  document.getElementById('k8s-summary').style.display = 'none';
  document.getElementById('k8s-log').textContent = '';
  document.getElementById('k8s-progress').style.display = '';
  document.getElementById('k8s-progress-bar').value = 0;
  document.getElementById('k8s-progress-label').textContent = '准备中…';
  document.getElementById('btn-k8s-report').style.display = 'none';
  document.getElementById('btn-k8s-dir').style.display = 'none';
  appendK8sLog('开始抓取 K8s 快照…');
  setK8sRunning(true);
  try {
    await apiPost('/api/k8s/snapshot', cfg);
  } catch (ex) {
    appendK8sLog('请求失败：' + ex.message, 'error');
    setK8sRunning(false);
  }
}

async function cancelK8s() {
  try {
    await apiPost('/api/k8s/cancel', {});
    appendK8sLog('已发送取消信号');
  } catch (ex) {
    appendK8sLog('取消失败：' + ex.message, 'error');
  }
}

function openK8sReport() {
  window.open('/api/k8s/report', '_blank');
}

async function copyK8sDir() {
  const dir = state.k8s.outDir;
  if (!dir) return;
  try {
    await navigator.clipboard.writeText(dir);
    appendK8sLog('已复制输出目录：' + dir);
  } catch (_) {
    appendK8sLog('输出目录：' + dir);
  }
}

// ===== K8s 环境管理 =====
async function loadK8sEnvs() {
  try {
    const d = await api('/api/k8s/env');
    if (!d || !d.environments) return;
    const sel = document.getElementById('k8s-env');
    // 临时解绑 onchange，避免 innerHTML 清空和设置 selected 时触发重复的 onK8sEnvChange
    const prevHandler = sel.onchange;
    sel.onchange = null;
    sel.innerHTML = '';
    d.environments.forEach(e => {
      const o = document.createElement('option');
      o.value = e.name;
      o.textContent = e.label + ' (' + e.name + ')';
      if (e.is_current) o.selected = true;
      sel.appendChild(o);
    });
    state.k8s.env = d.current;
    const cur = d.environments.find(e => e.name === d.current);
    document.getElementById('k8s-env-kc').textContent =
      cur && cur.kubeconfig ? 'kubeconfig: ' + cur.kubeconfig : '未配置 kubeconfig';
    updateK8sEnvTag();
    // 恢复 onchange 处理器
    sel.onchange = prevHandler;
    // 环境变化后，若正停留在「Pod YAML」子页则自动刷新 Pod 列表
    const yamlPane = document.getElementById('k8s-sub-yaml');
    if (yamlPane && yamlPane.classList.contains('active')) loadK8sPodList();
  } catch (ex) {
    log('加载环境失败：' + ex.message, 'error');
  }
}

function onK8sEnvChange() {
  state.k8s.env = document.getElementById('k8s-env').value;
  const cur = document.getElementById('k8s-env').selectedOptions[0];
  // kc 标签在 loadK8sEnvs 刷新，这里简单更新
  document.getElementById('k8s-env-kc').textContent = '当前环境：' + state.k8s.env;
  updateK8sEnvTag();
  // 若正停留在 Shell / 文件 子页，刷新 Pod 列表
  const connbar = document.getElementById('k8s-shell-connbar');
  if (connbar && connbar.style.display !== 'none') loadK8sShellPods();
}

// 根据当前环境名渲染带颜色标识的 pill（dev=蓝 / test=橙 / prod=红）
function updateK8sEnvTag() {
  const sel = document.getElementById('k8s-env');
  const tag = document.getElementById('k8s-env-tag');
  const nameEl = document.getElementById('k8s-env-tag-name');
  if (!sel || !tag || !nameEl) return;
  nameEl.textContent = sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : (sel.value || '—');
  const v = (sel.value || '').toLowerCase();
  let cls = 'k8s-env-tag ';
  if (v.includes('prod') || v === 'prd' || v.includes('生产')) cls += 'env-prod';
  else if (v.includes('test') || v.includes('uat') || v.includes('stag') || v.includes('测试') || v.includes('预发')) cls += 'env-test';
  else if (v.includes('dev') || v.includes('开发') || v.includes('development')) cls += 'env-dev';
  else cls += 'env-other';
  tag.className = cls;
}

function openK8sEnvModal() {
  loadK8sEnvList();
  document.getElementById('k8s-env-modal').style.display = 'flex';
}
function closeK8sEnvModal() { document.getElementById('k8s-env-modal').style.display = 'none'; }

async function loadK8sEnvList() {
  try {
    const d = await api('/api/k8s/env');
    const box = document.getElementById('k8s-env-list');
    box.innerHTML = '';
    d.environments.forEach(e => {
      const div = document.createElement('div');
      div.className = 'k8s-env-item';
      div.innerHTML = '<span class="nm">' + esc(e.label) + '</span>'
        + '<span class="nm">(' + esc(e.name) + ')</span>'
        + '<span class="kc">' + esc(e.kubeconfig || '(无 kubeconfig)') + '</span>'
        + (e.is_current ? '<span class="cur">当前</span>' : '');
      div.onclick = () => fillK8sEnvForm(e);
      box.appendChild(div);
    });
  } catch (ex) { log('环境列表失败：' + ex.message, 'error'); }
}

function fillK8sEnvForm(e) {
  document.getElementById('k8s-env-name').value = e.name;
  document.getElementById('k8s-env-label').value = e.label || '';
  document.getElementById('k8s-env-kubeconfig').value = e.kubeconfig || '';
  document.getElementById('k8s-env-context').value = e.context || '';
  document.getElementById('k8s-env-namespace').value = e.namespace || 'default';
  document.getElementById('k8s-env-intranet').value = (e.intranet_hosts || []).join('\n');
}

async function saveK8sEnv() {
  const name = document.getElementById('k8s-env-name').value.trim();
  if (!name) { document.getElementById('k8s-env-msg').textContent = '请填写环境标识'; return; }
  const body = {
    name,
    label: document.getElementById('k8s-env-label').value.trim(),
    kubeconfig: document.getElementById('k8s-env-kubeconfig').value.trim(),
    context: document.getElementById('k8s-env-context').value.trim(),
    namespace: document.getElementById('k8s-env-namespace').value.trim() || 'default',
    intranet_hosts: document.getElementById('k8s-env-intranet').value
      .split('\n').map(s => s.trim()).filter(Boolean),
  };
  try {
    await apiPost('/api/k8s/env', body);
    document.getElementById('k8s-env-msg').textContent = '已保存';
    await loadK8sEnvs();
  } catch (ex) { document.getElementById('k8s-env-msg').textContent = '失败：' + ex.message; }
}

async function switchK8sEnv() {
  const name = document.getElementById('k8s-env-name').value.trim();
  if (!name) { document.getElementById('k8s-env-msg').textContent = '请先填写环境标识'; return; }
  try {
    await apiPost('/api/k8s/env/switch', { name });
    document.getElementById('k8s-env-msg').textContent = '已切换';
    await loadK8sEnvs();
  } catch (ex) { document.getElementById('k8s-env-msg').textContent = '失败：' + ex.message; }
}

async function deleteK8sEnv() {
  const name = document.getElementById('k8s-env-name').value.trim();
  if (!name) return;
  try {
    await apiPost('/api/k8s/env/delete', { name });
    document.getElementById('k8s-env-msg').textContent = '已删除';
    await loadK8sEnvs();
  } catch (ex) { document.getElementById('k8s-env-msg').textContent = '失败：' + ex.message; }
}

// ===== K8s 子标签切换 =====
function switchK8sSub(tab) {
  document.querySelectorAll('.k8s-subtab').forEach(t =>
    t.classList.toggle('active', t.dataset.sub === tab));
  document.querySelectorAll('.k8s-subpane').forEach(p =>
    p.classList.toggle('active', p.id === 'k8s-sub-' + tab));
  // 离开子页时停掉自动刷新定时器，避免后台空跑
  clearK8sAutoTimers();
  // 离开 Shell 子页时断开 WebSocket，避免悬挂连接
  if (tab !== 'shell' && _shellWs) k8sShellDisconnect();
  // 共享连接栏：仅 Shell / 文件 子页显示
  const connbar = document.getElementById('k8s-shell-connbar');
  if (connbar) connbar.style.display = (tab === 'shell' || tab === 'files') ? '' : 'none';
  // 进入子页时按需自动载入
  if (tab === 'yaml') {
    loadK8sPodList();
  } else if (tab === 'events') {
    loadK8sEvents();
  } else if (tab === 'top') {
    loadK8sTop();
  } else if (tab === 'shell' || tab === 'files') {
    loadK8sShellPods();
    if (tab === 'files') {
      renderK8sBreadcrumb(state.k8s.files.path || '/');
      const pod = document.getElementById('k8s-shell-pod');
      if (pod && pod.value) k8sFilesList(state.k8s.files.path || '/');
    }
  }
}

// ===== Pod YAML 管理 =====
async function getK8sYaml() {
  const name = document.getElementById('k8s-yaml-name').value.trim();
  if (!name) { document.getElementById('k8s-yaml-msg').textContent = '请填写资源名称'; return; }
  document.getElementById('k8s-yaml-msg').textContent = '获取中…';
  try {
    const d = await apiPost('/api/k8s/yaml', {
      env: state.k8s.env,
      kind: document.getElementById('k8s-yaml-kind').value,
      name,
      namespace: document.getElementById('k8s-yaml-ns').value.trim(),
      action: 'get',
      clean: document.getElementById('k8s-yaml-clean').checked,
    });
    if (!d.ok) { document.getElementById('k8s-yaml-msg').textContent = '失败：' + d.error; return; }
    document.getElementById('k8s-yaml-editor').value = d.yaml;
    document.getElementById('k8s-yaml-out').style.display = 'none';
    document.getElementById('k8s-yaml-msg').textContent = '已获取 ' + name + (d.yaml.includes('status:') ? '' : '（已清洗）');
  } catch (ex) { document.getElementById('k8s-yaml-msg').textContent = '失败：' + ex.message; }
}

// 载入当前环境 Pod 列表，供「自动获取」下拉选择
async function loadK8sPodList() {
  const sel = document.getElementById('k8s-yaml-podlist');
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const ns = encodeURIComponent(document.getElementById('k8s-yaml-ns').value.trim());
    const d = await api(`/api/k8s/pods?env=${encodeURIComponent(state.k8s.env)}&namespace=${ns}`);
    if (!d.ok) { sel.innerHTML = '<option value="">加载失败：' + esc(d.error) + '</option>'; return; }
    const pods = d.pods || [];
    if (!pods.length) { sel.innerHTML = '<option value="">（无 Pod）</option>'; return; }
    sel.innerHTML = '<option value="">— 选择 Pod 自动获取 YAML —</option>' +
      pods.map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.phase || '')} · 重启${p.restarts || 0}</option>`).join('');
  } catch (ex) {
    sel.innerHTML = '<option value="">加载失败：' + esc(ex.message) + '</option>';
  }
}

// 从下拉选择某 Pod 后，自动填表并获取其 YAML
function onK8sPodSelected() {
  const sel = document.getElementById('k8s-yaml-podlist');
  const name = sel.value;
  if (!name) return;
  document.getElementById('k8s-yaml-kind').value = 'pod';
  document.getElementById('k8s-yaml-name').value = name;
  getK8sYaml();
}

async function applyK8sYaml() {
  const content = document.getElementById('k8s-yaml-editor').value;
  if (!content.trim()) { document.getElementById('k8s-yaml-msg').textContent = '内容为空'; return; }
  document.getElementById('k8s-yaml-msg').textContent = '上传中…';
  try {
    const d = await apiPost('/api/k8s/yaml', {
      env: state.k8s.env,
      kind: document.getElementById('k8s-yaml-kind').value,
      name: document.getElementById('k8s-yaml-name').value.trim(),
      namespace: document.getElementById('k8s-yaml-ns').value.trim(),
      content,
      action: 'apply',
    });
    if (!d.ok) { document.getElementById('k8s-yaml-msg').textContent = '失败：' + d.error; return; }
    const out = document.getElementById('k8s-yaml-out');
    out.style.display = '';
    out.textContent = (d.stdout || '') + (d.stderr ? '\n' + d.stderr : '');
    document.getElementById('k8s-yaml-msg').textContent = '✅ 上传成功';
  } catch (ex) { document.getElementById('k8s-yaml-msg').textContent = '失败：' + ex.message; }
}

// ===== 网络检测 =====
async function runK8sNet() {
  const hosts = document.getElementById('k8s-net-hosts').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const prog = document.getElementById('k8s-net-progress');
  prog.style.display = '';
  document.getElementById('k8s-net-checks').innerHTML = '检测中…';
  document.getElementById('k8s-net-intranet').innerHTML = '';
  try {
    const d = await apiPost('/api/k8s/network', { env: state.k8s.env, extra_hosts: hosts });
    if (!d.ok) { document.getElementById('k8s-net-summary').textContent = '失败：' + d.error; return; }
    renderK8sNet(d);
  } catch (ex) { document.getElementById('k8s-net-summary').textContent = '失败：' + ex.message; }
  finally { prog.style.display = 'none'; }
}

function renderK8sNet(d) {
  document.getElementById('k8s-net-summary').textContent = d.summary;
  const box = document.getElementById('k8s-net-checks');
  box.innerHTML = '';
  (d.checks || []).forEach(c => {
    const ico = c.status === 'ok' ? '✓' : (c.status === 'fail' ? '✕' : '!');
    const div = document.createElement('div');
    div.className = 'k8s-check';
    div.innerHTML = '<div class="k8s-chk-ico ' + c.status + '">' + ico + '</div>'
      + '<div><div class="k8s-chk-name">' + esc(c.name) + '</div>'
      + '<div class="k8s-chk-detail">' + esc(c.detail) + '</div></div>';
    box.appendChild(div);
  });
  const it = document.getElementById('k8s-net-intranet');
  it.innerHTML = '';
  (d.intranet || []).forEach(r => {
    const ok = r.ok;
    const div = document.createElement('div');
    div.className = 'k8s-check';
    div.innerHTML = '<div class="k8s-chk-ico ' + (ok ? 'ok' : 'fail') + '">' + (ok ? '✓' : '✕') + '</div>'
      + '<div><div class="k8s-chk-name">' + esc(r.target) + '</div>'
      + '<div class="k8s-chk-detail">' + (ok ? ('可达 (延迟 ' + r.ms + 'ms)') : '不可达') + '</div></div>';
    it.appendChild(div);
  });
  document.getElementById('k8s-net-verdict').textContent = d.cluster_ok
    ? '判定：当前可连接该环境集群与内网，可正常运维。'
    : '判定：未连通集群（可能未接入对应内网/VPN 或 kubeconfig 缺失）。请确认后重试。';
}

// ===== K8s 事件流 / 资源 Top / 描述 =====
let _k8sEvTimer = null;
let _k8sTopTimer = null;
function clearK8sAutoTimers() {
  if (_k8sEvTimer) { clearInterval(_k8sEvTimer); _k8sEvTimer = null; }
  if (_k8sTopTimer) { clearInterval(_k8sTopTimer); _k8sTopTimer = null; }
}

// 点击「刷新」时启动（带自动刷新）；进入子页时只加载一次（不带定时器）
function startK8sAuto(which) {
  if (which === 'events') {
    if (_k8sEvTimer) { clearInterval(_k8sEvTimer); _k8sEvTimer = null; }
    loadK8sEvents();
    if (document.getElementById('k8s-ev-auto').checked) {
      _k8sEvTimer = setInterval(loadK8sEvents, 10000);
    }
  } else {
    if (_k8sTopTimer) { clearInterval(_k8sTopTimer); _k8sTopTimer = null; }
    loadK8sTop();
    if (document.getElementById('k8s-top-auto').checked) {
      _k8sTopTimer = setInterval(loadK8sTop, 10000);
    }
  }
}

async function loadK8sEvents() {
  const ns = document.getElementById('k8s-ev-ns').value.trim();
  const kind = document.getElementById('k8s-ev-kind').value.trim();
  const name = document.getElementById('k8s-ev-name').value.trim();
  const limit = parseInt(document.getElementById('k8s-ev-limit').value) || 200;
  const allNs = document.getElementById('k8s-ev-allns').checked;
  const summary = document.getElementById('k8s-ev-summary');
  summary.textContent = '加载中…';
  try {
    const q = new URLSearchParams({ env: state.k8s.env, limit });
    if (ns) q.set('namespace', ns);
    if (kind) q.set('kind', kind);
    if (name) q.set('name', name);
    if (allNs) q.set('all_ns', '1');
    const d = await api('/api/k8s/events?' + q.toString());
    if (!d.ok) { summary.textContent = '失败：' + d.error; return; }
    renderK8sEvents(d);
    summary.textContent = '共 ' + d.total + ' 条' + (d.warning ? ' · ⚠ Warning ' + d.warning : '');
  } catch (ex) {
    summary.textContent = '失败：' + ex.message;
  }
}

function renderK8sEvents(d) {
  const tb = document.getElementById('k8s-ev-tbody');
  const evs = d.events || [];
  if (!evs.length) {
    tb.innerHTML = '<tr><td colspan="7" class="empty-hint">无事件</td></tr>';
    return;
  }
  document.getElementById('k8s-ev-hint').textContent = evs.length + ' 条';
  tb.innerHTML = '';
  for (const e of evs) {
    const tr = document.createElement('tr');
    tr.className = 'ev-' + (e.type || 'Normal');
    const time = (e.last_seen || '').replace('T', ' ').replace('Z', '').slice(0, 19);
    const obj = e.object_kind + '/' + e.object_name + (e.object_ns ? ' (' + e.object_ns + ')' : '');
    tr.innerHTML =
      '<td class="ev-time">' + esc(time) + '</td>' +
      '<td class="ev-type">' + esc(e.type || '') + '</td>' +
      '<td class="ev-reason">' + esc(e.reason || '') + '</td>' +
      '<td class="ev-obj" title="' + esc(obj) + '">' + esc(obj) + '</td>' +
      '<td>' + esc(e.source || '') + '</td>' +
      '<td class="ev-count">' + esc(String(e.count || 1)) + '</td>' +
      '<td class="ev-msg" title="' + esc(e.message || '') + '">' + esc(e.message || '') + '</td>';
    tb.appendChild(tr);
  }
}

async function loadK8sTop() {
  const scope = document.getElementById('k8s-top-scope').value;
  const ns = document.getElementById('k8s-top-ns').value.trim();
  const summary = document.getElementById('k8s-top-summary');
  const title = document.getElementById('k8s-top-title');
  // 切换范围时显隐命名空间输入（Node 无命名空间概念）
  document.getElementById('k8s-top-ns-col').style.display = scope === 'nodes' ? 'none' : '';
  title.textContent = scope === 'nodes' ? 'Node 消耗' : 'Pod 消耗';
  summary.textContent = '加载中…';
  try {
    const q = new URLSearchParams({ env: state.k8s.env, scope });
    if (ns && scope === 'pods') q.set('namespace', ns);
    const d = await api('/api/k8s/top?' + q.toString());
    if (!d.ok) { summary.textContent = '失败：' + d.error; return; }
    renderK8sTop(d);
    summary.textContent = '共 ' + (d.rows || []).length + ' 个';
  } catch (ex) {
    summary.textContent = '失败：' + ex.message;
  }
}

function renderK8sTop(d) {
  const thead = document.getElementById('k8s-top-thead');
  const tb = document.getElementById('k8s-top-tbody');
  const rows = d.rows || [];
  const scope = d.scope || 'pods';
  if (scope === 'nodes') {
    thead.innerHTML = '<tr><th>节点</th><th>CPU</th><th>CPU%</th><th>内存</th><th>内存%</th></tr>';
  } else {
    thead.innerHTML = '<tr><th>Pod</th><th>命名空间</th><th>CPU</th><th>内存</th></tr>';
  }
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty-hint">无数据（集群需启用 metrics-server）</td></tr>';
    return;
  }
  // 条形占比：相对列表内最大值
  const maxCpu = Math.max(1e-9, ...rows.map(r => _parseTopVal(r.cpu)));
  const maxMem = Math.max(1e-9, ...rows.map(r => _parseTopVal(r.memory)));
  tb.innerHTML = '';
  for (const r of rows) {
    const tr = document.createElement('tr');
    const cpuBar = '<div class="k8s-top-bar"><i style="width:' + Math.round(_parseTopVal(r.cpu) / maxCpu * 100) + '%"></i></div>';
    const memBar = '<div class="k8s-top-bar mem"><i style="width:' + Math.round(_parseTopVal(r.memory) / maxMem * 100) + '%"></i></div>';
    if (scope === 'nodes') {
      tr.innerHTML =
        '<td class="top-name" title="' + esc(r.name) + '">' + esc(r.name) + '</td>' +
        '<td class="top-cell"><span class="top-val">' + esc(r.cpu) + '</span>' + cpuBar + '</td>' +
        '<td class="top-pct">' + esc(r.cpu_pct || '') + '</td>' +
        '<td class="top-cell"><span class="top-val">' + esc(r.memory) + '</span>' + memBar + '</td>' +
        '<td class="top-pct">' + esc(r.memory_pct || '') + '</td>';
    } else {
      tr.innerHTML =
        '<td class="top-name" title="' + esc(r.name) + '">' + esc(r.name) + '</td>' +
        '<td>' + esc(r.namespace || '') + '</td>' +
        '<td class="top-cell"><span class="top-val">' + esc(r.cpu) + '</span>' + cpuBar + '</td>' +
        '<td class="top-cell"><span class="top-val">' + esc(r.memory) + '</span>' + memBar + '</td>';
    }
    tb.appendChild(tr);
  }
}

function _parseTopVal(s) {
  s = (s || '').trim();
  if (!s || s === '?') return 0;
  if (s.endsWith('m')) { const v = parseFloat(s.slice(0, -1)); return isNaN(v) ? 0 : v / 1000; }
  const m = s.match(/^([\d.]+)(Ki|Mi|Gi|Ti|K|M|G|T|i|n)?$/);
  if (!m) { const v = parseFloat(s); return isNaN(v) ? 0 : v; }
  const val = parseFloat(m[1]);
  const unit = m[2] || '';
  const mult = { n: 1e-9, Ki: 1 / 1024, Mi: 1, Gi: 1024, Ti: 1048576, K: 1e-6, M: 1e-3, G: 1, T: 1e3, i: 1 };
  return val * (mult[unit] || 1);
}

function openK8sDescribe(kind, name, ns) {
  if (kind) document.getElementById('k8s-describe-kind').value = kind;
  if (name) document.getElementById('k8s-describe-name').value = name;
  if (ns !== undefined) document.getElementById('k8s-describe-ns').value = ns || '';
  document.getElementById('k8s-describe-modal').style.display = 'flex';
  runK8sDescribe();
}
function closeK8sDescribeModal() {
  document.getElementById('k8s-describe-modal').style.display = 'none';
}
async function runK8sDescribe() {
  const kind = document.getElementById('k8s-describe-kind').value.trim();
  const name = document.getElementById('k8s-describe-name').value.trim();
  const ns = document.getElementById('k8s-describe-ns').value.trim();
  const msg = document.getElementById('k8s-describe-msg');
  const txt = document.getElementById('k8s-describe-text');
  if (!kind || !name) { msg.textContent = '请填写资源类型与名称'; return; }
  msg.textContent = '描述中…';
  txt.textContent = '';
  document.getElementById('k8s-describe-events').innerHTML = '';
  try {
    const q = new URLSearchParams({ env: state.k8s.env, kind, name });
    if (ns) q.set('namespace', ns);
    const d = await api('/api/k8s/describe?' + q.toString());
    if (!d.ok) { msg.textContent = '失败：' + d.error; return; }
    txt.textContent = d.text || '(无输出)';
    renderK8sDescribeEvents(d.events || []);
    msg.textContent = '✅ 已描述 ' + kind + '/' + name;
  } catch (ex) {
    msg.textContent = '失败：' + ex.message;
  }
}
function renderK8sDescribeEvents(evs) {
  const box = document.getElementById('k8s-describe-events');
  box.innerHTML = '';
  if (!evs.length) { box.innerHTML = '<div class="empty-hint">该资源无相关事件</div>'; return; }
  evs.slice(0, 30).forEach(e => {
    const ico = e.type === 'Warning' ? '✕' : '✓';
    const div = document.createElement('div');
    div.className = 'k8s-check';
    div.innerHTML = '<div class="k8s-chk-ico ' + (e.type === 'Warning' ? 'fail' : 'ok') + '">' + ico + '</div>'
      + '<div><div class="k8s-chk-name">' + esc(e.reason || '') + (e.count > 1 ? ' ×' + e.count : '') + '</div>'
      + '<div class="k8s-chk-detail">' + esc(e.message || '') + '</div></div>';
    box.appendChild(div);
  });
}

// ===== Shell 终端（WebSocket）/ 文件浏览器（REST） =====
// 共享连接栏的当前目标（env/pod/container/namespace）
function k8sTarget() {
  return {
    env: state.k8s.env,
    pod: document.getElementById('k8s-shell-pod')?.value || '',
    container: document.getElementById('k8s-shell-container')?.value || '',
    namespace: document.getElementById('k8s-shell-ns')?.value.trim() || '',
  };
}

// 填充共享连接栏的 Pod 下拉（进入 shell/files 子页时自动调用）
async function loadK8sShellPods() {
  const sel = document.getElementById('k8s-shell-pod');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">加载中…</option>';
  try {
    const ns = encodeURIComponent(document.getElementById('k8s-shell-ns').value.trim());
    const d = await api(`/api/k8s/pods?env=${encodeURIComponent(state.k8s.env)}&namespace=${ns}`);
    if (!d.ok) { sel.innerHTML = '<option value="">加载失败：' + esc(d.error) + '</option>'; return; }
    const pods = d.pods || [];
    if (!pods.length) { sel.innerHTML = '<option value="">（无 Pod）</option>'; return; }
    sel.innerHTML = '<option value="">— 选择 Pod —</option>' +
      pods.map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${esc(p.phase || '')}</option>`).join('');
    if (prev && pods.some(p => p.name === prev)) sel.value = prev;
  } catch (ex) {
    sel.innerHTML = '<option value="">加载失败：' + esc(ex.message) + '</option>';
  }
}

// Pod 变更：填充容器列表；若 Shell 已连接则断开旧连接
async function onK8sShellPodChange() {
  const pod = document.getElementById('k8s-shell-pod').value;
  const csel = document.getElementById('k8s-shell-container');
  if (_shellWs) k8sShellDisconnect();
  csel.innerHTML = '<option value="">（默认容器）</option>';
  if (!pod) return;
  try {
    const q = new URLSearchParams({ name: pod, env: state.k8s.env });
    const d = await api(`/api/k8s/pod-containers?${q.toString()}`);
    const cs = d.containers || [];
    if (cs.length) {
      csel.innerHTML = '<option value="">（默认容器）</option>' +
        cs.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    }
  } catch (ex) { /* 容器列表加载失败不阻断使用 */ }
}

// ---------- Shell 终端 ----------
let _shellWs = null;

function k8sShellConnect() {
  const t = k8sTarget();
  if (!t.env) { toast('请先选择环境（顶部「环境」）', 'warn'); return; }
  if (!t.pod) { toast('请先选择 Pod', 'warn'); return; }
  if (_shellWs) { try { _shellWs.close(); } catch (_) {} }
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  const q = new URLSearchParams({ env: t.env, pod: t.pod });
  if (t.container) q.set('container', t.container);
  if (t.namespace) q.set('namespace', t.namespace);
  let ws;
  try {
    ws = new WebSocket(proto + location.host + '/ws/k8s/exec?' + q.toString());
  } catch (ex) { appendShellLine('无法建立连接：' + ex.message, 'err'); return; }
  _shellWs = ws;
  const status = document.getElementById('k8s-shell-status');
  if (status) { status.className = 'k8s-conn-status off'; status.textContent = '连接中…'; }
  appendShellLine('正在连接 ' + t.pod + (t.container ? '/' + t.container : '') + ' …', 'sys');
  ws.onopen = () => {};
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { appendShellText(ev.data); return; }
    if (m.type === 'ready') {
      state.k8s.shell.cwd = m.cwd || '/';
      state.k8s.shell.connected = true;
      setShellConnected(true);
      updateShellPrompt();
      appendShellLine('已连接 · 工作目录 ' + (m.cwd || '/'), 'sys');
    } else if (m.type === 'output') {
      appendShellText(m.data || '');
    } else if (m.type === 'cwd') {
      state.k8s.shell.cwd = m.cwd || state.k8s.shell.cwd;
      updateShellPrompt();
    } else if (m.type === 'error') {
      appendShellLine('错误：' + (m.msg || ''), 'err');
    }
  };
  ws.onclose = () => {
    state.k8s.shell.connected = false;
    setShellConnected(false);
    appendShellLine('— 连接已关闭 —', 'sys');
    if (_shellWs === ws) _shellWs = null;
  };
  ws.onerror = () => { appendShellLine('WebSocket 连接错误', 'err'); };
}

function k8sShellSend() {
  const input = document.getElementById('k8s-shell-input');
  const val = input.value;
  if (!_shellWs || _shellWs.readyState !== WebSocket.OPEN) return;
  _shellWs.send(JSON.stringify({ type: 'cmd', data: val }));
  appendShellLine((state.k8s.shell.cwd || '/') + ' $ ' + val, 'cmd');
  if (val.trim()) {
    state.k8s.shell.history.push(val);
    state.k8s.shell.histIdx = state.k8s.shell.history.length;
  }
  input.value = '';
}

function k8sShellKey(e) {
  const st = state.k8s.shell;
  if (e.key === 'Enter') { k8sShellSend(); }
  else if (e.key === 'ArrowUp') {
    if (st.history.length && st.histIdx > 0) { st.histIdx--; e.target.value = st.history[st.histIdx] || ''; }
    e.preventDefault();
  } else if (e.key === 'ArrowDown') {
    if (st.histIdx < st.history.length - 1) { st.histIdx++; e.target.value = st.history[st.histIdx] || ''; }
    else { st.histIdx = st.history.length; e.target.value = ''; }
    e.preventDefault();
  }
}

function k8sShellDisconnect() {
  if (_shellWs) {
    try { _shellWs.send(JSON.stringify({ type: 'disconnect' })); } catch (_) {}
    try { _shellWs.close(); } catch (_) {}
    _shellWs = null;
  }
  state.k8s.shell.connected = false;
  setShellConnected(false);
}

function setShellConnected(on) {
  const status = document.getElementById('k8s-shell-status');
  const input = document.getElementById('k8s-shell-input');
  const btnC = document.getElementById('k8s-shell-connect');
  const btnD = document.getElementById('k8s-shell-disconnect');
  if (status) { status.className = 'k8s-conn-status ' + (on ? 'on' : 'off'); status.textContent = on ? '已连接' : '未连接'; }
  if (input) input.disabled = !on;
  if (btnC) btnC.disabled = on;
  if (btnD) btnD.disabled = !on;
}

function updateShellPrompt() {
  const p = document.getElementById('k8s-shell-prompt');
  if (p) p.textContent = (state.k8s.shell.cwd || '/') + ' $ ';
}

function appendShellText(t) {
  const out = document.getElementById('k8s-shell-out');
  if (!out) return;
  const div = document.createElement('div');
  div.className = 'k8s-shell-line out';
  div.textContent = t;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
  while (out.children.length > 4000) out.removeChild(out.firstChild);
}
function appendShellLine(t, kind) {
  const out = document.getElementById('k8s-shell-out');
  if (!out) return;
  const div = document.createElement('div');
  div.className = 'k8s-shell-line ' + (kind || 'sys');
  div.textContent = t;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
  while (out.children.length > 4000) out.removeChild(out.firstChild);
}

// ---------- 文件浏览器 ----------
function k8sPathJoin(base, name) {
  if (!base || base === '/') return '/' + name;
  if (base.endsWith('/')) return base + name;
  return base + '/' + name;
}
function k8sPathParent(p) {
  if (!p || p === '/') return '/';
  let s = p.endsWith('/') ? p.slice(0, -1) : p;
  const i = s.lastIndexOf('/');
  return i <= 0 ? '/' : s.slice(0, i);
}

function renderK8sBreadcrumb(path) {
  const el = document.getElementById('k8s-files-path');
  if (!el) return;
  el.innerHTML = '';
  const root = document.createElement('span');
  root.className = 'k8s-files-crumb';
  root.textContent = '📁 /';
  root.onclick = () => k8sFilesList('/');
  el.appendChild(root);
  const parts = (path || '/').split('/').filter(Boolean);
  let acc = '';
  parts.forEach(p => {
    const sep = document.createElement('span');
    sep.className = 'k8s-files-sep'; sep.textContent = '/';
    el.appendChild(sep);
    acc += '/' + p;
    const crumb = document.createElement('span');
    crumb.className = 'k8s-files-crumb';
    crumb.textContent = p;
    const segPath = acc;
    crumb.onclick = () => k8sFilesList(segPath);
    el.appendChild(crumb);
  });
}

async function k8sFilesList(path) {
  const t = k8sTarget();
  if (!t.pod) {
    const tb = document.getElementById('k8s-files-list');
    tb.innerHTML = '<tr><td colspan="4" class="empty-hint">请先在上方选择 Pod / 容器</td></tr>';
    return;
  }
  if (path !== undefined) state.k8s.files.path = path;
  const p = state.k8s.files.path || '/';
  renderK8sBreadcrumb(p);
  const tb = document.getElementById('k8s-files-list');
  tb.innerHTML = '<tr><td colspan="4" class="empty-hint">加载中…</td></tr>';
  try {
    const d = await apiPost('/api/k8s/file/list', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace, path: p,
    });
    if (!d.ok) { tb.innerHTML = '<tr><td colspan="4" class="empty-hint">失败：' + esc(d.error) + '</td></tr>'; return; }
    state.k8s.files.entries = d.entries || [];
    renderK8sFiles(d.entries || []);
  } catch (ex) {
    tb.innerHTML = '<tr><td colspan="4" class="empty-hint">失败：' + esc(ex.message) + '</td></tr>';
  }
}

function renderK8sFiles(entries) {
  const tb = document.getElementById('k8s-files-list');
  tb.innerHTML = '';
  const rows = (entries || []).slice().sort((a, b) => {
    const ad = a.type === 'dir' ? 0 : 1, bd = b.type === 'dir' ? 0 : 1;
    return ad - bd || String(a.name).localeCompare(String(b.name));
  });
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="4" class="empty-hint">空目录</td></tr>';
    return;
  }
  for (const e of rows) {
    const isDir = e.type === 'dir';
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="k8s-files-name"><span class="k8s-files-icon">' + (isDir ? '📁' : '📄') + '</span>' + esc(e.name) + '</td>' +
      '<td class="k8s-files-type">' + (isDir ? '目录' : '文件') + '</td>' +
      '<td class="k8s-files-size">' + (isDir ? '—' : fmtSize(e.size)) + '</td>' +
      '<td class="k8s-files-time">' + esc((e.modtime || '').replace('T', ' ').replace('Z', '').slice(0, 19)) + '</td>';
    tr.onclick = () => {
      document.querySelectorAll('#k8s-files-list tr').forEach(r => r.classList.remove('selected'));
      tr.classList.add('selected');
      state.k8s.files.selected = { name: e.name, isDir };
    };
    tr.ondblclick = () => k8sFileOpen(e.name, isDir);
    tb.appendChild(tr);
  }
}

async function k8sFileOpen(name, isDir) {
  const path = k8sPathJoin(state.k8s.files.path, name);
  if (isDir) { k8sFilesList(path); return; }
  const t = k8sTarget();
  try {
    const d = await apiPost('/api/k8s/file/read', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace, path, max_bytes: 200000,
    });
    if (!d.ok) { toast('读取失败：' + d.error, 'error'); return; }
    if (d.is_binary) { toast('这是二进制文件，不支持在线编辑。', 'info'); return; }
    state.k8s.files.editPath = path;
    document.getElementById('k8s-file-edit-area').value = d.content || '';
    document.getElementById('k8s-file-edit-title').textContent =
      '编辑 · ' + name + (d.truncated ? '（已截断，原始文件较大）' : '');
    document.getElementById('k8s-file-edit-msg').textContent = '';
    document.getElementById('k8s-file-edit-modal').style.display = 'flex';
  } catch (ex) { toast('读取失败：' + ex.message, 'error'); }
}

async function k8sFileSave() {
  const path = state.k8s.files.editPath;
  if (!path) return;
  const t = k8sTarget();
  const content = document.getElementById('k8s-file-edit-area').value;
  const msg = document.getElementById('k8s-file-edit-msg');
  msg.textContent = '保存中…';
  try {
    const d = await apiPost('/api/k8s/file/write', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace, path, content,
    });
    if (!d.ok) { msg.textContent = '失败：' + d.error; return; }
    msg.textContent = '✅ 已保存';
    k8sFilesList(state.k8s.files.path || '/');
  } catch (ex) { msg.textContent = '失败：' + ex.message; }
}

function k8sFileDownload() {
  const content = document.getElementById('k8s-file-edit-area').value;
  const path = state.k8s.files.editPath || 'file.txt';
  const name = (path.split('/').pop()) || 'file.txt';
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(a.href);
}

function k8sFileEditClose() {
  document.getElementById('k8s-file-edit-modal').style.display = 'none';
  document.getElementById('k8s-file-edit-msg').textContent = '';
}

async function k8sFilesMkdir() {
  const name = prompt('新建文件夹名称：');
  if (!name) return;
  const t = k8sTarget();
  const path = k8sPathJoin(state.k8s.files.path, name.trim());
  try {
    const d = await apiPost('/api/k8s/file/mkdir', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace, path,
    });
    if (!d.ok) { toast('新建失败：' + d.error, 'error'); return; }
    k8sFilesList(state.k8s.files.path || '/');
  } catch (ex) { toast('新建失败：' + ex.message, 'error'); }
}

function k8sFilesUploadClick() { document.getElementById('k8s-files-fileinput').click(); }
async function k8sFilesUploadChange(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const t = k8sTarget();
  try {
    const data = await fileToBase64(file);
    const d = await apiPost('/api/k8s/file/upload', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace,
      path: state.k8s.files.path || '/', data,
    });
    if (!d.ok) { toast('上传失败：' + d.error, 'error'); return; }
    k8sFilesList(state.k8s.files.path || '/');
  } catch (ex) { toast('上传失败：' + ex.message, 'error'); }
  e.target.value = '';  // 允许重复上传同名文件
}

async function k8sFilesDelete() {
  const sel = state.k8s.files.selected;
  if (!sel) { toast('请先单击选中要删除的文件 / 目录', 'warn'); return; }
  if (!confirm('确认删除 ' + (sel.isDir ? '目录' : '文件') + '「' + sel.name + '」？此操作不可恢复。')) return;
  const t = k8sTarget();
  const path = k8sPathJoin(state.k8s.files.path, sel.name);
  try {
    const d = await apiPost('/api/k8s/file/delete', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace, path, is_dir: sel.isDir,
    });
    if (!d.ok) { toast('删除失败：' + d.error, 'error'); return; }
    state.k8s.files.selected = null;
    k8sFilesList(state.k8s.files.path || '/');
  } catch (ex) { toast('删除失败：' + ex.message, 'error'); }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve((r.result || '').split(',')[1] || '');
    r.onerror = () => reject(r.error || new Error('读取文件失败'));
    r.readAsDataURL(file);
  });
}

function k8sFilesUp() { k8sFilesList(k8sPathParent(state.k8s.files.path || '/')); }
function k8sFilesRefresh() { k8sFilesList(state.k8s.files.path || '/'); }

// ===== 差异进度条控制 =====
function setDiffProgress(opts) {
  const wrap = document.getElementById('diff-progress');
  const stageEl = document.getElementById('diff-progress-stage');
  const detailEl = document.getElementById('diff-progress-detail');
  const bar = document.getElementById('diff-progress-bar');
  const fill = bar.querySelector('.diff-progress-fill');
  const pctEl = document.getElementById('diff-progress-pct');
  const row = stageEl.parentElement;

  if (opts.visible === false) { wrap.style.display = 'none'; return; }
  if (opts.visible === true) wrap.style.display = '';

  if (opts.stage !== undefined) stageEl.textContent = opts.stage;
  if (opts.detail !== undefined) detailEl.textContent = opts.detail;

  // 重置状态类
  row.classList.remove('done', 'error');
  bar.classList.remove('indeterminate', 'done', 'error');

  if (opts.mode === 'indeterminate') {
    bar.classList.add('indeterminate');
    pctEl.textContent = '';
  } else if (opts.mode === 'done') {
    row.classList.add('done');
    bar.classList.add('done');
    fill.style.width = '100%';
    pctEl.textContent = '100%';
  } else if (opts.mode === 'error') {
    row.classList.add('error');
    bar.classList.add('error');
    fill.style.width = '100%';
    pctEl.textContent = '';
  } else if (opts.mode === 'determinate') {
    const pct = Math.max(0, Math.min(100, opts.pct || 0));
    fill.style.width = pct + '%';
    pctEl.textContent = pct + '%';
  }
}

function addDiffError(msg) {
  const box = document.getElementById('diff-error-box');
  box.style.display = '';
  const line = document.createElement('div');
  line.className = 'err-line';
  line.textContent = msg;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function clearDiffErrors() {
  const box = document.getElementById('diff-error-box');
  box.innerHTML = '';
  box.style.display = 'none';
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

// ===== 连接设置弹窗 =====
function openConnectModal() {
  document.getElementById('connect-modal').style.display = '';
  const statusEl = document.getElementById('connect-status');
  statusEl.textContent = '';
  statusEl.style.color = '';
  // 从后端拉当前值（含 session.json 中的 Cookie）
  api('/api/status').then(s => {
    document.getElementById('cfg-url').value = s.jira_url || '';
    document.getElementById('cfg-user').value = s.username || '';
    document.querySelector(`input[name="mode"][value="${s.mode || 'pat'}"]`).checked = true;
    document.getElementById('cfg-repo-id').value = s.repo_id || '';
    document.getElementById('cfg-branch').value = s.branch || '';
    document.getElementById('cfg-repo-name').value = s.repo_name || '';
    // Cookie 已从 session.json 加载到后端，但不在 /api/status 明文返回（安全）
    // 如果 cookie_set=true 且来源是 session，提示用户
    if (s.cookie_set && s.cookie_source === 'session') {
      statusEl.textContent = '已从本地读取上次保存的 Cookie（如需更新请重新粘贴）';
    }
    onModeChange();
  });
}

function closeConnectModal() {
  document.getElementById('connect-modal').style.display = 'none';
}

function onModeChange() {
  const pat = document.querySelector('input[name="mode"]:checked').value === 'pat';
  document.getElementById('pat-group').style.display = pat ? '' : 'none';
  document.getElementById('cookie-group').style.display = pat ? 'none' : '';
}

function getConnectConfig() {
  return {
    jira_url: document.getElementById('cfg-url').value.trim(),
    username: document.getElementById('cfg-user').value.trim(),
    mode: document.querySelector('input[name="mode"]:checked').value,
    pat: document.getElementById('cfg-pat').value.trim(),
    cookie: document.getElementById('cfg-cookie').value.trim(),
    repo_id: document.getElementById('cfg-repo-id').value.trim(),
    repo_name: document.getElementById('cfg-repo-name').value.trim(),
    branch: document.getElementById('cfg-branch').value.trim(),
  };
}

async function testConnect() {
  const cfg = getConnectConfig();
  const btn = document.getElementById('btn-test-connect');
  btn.disabled = true;
  btn.innerHTML = '<span class="caction-sparkle">⏳</span>测试中…';
  const statusEl = document.getElementById('connect-status');
  statusEl.textContent = '测试中…（PAT 模式会触发真实克隆，可能耗时）';
  statusEl.style.color = '';
  try {
    const res = await apiPost('/api/connect', cfg);
    const parts = [];
    parts.push(res.cookieOk ? 'Cookie ✓' : 'Cookie ✗');
    if (res.patTest) parts.push(`PAT ${res.patTest.ok ? '✓' : '✗'}: ${res.patTest.msg}`);
    if (res.repoDefaults?.displayName) {
      document.getElementById('cfg-repo-name').value = res.repoDefaults.displayName;
      parts.push(`仓库名已探测: ${res.repoDefaults.displayName}`);
    }
    if (res.note) parts.push(res.note);
    // Cookie 持久化反馈
    if (cfg.mode === 'cookie' && cfg.cookie) {
      if (res.cookieSaved) {
        parts.push('Cookie 已保存到本地，下次启动自动读取');
      } else if (res.cookieWarning) {
        parts.push(res.cookieWarning);
        statusEl.style.color = 'var(--danger)';
      }
    }
    statusEl.textContent = parts.join(' | ');
  } catch (ex) {
    statusEl.textContent = `错误：${ex.message}`;
    statusEl.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="caction-sparkle">⚡</span>测试连接';
  }
}

async function applyConnect() {
  const cfg = getConnectConfig();
  try {
    await withLoading('#btn-connect-ok', async () => {
      await apiPost('/api/connect', cfg);
    }, {
      loadingText: '保存中…',
      originalText: '确定',
      okToast: '连接配置已更新',
    });
    closeConnectModal();
    updateStatus();
    log('连接配置已更新。');
  } catch (_) {}
}

// ===== 仓库列表 =====
async function discoverRepos() {
  const btn = document.getElementById('btn-discover');
  btn.disabled = true;
  btn.textContent = '发现中…';
  log('【发现仓库】开始…');
  try {
    const res = await api('/api/repos');
    if (res.error) {
      log(`发现仓库错误：${res.error}`, 'warning');
      // Cookie 可能过期
      if (/cookie|登录|login|未配置/i.test(res.error)) {
        log('Cookie 可能已过期，请重新打开「连接设置」获取新 Cookie。', 'error');
      }
    }
    state.repos = res.repos || [];
    renderRepoList();
    log(`【发现仓库】返回 ${state.repos.length} 个`);
  } catch (ex) {
    log(`发现仓库异常：${ex.message}`, 'error');
    toastApiError(ex, { label: '重试', onClick: () => discoverRepos() });
  } finally {
    btn.disabled = false;
    btn.textContent = '发现仓库';
  }
}

function renderRepoList(keyword) {
  const el = document.getElementById('repo-list');
  el.innerHTML = '';
  if (!state.repos.length) {
    el.innerHTML = '<div class="empty-hint">未发现仓库，或该账号无权限</div>';
    return;
  }
  const kw = (keyword || (document.getElementById('repo-search')?.value || '')).trim().toLowerCase();
  let matched = state.repos;
  if (kw) {
    matched = state.repos.filter(r =>
      (r.display_name || '').toLowerCase().includes(kw) ||
      String(r.repo_id).toLowerCase().includes(kw) ||
      (r.default_branch || '').toLowerCase().includes(kw)
    );
  }
  if (!matched.length) {
    el.innerHTML = `<div class="empty-hint">无匹配项（关键字：${esc(kw)}）</div>`;
    return;
  }
  matched.forEach(r => {
    const item = document.createElement('div');
    item.className = 'repo-item';
    item.innerHTML = `
      <div class="repo-name">${esc(r.display_name || r.repo_id)}</div>
      <div class="repo-meta">id=${esc(r.repo_id)}${r.default_branch ? ` [branch=${esc(r.default_branch)}]` : ''}</div>
    `;
    item.onclick = () => openRepo(r, item);
    el.appendChild(item);
  });
}

function onRepoSearch() {
  const kw = document.getElementById('repo-search').value;
  renderRepoList(kw);
}

function selectRepo(r, el) {
  state.selectedRepo = r;
  document.querySelectorAll('.repo-item').forEach(it => it.classList.remove('selected'));
  if (el) el.classList.add('selected');
}

async function openRepo(r, el) {
  selectRepo(r, el);
  const branch = r.default_branch || document.getElementById('inp-branch').value.trim();
  await apiPost('/api/repo/select', {
    repo_id: r.repo_id,
    repo_name: r.display_name,
    branch,
  });
  updateStatus();
  log(`已选择仓库 id=${r.repo_id} name=${r.display_name} branch=${branch || '(默认)'}`);
  loadTree('');
  autoFillLocalDir();
  switchTab('tree');
}

async function loadRepoMappings() {
  // 从后端加载 .env 中 MERGE_REPO_* 的远程仓库 → 本地目录映射
  try {
    const res = await api('/api/diff/repo-mappings');
    state.repoMappings = {};
    (res.mappings || []).forEach(m => {
      if (m.repo_name && m.local_dir) state.repoMappings[m.repo_name] = m.local_dir;
    });
    log(`已加载 ${Object.keys(state.repoMappings).length} 个仓库本地映射`);
  } catch (ex) {
    log(`加载仓库映射失败：${ex.message}`, 'error');
  }
}

function autoFillLocalDir() {
  // 根据当前选中的远程仓库自动填充本地目录（优先 .env 映射）
  if (!state.selectedRepo) return;
  const repoName = state.selectedRepo.display_name;
  if (!repoName) return;
  const input = document.getElementById('diff-local-dir');
  // 若用户已手动填写，则不覆盖；仅当输入为空时自动填充
  if (input.value.trim()) return;
  const mapped = state.repoMappings[repoName];
  if (mapped) {
    input.value = mapped;
    diffState.localDir = mapped;
    log(`已自动填充本地目录：${mapped}`);
    return;
  }
  // 无精确映射时尝试发现候选目录
  discoverLocalDirs(repoName);
}

async function discoverLocalDirs(repoName) {
  // 调用后端发现本地候选目录；若只有一个高置信候选则自动填充
  try {
    const res = await api(`/api/diff/discover-local-dirs?repo_name=${encodeURIComponent(repoName)}`);
    const candidates = res.candidates || [];
    if (candidates.length === 1) {
      document.getElementById('diff-local-dir').value = candidates[0];
      diffState.localDir = candidates[0];
      log(`已自动发现本地目录：${candidates[0]}`);
    } else if (candidates.length > 1) {
      log(`发现 ${candidates.length} 个候选目录，请手动选择：${candidates.join('、')}`, 'warning');
    }
  } catch (ex) {
    log(`自动发现本地目录失败：${ex.message}`, 'error');
  }
}

async function viewFiles() {
  if (!state.selectedRepo) {
    log('请先在列表中选择一个仓库。', 'warning');
    return;
  }
  await openRepo(state.selectedRepo, null);
}

async function loadTreeManual() {
  const rid = document.getElementById('inp-repo-id').value.trim();
  if (!rid) return;
  await apiPost('/api/repo/select', {
    repo_id: rid,
    repo_name: document.getElementById('inp-repo-name').value.trim(),
    branch: document.getElementById('inp-branch').value.trim(),
  });
  updateStatus();
  log(`已手动指定仓库 id=${rid}`);
  loadTree('');
  switchTab('tree');
}

// ===== 文件树 =====
async function loadTree(path) {
  const container = document.getElementById('tree-container');
  if (!path) {
    container.innerHTML = '<div class="tree-loading">加载中…</div>';
    state.treeNodes = {};
    state.checkedPaths.clear();
  }
  try {
    const res = await api(`/api/tree?path=${encodeURIComponent(path)}`);
    if (path) {
      // 子目录加载：把子节点放入 .tree-children 容器（而非父节点 div 本身）
      const parentNode = state.treeNodes[path];
      if (parentNode) {
        parentNode.loaded = true;
        const childrenEl = parentNode.element.querySelector(':scope > .tree-children');
        renderTreeChildren(childrenEl || parentNode.element, res.entries);
      }
    } else {
      container.innerHTML = '';
      renderTreeChildren(container, res.entries);
    }
    if (!path) log(`文件树已加载，共 ${res.entries.length} 项。`);
  } catch (ex) {
    container.innerHTML = `<div class="empty-hint">加载失败：${esc(ex.message)}</div>`;
    log(`加载文件树失败：${ex.message}`, 'error');
  }
}

function renderTreeChildren(container, entries) {
  // 清除加载中占位
  container.querySelectorAll('.tree-loading').forEach(el => el.remove());
  entries.forEach(e => {
    const node = document.createElement('div');
    node.className = 'tree-node';
    const row = document.createElement('div');
    row.className = 'tree-row';

    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle';
    toggle.textContent = e.type === 'dir' ? '▶' : '';

    const icon = document.createElement('span');
    icon.className = 'tree-icon';
    icon.textContent = e.type === 'dir' ? '📁' : '📄';

    const name = document.createElement('span');
    name.className = 'tree-name';
    name.textContent = e.name;

    const size = document.createElement('span');
    size.className = 'tree-size';
    size.textContent = e.size != null ? fmtSize(e.size) : '';

    row.appendChild(toggle);
    row.appendChild(icon);
    row.appendChild(name);
    row.appendChild(size);

    if (e.type === 'file') {
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'tree-checkbox';
      cb.checked = state.checkedPaths.has(e.path);
      cb.onclick = ev => ev.stopPropagation();
      cb.onchange = () => {
        if (cb.checked) state.checkedPaths.add(e.path);
        else state.checkedPaths.delete(e.path);
      };
      row.appendChild(cb);

      row.onclick = () => {
        document.querySelectorAll('.tree-row.selected').forEach(el => el.classList.remove('selected'));
        row.classList.add('selected');
        openFile(e.path);
      };
    } else {
      row.onclick = () => toggleDir(e.path, toggle, node);
    }

    node.appendChild(row);
    container.appendChild(node);
    state.treeNodes[e.path] = { loaded: false, element: node };
  });
}

async function toggleDir(path, toggleEl, nodeEl) {
  const node = state.treeNodes[path];
  if (!node) return;
  if (node.loaded) {
    // 切换展开/折叠
    const children = nodeEl.querySelector('.tree-children');
    if (children) {
      children.style.display = children.style.display === 'none' ? '' : 'none';
      toggleEl.textContent = children.style.display === 'none' ? '▶' : '▼';
    }
    return;
  }
  toggleEl.textContent = '▼';
  const childrenContainer = document.createElement('div');
  childrenContainer.className = 'tree-children';
  childrenContainer.innerHTML = '<div class="tree-loading">加载中…</div>';
  nodeEl.appendChild(childrenContainer);
  await loadTree(path);
}

// ===== 文件预览 =====
// ===== 文件预览 =====
function formatContent(path, content) {
  if (!content) return '';
  // JSON 文件：尝试格式化
  if (/\.json$/i.test(path) || (content.trim().startsWith('{') && content.trim().endsWith('}'))) {
    try {
      return JSON.stringify(JSON.parse(content), null, 2);
    } catch (_) {
      // 解析失败，返回原文
      return content;
    }
  }
  return content;
}

async function openFile(path) {
  state.selectedFile = path;
  document.getElementById('preview-title').textContent = `加载中 · ${path}`;
  document.getElementById('preview-content').textContent = '';
  switchTab('preview');
  try {
    const res = await api(`/api/file?path=${encodeURIComponent(path)}`);
    if (res.error) {
      document.getElementById('preview-title').textContent = '错误';
      document.getElementById('preview-content').textContent = res.error;
    } else {
      const formatted = formatContent(path, res.content || '');
      const isJson = formatted !== (res.content || '');
      document.getElementById('preview-title').textContent =
        `预览 · ${path}${isJson ? '  (JSON 已格式化)' : ''}`;
      document.getElementById('preview-content').textContent = formatted;
    }
  } catch (ex) {
    document.getElementById('preview-title').textContent = '错误';
    document.getElementById('preview-content').textContent = ex.message;
  }
}

// ===== 提交记录 =====
async function queryCommits() {
  const localMode = document.getElementById('commit-mode').value === 'local';
  const issueKey = document.getElementById('commit-issue').value.trim();
  const btn = document.getElementById('btn-query-commits');
  btn.disabled = true;
  btn.textContent = '查询中…';

  try {
    const params = new URLSearchParams();
    if (issueKey) params.set('issue_key', issueKey);
    if (localMode) params.set('local_mode', 'true');
    const res = await api(`/api/commits?${params}`);
    if (res.error) {
      document.getElementById('commit-list').innerHTML = `<div class="empty-hint">${esc(res.error)}</div>`;
      state.commits = [];
    } else {
      state.commits = res.commits || [];
      renderCommitList();
      log(`提交记录：共 ${state.commits.length} 条`);
    }
  } catch (ex) {
    log(`提交查询失败：${ex.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '查询';
  }
}

function renderCommitList() {
  const el = document.getElementById('commit-list');
  el.innerHTML = '';
  if (!state.commits.length) {
    el.innerHTML = '<div class="empty-hint">没有查询到提交记录</div>';
    return;
  }
  state.commits.forEach((c, i) => {
    const item = document.createElement('div');
    item.className = 'commit-item';
    const msg = (c.message || '').split('\n')[0] || '';
    const shortMsg = msg.length > 56 ? msg.slice(0, 55) + '…' : msg;
    item.innerHTML = `
      <div class="commit-msg">${esc(c.display_id)} ${esc(shortMsg)}</div>
      <div class="commit-meta">${esc(c.author || '?')}${c.date ? ' · ' + esc(c.date.slice(0, 10)) : ''}</div>
    `;
    item.onclick = () => selectCommit(i);
    el.appendChild(item);
  });
}

function selectCommit(idx) {
  state.selectedCommit = state.commits[idx];
  document.querySelectorAll('.commit-item').forEach((el, i) => {
    el.classList.toggle('selected', i === idx);
  });
  const c = state.selectedCommit;
  const detail = [
    `commit  ${c.commit_id}`,
    `Author: ${c.author}`,
    `Date:   ${c.date}`,
    `Branch: ${c.branch}${c.repository_name ? `  (repo: ${c.repository_name})` : ''}`,
    '',
    c.message || '',
    '',
    `变更文件（${c.files.length}）：单击文件可查看历史版本`,
  ].join('\n');
  document.getElementById('commit-detail-text').textContent = detail;

  const filesEl = document.getElementById('commit-files');
  filesEl.innerHTML = '';
  const signMap = { ADDED: '+', MODIFIED: 'M', DELETED: 'D', RENAMED: 'R', COPIED: 'C',
                    A: '+', M: 'M', D: 'D', R: 'R', C: 'C' };
  c.files.forEach(f => {
    const sign = signMap[f.change_type?.toUpperCase()] || '?';
    const item = document.createElement('div');
    item.className = 'commit-file-item';
    item.innerHTML = `<span class="change-badge change-${sign}">${sign}</span>${esc(f.path)}`;
    item.onclick = () => openFileAtCommit(c.commit_id, f.path);
    filesEl.appendChild(item);
  });
}

async function openFileAtCommit(commitId, path) {
  document.getElementById('preview-title').textContent = `加载中 · ${path} @ ${commitId.slice(0, 8)}`;
  document.getElementById('preview-content').textContent = '';
  switchTab('preview');
  try {
    const res = await api(`/api/file-at-commit?commit_id=${encodeURIComponent(commitId)}&path=${encodeURIComponent(path)}`);
    if (res.error) {
      document.getElementById('preview-title').textContent = '错误';
      document.getElementById('preview-content').textContent = res.error;
    } else {
      const formatted = formatContent(path, res.content || '');
      const isJson = formatted !== (res.content || '');
      document.getElementById('preview-title').textContent =
        `${path}  (commit ${commitId.slice(0, 8)})${isJson ? '  (JSON 已格式化)' : ''}`;
      document.getElementById('preview-content').textContent = formatted;
    }
  } catch (ex) {
    document.getElementById('preview-title').textContent = '错误';
    document.getElementById('preview-content').textContent = ex.message;
  }
}

// ===== 克隆 / 下载 =====
async function cloneRepo() {
  try {
    await apiPost('/api/clone', {});
    showProgress();
    log('开始克隆仓库…');
  } catch (ex) {
    log(`克隆请求失败：${ex.message}`, 'error');
  }
}

async function downloadSelected() {
  const paths = Array.from(state.checkedPaths);
  if (!paths.length) {
    log('未勾选任何文件。请在文件树勾选要下载的文件。', 'warning');
    return;
  }
  try {
    await apiPost('/api/download', { paths, max_workers: state.maxWorkers });
    showProgress();
    log(`开始下载 ${paths.length} 个文件…`);
  } catch (ex) {
    log(`下载请求失败：${ex.message}`, 'error');
  }
}

async function downloadAll() {
  try {
    await apiPost('/api/download/repo', { max_workers: state.maxWorkers });
    showProgress();
    log('开始递归下载整个仓库…');
  } catch (ex) {
    log(`整库下载请求失败：${ex.message}`, 'error');
  }
}

async function cancelDownload() {
  await apiPost('/api/download/cancel', {});
  log('已请求取消下载。');
}

async function clearResume() {
  try {
    const res = await apiDelete('/api/resume');
    log(res.msg || res.error || '操作完成');
  } catch (ex) {
    log(`清空断点失败：${ex.message}`, 'error');
  }
}

// ===== 速率 / 并发 =====
async function setRate(v) {
  state.qps = v;
  await apiPost('/api/rate-limit', { qps: v });
  updateStatus();
  log(`请求速率上限已设为 ${v} 请求/秒`);
}

function setConcurrency(v) {
  state.maxWorkers = v;
  log(`下载并发数已设为 ${v}`);
}

// ===== 标签页切换 =====
function switchTab(name) {
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
    const showBar = ['repo', 'tree', 'preview', 'commits', 'diff'].includes(name);
    bar.style.display = showBar ? '' : 'none';
  }
}

// ===== 提交模式切换 =====
function onCommitModeChange() {
  const local = document.getElementById('commit-mode').value === 'local';
  document.getElementById('commit-issue-label').textContent = local ? '仓库' : 'Issue';
  document.getElementById('commit-issue').placeholder = local ? '(当前仓库)' : 'TST-234';
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

// ===== 事件绑定 =====
document.getElementById('btn-connect').onclick = openConnectModal;
document.getElementById('connect-close').onclick = closeConnectModal;
document.getElementById('btn-connect-cancel').onclick = closeConnectModal;
document.getElementById('btn-test-connect').onclick = testConnect;
document.getElementById('btn-connect-ok').onclick = applyConnect;
document.getElementById('btn-theme').onclick = toggleTheme;
document.querySelectorAll('input[name="mode"]').forEach(r => r.onchange = onModeChange);

document.getElementById('btn-discover').onclick = discoverRepos;
document.getElementById('btn-view-files').onclick = viewFiles;
document.getElementById('btn-load-tree').onclick = loadTreeManual;
document.getElementById('repo-search').addEventListener('input', onRepoSearch);
// 手动指定区：回车即加载
['inp-repo-id', 'inp-repo-name', 'inp-branch'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') loadTreeManual(); });
});
// 差异对比
document.getElementById('btn-diff-scan').onclick = scanDiff;
document.getElementById('btn-diff-merge-one').onclick = mergeOne;
document.getElementById('btn-diff-merge-all').onclick = mergeAll;
document.getElementById('btn-diff-auto-dir').onclick = autoFillLocalDir;
document.getElementById('chk-show-same').onchange = (e) => {
  diffState.showSame = e.target.checked;
  renderDiffList();
};
document.getElementById('chk-ignore-eol').onchange = (e) => {
  diffState.ignoreLineEndings = e.target.checked;
  // 仅重新扫描才能在后端应用新策略；立即重扫提升体验
  if (diffState.entries.length) scanDiff();
};
document.getElementById('chk-merge-remote-only').onchange = (e) => {
  diffState.mergeRemoteOnly = e.target.checked;
  updateMergeAllButton();
};

document.getElementById('btn-clone').onclick = cloneRepo;
document.getElementById('btn-download').onclick = downloadSelected;
document.getElementById('btn-download-all').onclick = downloadAll;
document.getElementById('btn-cancel').onclick = cancelDownload;
document.getElementById('btn-clear-resume').onclick = clearResume;
document.getElementById('btn-clear-log').onclick = clearLog;
document.getElementById('btn-clear-log-2').onclick = clearLog;

document.getElementById('inp-concurrency').onchange = e => setConcurrency(parseInt(e.target.value) || 4);
document.getElementById('inp-rate').onchange = e => setRate(parseInt(e.target.value) || 6);

document.getElementById('btn-query-commits').onclick = queryCommits;
document.getElementById('commit-issue').addEventListener('keydown', e => {
  if (e.key === 'Enter') queryCommits();
});
document.getElementById('commit-mode').onchange = onCommitModeChange;

document.querySelectorAll('.tab').forEach(t => t.onclick = () => switchTab(t.dataset.tab));

// ===== K8s 按钮绑定 =====
document.getElementById('btn-k8s-run').onclick = runK8s;
document.getElementById('btn-k8s-cancel').onclick = cancelK8s;
document.getElementById('btn-k8s-report').onclick = openK8sReport;
document.getElementById('btn-k8s-dir').onclick = copyK8sDir;
document.getElementById('btn-k8s-log-open').onclick = () => {
  const pod = state.k8s.lastPod;
  if (!pod) { toast('请先在上方选择一个 Pod 查看其日志。', 'warn'); return; }
  const url = '/web/log_viewer.html?pod=' + encodeURIComponent(pod) +
              '&env=' + encodeURIComponent(state.k8s.env || '');
  window.open(url, '_blank');
};

// 环境栏 + 子标签 + 弹窗 + YAML + 网络
document.getElementById('k8s-env').onchange = onK8sEnvChange;
document.getElementById('btn-k8s-env-manage').onclick = openK8sEnvModal;
document.getElementById('btn-k8s-env-close').onclick = closeK8sEnvModal;
document.getElementById('btn-k8s-env-save').onclick = saveK8sEnv;
document.getElementById('btn-k8s-env-switch').onclick = switchK8sEnv;
document.getElementById('btn-k8s-env-delete').onclick = deleteK8sEnv;
document.querySelectorAll('.k8s-subtab').forEach(t =>
  t.onclick = () => switchK8sSub(t.dataset.sub));
document.getElementById('btn-k8s-yaml-get').onclick = getK8sYaml;
document.getElementById('btn-k8s-yaml-apply').onclick = applyK8sYaml;
document.getElementById('btn-k8s-yaml-pods').onclick = loadK8sPodList;
document.getElementById('k8s-yaml-podlist').onchange = onK8sPodSelected;
// 命名空间变化后自动刷新 Pod 列表（防抖 500ms）
let _k8sPodListTimer = null;
document.getElementById('k8s-yaml-ns').addEventListener('input', () => {
  clearTimeout(_k8sPodListTimer);
  _k8sPodListTimer = setTimeout(() => {
    const yamlPane = document.getElementById('k8s-sub-yaml');
    if (yamlPane && yamlPane.classList.contains('active')) loadK8sPodList();
  }, 500);
});
document.getElementById('btn-k8s-net-run').onclick = runK8sNet;

// 事件 / 资源 Top / 描述
document.getElementById('btn-k8s-ev').onclick = () => startK8sAuto('events');
document.getElementById('k8s-ev-auto').onchange = () => startK8sAuto('events');
document.getElementById('btn-k8s-top').onclick = () => startK8sAuto('top');
document.getElementById('k8s-top-scope').onchange = loadK8sTop;
document.getElementById('k8s-top-auto').onchange = () => startK8sAuto('top');
document.getElementById('btn-k8s-describe-pod').onclick = () => {
  const pod = state.k8s.lastPod;
  if (!pod) { toast('请先在上方选择一个 Pod 查看其日志。', 'warn'); return; }
  openK8sDescribe('pod', pod);
};
document.getElementById('btn-k8s-yaml-describe').onclick = () => {
  const kind = document.getElementById('k8s-yaml-kind').value.trim();
  const name = document.getElementById('k8s-yaml-name').value.trim();
  const ns = document.getElementById('k8s-yaml-ns').value.trim();
  if (!name) { document.getElementById('k8s-yaml-msg').textContent = '请先填写资源名称'; return; }
  openK8sDescribe(kind, name, ns);
};
document.getElementById('btn-k8s-describe-run').onclick = runK8sDescribe;
document.getElementById('btn-k8s-describe-close').onclick = closeK8sDescribeModal;
document.getElementById('k8s-describe-modal').onclick = (e) => {
  if (e.target === document.getElementById('k8s-describe-modal')) closeK8sDescribeModal();
};
document.getElementById('btn-k8s-describe-copy').onclick = () => {
  const t = document.getElementById('k8s-describe-text').textContent;
  (navigator.clipboard?.writeText(t) || Promise.reject())
    .then(() => document.getElementById('k8s-describe-msg').textContent = '已复制',
          () => document.getElementById('k8s-describe-msg').textContent = '复制失败');
};

// ===== Shell / 文件 子标签事件绑定 =====
document.getElementById('k8s-shell-pod').onchange = onK8sShellPodChange;
document.getElementById('k8s-shell-container').onchange = () => { if (_shellWs) k8sShellDisconnect(); };
document.getElementById('k8s-shell-connect').onclick = k8sShellConnect;
document.getElementById('k8s-shell-disconnect').onclick = k8sShellDisconnect;
document.getElementById('k8s-shell-input').addEventListener('keydown', k8sShellKey);

document.getElementById('k8s-files-up').onclick = k8sFilesUp;
document.getElementById('k8s-files-refresh').onclick = k8sFilesRefresh;
document.getElementById('k8s-files-mkdir').onclick = k8sFilesMkdir;
document.getElementById('k8s-files-upload').onclick = k8sFilesUploadClick;
document.getElementById('k8s-files-delete').onclick = k8sFilesDelete;
document.getElementById('k8s-files-fileinput').addEventListener('change', k8sFilesUploadChange);

document.getElementById('k8s-file-edit-close').onclick = k8sFileEditClose;
document.getElementById('k8s-file-edit-save').onclick = k8sFileSave;
document.getElementById('k8s-file-edit-download').onclick = k8sFileDownload;
document.getElementById('k8s-file-edit-modal').onclick = (e) => {
  if (e.target === document.getElementById('k8s-file-edit-modal')) k8sFileEditClose();
};

// 进入 K8s 标签页时加载环境列表（保留原有 switchTab 行为）
document.querySelectorAll('.tab').forEach(t => {
  const prev = t.onclick;
  t.onclick = () => { if (prev) prev(); if (t.dataset.tab === 'k8s') loadK8sEnvs(); };
});
loadK8sEnvs();

// ===== HCM 云函数日志 =====
const HCM_CFG_KEY = 'jgg-hcm-cfg';
// 预置环境（可直接切换，地址/账号密码用户填全）
const HCM_PRESETS = {
  public: {
    name: '公有云',
    server_url: 'https://21qor.hcmcloud.cn',
    mobile: '666666',
    password: 'Ab666666',
  },
  test1: {
    name: '测试地址',
    server_url: 'http://73.2.3.27',
    mobile: '666666',
    password: 'Ab666666',
  },
  test2: {
    name: '测试环境',
    server_url: 'http://73.2.192',
    mobile: '666666',
    password: 'Ab666666',
  },
  dev: {
    name: '开发环境',
    server_url: '',
    mobile: '666666',
    password: 'Ab666666',
  },
};
function switchHcmEnv(key) {
  if (!key || !HCM_PRESETS[key]) {
    // custom / 空：不填地址，其他填默认
    if (key === 'custom') {
      document.getElementById('hcm-username').value = '666666';
      document.getElementById('hcm-password').value = 'Ab666666';
    }
    return;
  }
  const p = HCM_PRESETS[key];
  document.getElementById('hcm-server-url').value = p.server_url || '';
  document.getElementById('hcm-username').value = p.mobile || '';
  document.getElementById('hcm-password').value = p.password || '';
  toast(`已切换到「${p.name}」环境，账号密码已预填，地址：${p.server_url || '请手动填写'}`, 'info');
  saveHcmCfg();
}

function loadHcmCfg() {
  try {
    const cfg = JSON.parse(localStorage.getItem(HCM_CFG_KEY) || '{}');
    document.getElementById('hcm-server-url').value = cfg.server_url || 'https://21qor.hcmcloud.cn';
    document.getElementById('hcm-username').value = cfg.mobile || cfg.username || '666666';
    document.getElementById('hcm-password').value = cfg.password || 'Ab666666';
    document.getElementById('hcm-token').value = cfg.token || '';
    document.getElementById('hcm-proxy').value = cfg.proxy || '';
    document.getElementById('hcm-log-type').value = cfg.log_type || '';
    document.getElementById('hcm-page-size').value = cfg.page_size || 200;
    document.getElementById('hcm-page-index').value = cfg.page_index || 1;
    // 自动选中匹配的预置环境
    const sel = document.getElementById('hcm-env-select');
    const url = cfg.server_url || '';
    let matched = 'custom';
    for (const k of Object.keys(HCM_PRESETS)) {
      if (HCM_PRESETS[k].server_url && url && HCM_PRESETS[k].server_url === url) { matched = k; break; }
      if (HCM_PRESETS[k].server_url && url && url.startsWith(HCM_PRESETS[k].server_url)) { matched = k; break; }
    }
    if (sel) sel.value = matched;
  } catch (_) {}
}

function saveHcmCfg() {
  const cfg = {
    server_url: document.getElementById('hcm-server-url').value.trim(),
    mobile: document.getElementById('hcm-username').value.trim(),
    password: document.getElementById('hcm-password').value.trim(),
    token: document.getElementById('hcm-token').value.trim(),
    proxy: document.getElementById('hcm-proxy').value.trim(),
    log_type: document.getElementById('hcm-log-type').value.trim(),
    page_size: parseInt(document.getElementById('hcm-page-size').value) || 200,
    page_index: parseInt(document.getElementById('hcm-page-index').value) || 1,
  };
  try { localStorage.setItem(HCM_CFG_KEY, JSON.stringify(cfg)); } catch (_) {}
}

let HCM_CURRENT_CAPTCHA_ID = '';
let HCM_CURRENT_IMAGE_CODE_INDEX = '';
let HCM_LAST_LOG_RESULT = null;  // 上次查询结果，供导出 JSON 用
let HCM_SORT_DIR = 'desc';        // 'asc' | 'desc' 按 create_time 排序（客户端）

async function fetchHcmCaptcha() {
  const serverUrl = document.getElementById('hcm-server-url').value.trim();
  const proxy = document.getElementById('hcm-proxy').value.trim();
  const imgEl = document.getElementById('hcm-captcha-img');
  const statusEl = document.getElementById('hcm-query-status');
  if (!serverUrl) {
    toast('请先填写服务器地址', 'warn');
    return;
  }
  imgEl.style.opacity = '0.4';
  imgEl.style.cursor = 'progress';
  try {
    const res = await apiPost('/api/hcm/captcha', { server_url: serverUrl, proxy });
    HCM_CURRENT_CAPTCHA_ID = res.captcha_id || '';
    HCM_CURRENT_IMAGE_CODE_INDEX = res.image_code_index || '';
    imgEl.src = res.image || '';
    imgEl.style.opacity = '1';
    imgEl.style.cursor = 'pointer';
    document.getElementById('hcm-captcha-code').value = '';
  } catch (e) {
    HCM_CURRENT_CAPTCHA_ID = '';
    HCM_CURRENT_IMAGE_CODE_INDEX = '';
    imgEl.style.opacity = '1';
    imgEl.style.cursor = 'pointer';
    imgEl.removeAttribute('src');
    if (statusEl) {
      statusEl.textContent = `获取验证码失败：${e.message}`;
      statusEl.className = 'hcm-query-status error';
    } else {
      toast('获取验证码失败：' + e.message, 'error');
    }
  }
}

async function exportHcmLogs() {
  const statusEl = document.getElementById('hcm-query-status');
  const btn = document.getElementById('hcm-btn-export-json');
  if (!HCM_LAST_LOG_RESULT || !HCM_LAST_LOG_RESULT.rows || !HCM_LAST_LOG_RESULT.rows.length) {
    statusEl.textContent = '请先查询日志并确保有结果，再导出';
    statusEl.className = 'hcm-query-status error';
    return;
  }
  const r = HCM_LAST_LOG_RESULT;
  if (btn) { btn.disabled = true; btn.textContent = '导出中…'; }
  try {
    const res = await apiPost('/api/hcm/logs/export', {
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
      statusEl.className = 'hcm-query-status success';
      try { console.log('HCM 日志导出路径:', res.path); } catch (_) {}
    } else {
      throw new Error('未返回文件路径');
    }
  } catch (e) {
    statusEl.textContent = `导出失败：${e.message}`;
    statusEl.className = 'hcm-query-status error';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '导出 JSON'; }
  }
}

async function saveClipboardToFile() {
  const statusEl = document.getElementById('hcm-query-status');
  const btn = document.getElementById('hcm-btn-clipboard-save');

  // 1) 读取系统剪贴板文本
  //    Electron 环境优先走原生 clipboard 模块（不受浏览器 clipboard-read 权限限制），
  //    纯 Web / HTTPS 环境回退到 navigator.clipboard.readText()。
  const isElectron = !!(window.electronAPI && window.electronAPI.isElectron);
  let text = '';
  try {
    if (isElectron) {
      text = await window.electronAPI.readClipboardText();
    } else if (navigator.clipboard && navigator.clipboard.readText) {
      text = await navigator.clipboard.readText();
    } else {
      throw new Error('当前环境不支持剪贴板读取 API');
    }
  } catch (e) {
    statusEl.textContent = `读取剪贴板失败：${e.message}（请先复制文本，并点击本窗口使其获得焦点，再重试）`;
    statusEl.className = 'hcm-query-status error';
    return;
  }
  if (!text || !text.trim()) {
    statusEl.textContent = '剪贴板内容为空，请先复制一些文本再点击';
    statusEl.className = 'hcm-query-status error';
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  statusEl.textContent = '正在保存剪贴板内容到文件…';
  statusEl.className = 'hcm-query-status';
  try {
    const res = await apiPost('/api/hcm/clipboard-save', { text });
    if (res.path) {
      // 复制文件路径到剪贴板，便于直接粘贴
      let copied = false;
      try {
        if (isElectron) {
          await window.electronAPI.writeClipboardText(res.path);
          copied = true;
        } else if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(res.path);
          copied = true;
        }
      } catch (_) {}
      statusEl.textContent = `已保存剪贴板内容（${res.size} 字符）→ ${res.path}${copied ? '（路径已复制到剪贴板）' : ''}`;
      statusEl.className = 'hcm-query-status success';
      log(`剪贴板转文件成功：${res.path}`);
    } else {
      throw new Error('未返回文件路径');
    }
  } catch (ex) {
    statusEl.textContent = `保存失败：${ex.message}`;
    statusEl.className = 'hcm-query-status error';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📋 剪贴板转文件'; }
  }
}

function toggleHcmCfg() {
  const body = document.getElementById('hcm-cfg-body');
  const btn = document.getElementById('hcm-cfg-toggle');
  const visible = body.style.display !== 'none';
  body.style.display = visible ? 'none' : '';
  btn.textContent = visible ? '配置 ▸' : '配置 ▾';
}

async function hcmLogin() {
  const serverUrl = document.getElementById('hcm-server-url').value.trim();
  const mobile = document.getElementById('hcm-username').value.trim();
  const password = document.getElementById('hcm-password').value.trim();
  const proxy = document.getElementById('hcm-proxy').value.trim();
  const imageCode = document.getElementById('hcm-captcha-code').value.trim();
  const statusEl = document.getElementById('hcm-query-status');

  if (!serverUrl || !mobile || !password) {
    statusEl.textContent = '请填写服务器地址、手机号和密码';
    statusEl.className = 'hcm-query-status error';
    return;
  }
  // 图片验证码改为可选：默认不强制，仅当用户填了验证码或后端要求(need_img_valid)时才需要
  // 若用户填了验证码却没拉取过验证码图片，提示先刷新
  if (imageCode && !HCM_CURRENT_CAPTCHA_ID) {
    statusEl.textContent = '请先点击「刷新」获取验证码图片再输入';
    statusEl.className = 'hcm-query-status error';
    return;
  }

  const btn = document.getElementById('hcm-btn-login');
  btn.disabled = true;
  btn.textContent = '登录中…';
  statusEl.textContent = '正在登录…';

  try {
    const res = await apiPost('/api/hcm/login', {
      server_url: serverUrl,
      mobile,
      password,
      proxy,
      image_code: imageCode,
      image_code_index: HCM_CURRENT_IMAGE_CODE_INDEX,
      captcha_id: HCM_CURRENT_CAPTCHA_ID,
    });
    // 登录成功
    if (res.token) {
      document.getElementById('hcm-token').value = res.token;
      HCM_CURRENT_CAPTCHA_ID = '';
      HCM_CURRENT_IMAGE_CODE_INDEX = '';
      document.getElementById('hcm-captcha-img').removeAttribute('src');
      document.getElementById('hcm-captcha-code').value = '';
      saveHcmCfg();
      statusEl.textContent = '登录成功，Token 已获取并保存';
      statusEl.className = 'hcm-query-status success';
      return;
    }
    // 后端透传的登录被拒：success=false
    if (res && res.ok === false) {
      const needImg = res.need_img_valid === true;
      const msg = res.message || '登录失败';
      if (needImg) {
        statusEl.textContent = `${msg}（需要图片验证码，请输入后重新登录）`;
        statusEl.className = 'hcm-query-status error';
        // 自动拉取验证码图片供用户填写
        try { await fetchHcmCaptcha(); } catch (_) {}
      } else {
        statusEl.textContent = `登录失败：${msg}`;
        statusEl.className = 'hcm-query-status error';
      }
      return;
    }
    throw new Error('未获取到 token');
  } catch (ex) {
    statusEl.textContent = `登录失败：${ex.message}`;
    statusEl.className = 'hcm-query-status error';
    // 仅在已有验证码会话时刷新（避免无验证码部署被狂刷）
    if (HCM_CURRENT_CAPTCHA_ID) {
      try { await fetchHcmCaptcha(); } catch (_) {}
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '登录获取 Token';
  }
}

async function queryHcmLogs() {
  const serverUrl = document.getElementById('hcm-server-url').value.trim();
  const token = document.getElementById('hcm-token').value.trim();
  const logType = document.getElementById('hcm-log-type').value.trim();
  const pageSize = parseInt(document.getElementById('hcm-page-size').value) || 200;
  const pageIndex = parseInt(document.getElementById('hcm-page-index').value) || 1;
  const proxy = document.getElementById('hcm-proxy').value.trim();
  const statusEl = document.getElementById('hcm-query-status');
  const resultsEl = document.getElementById('hcm-results');

  if (!token) {
    statusEl.textContent = '请先配置 Token（可点击「登录获取 Token」或手动填写）';
    statusEl.className = 'hcm-query-status error';
    return;
  }
  // 注意：log_type 允许留空（后端按空 filter 查询全部 dynamic_log），不再强制必填

  saveHcmCfg();

  const btn = document.getElementById('hcm-btn-query');
  btn.disabled = true;
  btn.textContent = '查询中…';
  statusEl.textContent = proxy ? `正在查询（代理：${proxy}）…` : '正在查询（直连）…';
  statusEl.className = 'hcm-query-status';
  resultsEl.innerHTML = '<div class="empty-hint">加载中…</div>';

  try {
    const res = await apiPost('/api/hcm/logs', {
      server_url: serverUrl,
      token,
      log_type: logType,
      page_index: pageIndex,
      page_size: pageSize,
      proxy,
    });

    // 解析返回数据：兼容 HCM OpenAPI {data: {list/total}} / {result: {...}} / 裸 {list/total}
    const payload = res.data || res.result || res;
    const rows = payload.list || payload.data || payload.items || res.list || res.data || [];
    const total = payload.total ?? payload.count ?? payload.row_count ?? res.total ?? rows.length;

    // 保存查询结果供「导出 JSON」使用
    HCM_LAST_LOG_RESULT = {
      server_url: serverUrl,
      log_type: logType,
      auth_method: res.method || '',
      page_index: pageIndex,
      page_size: pageSize,
      total,
      rows,
      raw: res,
    };
    const exportBtn = document.getElementById('hcm-btn-export-json');
    if (exportBtn) exportBtn.disabled = rows.length === 0;

    statusEl.textContent = `查询成功，共 ${total} 条`;
    statusEl.className = 'hcm-query-status success';

    // 清空上次搜索词，确保新查询整页可见
    const searchInputEl = document.getElementById('hcm-search-input');
    if (searchInputEl) searchInputEl.value = '';

    if (!rows.length) {
      resultsEl.innerHTML = '<div class="empty-hint">未找到匹配的日志记录</div>';
      const sb = document.getElementById('hcm-search-bar');
      if (sb) sb.style.display = 'none';
      return;
    }

    // 渲染结果（含排序 + 客户端实时搜索过滤；详见 renderHcmResults）
    renderHcmResults();

  } catch (ex) {
    statusEl.textContent = `查询失败：${ex.message}`;
    statusEl.className = 'hcm-query-status error';
    resultsEl.innerHTML = '<div class="empty-hint">查询失败</div>';
    const exportBtn = document.getElementById('hcm-btn-export-json');
    if (exportBtn) exportBtn.disabled = true;
  } finally {
    btn.disabled = false;
    btn.textContent = '查询日志';
  }
}

// HCM 日志结果渲染（排序 + 客户端实时搜索过滤，不重新请求服务器）
function _hcmTime(row) {
  return String(row.create_time || row.createTime || row.created_at || '');
}
function _hcmContent(row) {
  const c = row.content || row.message || row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c) : String(c);
}
function _hcmContentFull(row) {
  const c = row.content || row.message || row.data;
  if (c == null) return '';
  return typeof c === 'object' ? JSON.stringify(c, null, 2) : String(c);
}
function _hcmLogType(row, fallback) {
  // 优先取记录自身的 log_type（HCM dynamic_log 每条记录都带该字段）；
  // 查询时指定了 log_type 则作为兜底；都没有则标「未知」
  return row.log_type || row.logType || fallback || '(未知)';
}
// 拉取剩余分页，合并为全量日志集（供「按时间排序所有日志」使用，避免只排当前页）
async function ensureAllHcmLogs() {
  const base = HCM_LAST_LOG_RESULT;
  if (!base || !base.rows) return;
  const total = base.total || 0;
  if (total === 0 || base.rows.length >= total) return;  // 已全量或未知总量
  const statusEl = document.getElementById('hcm-query-status');
  const token = document.getElementById('hcm-token').value.trim();
  const proxy = document.getElementById('hcm-proxy').value.trim();
  const pageSize = base.page_size || 200;
  let nextPage = Math.floor(base.rows.length / pageSize) + 1;
  if (nextPage < 2) nextPage = 2;
  try {
    while (base.rows.length < total && nextPage <= 500) {
      statusEl.textContent = `正在加载全部日志用于排序…（${base.rows.length}/${total}）`;
      statusEl.className = 'hcm-query-status';
      const res = await apiPost('/api/hcm/logs', {
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
    statusEl.className = 'hcm-query-status error';
  }
}

function renderHcmResults() {
  const base = HCM_LAST_LOG_RESULT;
  const resultsEl = document.getElementById('hcm-results');
  const searchBar = document.getElementById('hcm-search-bar');
  if (!base || !base.rows) return;

  const all = base.rows.slice();
  // 是否已加载全部日志（用于排序/计数提示）
  const isFull = (base.total || 0) > 0 && base.rows.length >= base.total;
  // 排序（按时间）
  all.sort((a, b) => {
    const ta = _hcmTime(a), tb = _hcmTime(b);
    const cmp = ta < tb ? -1 : ta > tb ? 1 : 0;
    return HCM_SORT_DIR === 'asc' ? cmp : -cmp;
  });

  // 客户端实时过滤（内容 + 时间）
  const q = (document.getElementById('hcm-search-input').value || '').trim();
  const caseSensitive = document.getElementById('hcm-search-case').checked;
  let filtered = all;
  if (q) {
    const needle = caseSensitive ? q : q.toLowerCase();
    filtered = all.filter(r => {
      const content = caseSensitive ? _hcmContent(r) : _hcmContent(r).toLowerCase();
      const time = caseSensitive ? _hcmTime(r) : _hcmTime(r).toLowerCase();
      const type = caseSensitive ? _hcmLogType(r, base.log_type) : _hcmLogType(r, base.log_type).toLowerCase();
      return content.includes(needle) || time.includes(needle) || type.includes(needle);
    });
  }

  // 显示搜索栏 + 更新计数 / 排序按钮文案
  if (searchBar) searchBar.style.display = '';
  const cntEl = document.getElementById('hcm-search-count');
  if (cntEl) cntEl.innerHTML = q
    ? `匹配 <b>${filtered.length}</b> / ${isFull ? '全部' : '本页'} ${all.length}`
    : (isFull ? `共 ${all.length} 条` : `本页 ${all.length} / 共 ${base.total} 条`);
  const sortBtn = document.getElementById('hcm-btn-sort-time');
  if (sortBtn) sortBtn.textContent = HCM_SORT_DIR === 'asc' ? '时间 ↑' : '时间 ↓';

  if (!filtered.length) {
    resultsEl.innerHTML = `<div class="empty-hint">${q ? '没有匹配当前搜索条件的日志' : '未找到匹配的日志记录'}</div>`;
    return;
  }

  const logType = base.log_type || '';
  let html = `
    <div class="hcm-result-meta">
      <span class="hcm-result-count">${all.length} 条结果</span>
      <span>${isFull ? '已加载全部（本地排序）' : `第 ${base.page_index || 1} 页`}</span>
      ${logType ? `<span>log_type: ${esc(logType)}</span>` : '<span>全部 log_type</span>'}
    </div>
    <table class="hcm-log-table">
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
    const createTime = _hcmTime(row);
    const content = _hcmContent(row);
    const contentFull = _hcmContentFull(row);
    const logTypeVal = _hcmLogType(row, base.log_type);
    const idx = i + 1;
    html += `
      <tr class="hcm-log-row" data-idx="${i}">
        <td>${idx}</td>
        <td class="hcm-log-type" title="${esc(logTypeVal)}">${esc(logTypeVal)}</td>
        <td class="hcm-log-time">${esc(createTime)}</td>
        <td class="hcm-log-content">${esc(content)}</td>
      </tr>
      <tr class="hcm-log-detail-row" id="hcm-detail-${i}" style="display:none">
        <td colspan="4">
          <div class="hcm-log-meta">类型：${esc(logTypeVal)} ｜ ID：${esc(row.id != null ? row.id : (row._id || ''))} ｜ 时间：${esc(createTime)}</div>
          <div class="hcm-log-content-full">${esc(contentFull)}</div>
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
    html += '<div class="hcm-pagination">';
    if (pageIndex > 1) {
      html += `<button class="btn btn-sm" onclick="document.getElementById('hcm-page-index').value=${pageIndex - 1};queryHcmLogs()">上一页</button>`;
    }
    html += `<span style="font-size:12px;color:var(--muted)">${pageIndex} / ${totalPages}</span>`;
    if (pageIndex < totalPages) {
      html += `<button class="btn btn-sm" onclick="document.getElementById('hcm-page-index').value=${pageIndex + 1};queryHcmLogs()">下一页</button>`;
    }
    html += '</div>';
  }
  resultsEl.innerHTML = html;

  // 绑定行点击展开/收起
  document.querySelectorAll('.hcm-log-row').forEach(tr => {
    tr.onclick = () => {
      const idx = tr.dataset.idx;
      const detail = document.getElementById(`hcm-detail-${idx}`);
      if (detail) {
        const visible = detail.style.display !== 'none';
        detail.style.display = visible ? 'none' : '';
        tr.classList.toggle('expanded', !visible);
      }
    };
  });
}

// HCM 事件绑定
document.getElementById('hcm-env-select').addEventListener('change', (e) => {
  switchHcmEnv(e.target.value);
});
document.getElementById('hcm-cfg-toggle').onclick = function() {
  toggleHcmCfg();
  // 不自动拉验证码 — 只有用户手动点「刷新」或「登录获取Token」才拉
};
document.getElementById('hcm-btn-save').onclick = saveHcmCfg;
document.getElementById('hcm-btn-login').onclick = async () => {
  // 点登录前先确保有验证码
  const imgEl = document.getElementById('hcm-captcha-img');
  if (!imgEl.getAttribute('src')) {
    try { await fetchHcmCaptcha(); } catch (e) { return; }
  }
  hcmLogin();
};
document.getElementById('hcm-btn-query').onclick = queryHcmLogs;
document.getElementById('hcm-btn-export-json').onclick = exportHcmLogs;
document.getElementById('hcm-btn-clipboard-save').onclick = saveClipboardToFile;
document.getElementById('hcm-btn-refresh-captcha').onclick = fetchHcmCaptcha;
document.getElementById('hcm-captcha-img').addEventListener('click', fetchHcmCaptcha);
document.getElementById('hcm-log-type').addEventListener('keydown', e => {
  if (e.key === 'Enter') queryHcmLogs();
});
// 云函数日志搜索栏：实时过滤 + 时间排序
document.getElementById('hcm-search-input').addEventListener('input', renderHcmResults);
document.getElementById('hcm-search-case').addEventListener('change', renderHcmResults);
document.getElementById('hcm-btn-sort-time').addEventListener('click', async () => {
  HCM_SORT_DIR = HCM_SORT_DIR === 'asc' ? 'desc' : 'asc';
  const btn = document.getElementById('hcm-btn-sort-time');
  if (btn) btn.disabled = true;
  try {
    // 排序前先拉取剩余分页，确保排序覆盖「所有日志」而非仅当前页
    await ensureAllHcmLogs();
    renderHcmResults();
  } finally {
    if (btn) btn.disabled = false;
  }
});
loadHcmCfg();

// ===== 差异对比 =====
const diffState = {
  entries: [],
  selectedPath: '',
  localDir: '',
  showSame: false,         // 默认隐藏相同文件
  ignoreLineEndings: true, // 默认忽略 CRLF/LF 行尾差异
  mergeRemoteOnly: false,  // 仅合并「仅远程」的云端差异项
};
let diffDoneTimer = null;

async function scanDiff() {
  const localDir = document.getElementById('diff-local-dir').value.trim();
  if (!localDir) { log('请输入本地目录路径', 'warning'); return; }
  if (!state.selectedRepo) { log('请先选择远程仓库', 'warning'); return; }

  const btn = document.getElementById('btn-diff-scan');
  btn.disabled = true;
  btn.textContent = '扫描中…';

  // 立即显示进度条并重置状态（不等首个 SSE 事件）
  clearTimeout(diffDoneTimer);
  setDiffProgress({
    visible: true, mode: 'indeterminate',
    stage: '准备中…', detail: '',
  });
  clearDiffErrors();

  document.getElementById('diff-summary').textContent = '正在扫描本地和远程…';
  document.getElementById('diff-list').innerHTML = '<div class="empty-hint">扫描中…大仓库可能需要较长时间</div>';
  document.getElementById('btn-diff-merge-all').style.display = 'none';

  try {
    const res = await apiPost('/api/diff/scan', {
      local_dir: localDir,
      repo_name: state.selectedRepo.display_name || '',
      ignore_line_endings: diffState.ignoreLineEndings,
    });
    diffState.entries = res.entries || [];
    diffState.localDir = localDir;

    const s = res.summary;
    const wsBadge = s.whitespace_only
      ? ` · <span class="badge-eol" title="仅 CRLF/LF 行尾差异，已默认忽略">行尾差异 ${s.whitespace_only}</span>`
      : '';
    document.getElementById('diff-summary').innerHTML =
      `共 ${s.total} 个文件 | ` +
      `<span class="badge-modified">修改 ${s.modified}</span> · ` +
      `<span class="badge-local">仅本地 ${s.local_only}</span> · ` +
      `<span class="badge-remote">仅远程 ${s.remote_only}</span> · 相同 ${s.same}` +
      wsBadge;

    renderDiffList();
    const mergeAllBtn = document.getElementById('btn-diff-merge-all');
    mergeAllBtn.style.display = diffState.entries.length > 0 ? '' : 'none';
    log(`差异扫描完成：共 ${s.total} 个文件，修改 ${s.modified}，仅本地 ${s.local_only}，仅远程 ${s.remote_only}`);
  } catch (ex) {
    document.getElementById('diff-summary').textContent = `扫描失败：${ex.message}`;
    document.getElementById('diff-list').innerHTML = `<div class="empty-hint">扫描失败：${esc(ex.message)}</div>`;
    // 进度条置为错误态；若 SSE scan_error 未展示则补充到错误框
    setDiffProgress({ mode: 'error', stage: '扫描失败', detail: '' });
    const errBox = document.getElementById('diff-error-box');
    if (!errBox.children.length) addDiffError(ex.message);
    log(`差异扫描失败：${ex.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '扫描差异';
  }
}

const DIFF_ICONS = {
  modified: '✎',
  whitespace_only: '≈',
  local_only: '←',
  remote_only: '→',
  same: '=',
};

const DIFF_LABELS = {
  modified: '已修改',
  whitespace_only: '仅行尾差异',
  local_only: '仅本地',
  remote_only: '仅远程',
  same: '相同',
};

function renderDiffList() {
  const el = document.getElementById('diff-list');
  const visible = diffState.entries.filter(e => {
    if (e.status === 'same') return diffState.showSame;
    if (e.status === 'whitespace_only') return !diffState.ignoreLineEndings;
    return true;
  });
  if (!visible.length) {
    const allSame = diffState.entries.length && diffState.entries.every(e => e.status === 'same');
    const allIgnored = diffState.entries.length && diffState.entries.every(e => e.status === 'same' || e.status === 'whitespace_only');
    el.innerHTML = allSame
      ? '<div class="empty-hint">无差异文件（所有文件相同；可勾选「显示相同」查看）</div>'
      : allIgnored
        ? '<div class="empty-hint">无有效差异（剩余差异均为 CRLF/LF 行尾符；可取消「忽略行尾差异」查看）</div>'
        : '<div class="empty-hint">无差异（本地与远程完全一致）</div>';
    return;
  }
  el.innerHTML = '';
  visible.forEach(e => {
    const item = document.createElement('div');
    item.className = 'diff-item' + (e.status === 'whitespace_only' ? ' diff-item-eol' : '');
    const badge = e.status === 'whitespace_only' ? ' <span class="diff-eol-badge">CRLF/LF</span>' : '';
    item.innerHTML = `<span class="diff-icon">${DIFF_ICONS[e.status] || '?'}</span><span class="diff-path" title="${esc(e.path)}">${esc(e.path)}${badge}</span>`;
    item.onclick = () => openDiffFile(e.path, item);
    el.appendChild(item);
  });
}

async function openDiffFile(path, itemEl) {
  diffState.selectedPath = path;
  document.querySelectorAll('.diff-item').forEach(el => el.classList.remove('selected'));
  if (itemEl) itemEl.classList.add('selected');

  document.getElementById('diff-file-title').textContent = `加载中 · ${path}`;
  document.getElementById('diff-content').innerHTML = '<div class="empty-hint">加载中…</div>';
  document.getElementById('btn-diff-merge-one').style.display = 'none';

  try {
    const res = await apiPost('/api/diff/file', { local_dir: diffState.localDir, path });
    const entry = diffState.entries.find(e => e.path === path);
    const status = entry?.status || '';
    document.getElementById('diff-file-title').textContent =
      `${path}  (${DIFF_LABELS[status] || status})`;
    renderDiffContent(res, status);
    document.getElementById('btn-diff-merge-one').style.display = '';
  } catch (ex) {
    document.getElementById('diff-file-title').textContent = '错误';
    document.getElementById('diff-content').textContent = ex.message;
  }
}

/**
 * GitHub 风格 diff 渲染。
 *
 * 策略：
 * 1. 有 unified diff → 渲染行号 + 绿/红背景的 +/- 行
 * 2. diff 为空但文件标记为 modified → 内容实际相同（行尾/编码差异），
 *    显示提示"内容相同（可能仅行尾/编码差异）"
 * 3. diff 为空且文件相同 → 显示"内容完全相同"
 * 4. 只有本地/只有远程 → 显示单侧内容
 */
function renderDiffContent(res, status) {
  const el = document.getElementById('diff-content');
  el.innerHTML = '';
  const diffText = res.diff || '';
  const local = res.local_content || '';
  const remote = res.remote_content || '';

  // 没有 diff 文本的情况
  if (!diffText) {
    if (status === 'modified' || status === 'whitespace_only' || res.normalized_same) {
      // 标记为 modified / whitespace_only 但 unified_diff 为空 → 内容实际相同（行尾/编码差异）
      const hint = document.createElement('div');
      hint.className = 'diff-info-hint';
      hint.innerHTML = '内容实际相同（可能仅行尾符 / 空白 / 格式差异）<br>本地大小 ' +
        (local.length) + ' 字符，远程大小 ' + (remote.length) + ' 字符';
      el.appendChild(hint);
      // 仍然显示内容供用户确认
      _renderSideBySide(el, local, remote);
      return;
    }
    if (status === 'local_only') {
      el.innerHTML = '<div class="empty-hint">仅本地存在，远程无此文件</div>';
      _renderPlain(el, local, 'local');
      return;
    }
    if (status === 'remote_only') {
      el.innerHTML = '<div class="empty-hint">仅远程存在，本地无此文件（新增）</div>';
      _renderPlain(el, remote, 'remote');
      return;
    }
    el.innerHTML = '<div class="empty-hint">内容完全相同</div>';
    return;
  }

  // 有 unified diff → GitHub 风格渲染
  _renderUnifiedDiff(el, diffText);
}

/** 渲染 GitHub 风格 unified diff（带行号 + 绿/红行） */
function _renderUnifiedDiff(el, diffText) {
  const lines = diffText.split('\n');
  const tbl = document.createElement('table');
  tbl.className = 'diff-table';
  let oldNo = 0, newNo = 0;

  for (const line of lines) {
    if (!line) continue;
    const tr = document.createElement('tr');

    let type = 'ctx';
    let oldCell = '', newCell = '', content = line;

    if (line.startsWith('@@')) {
      type = 'hunk';
      // @@ -oldStart,oldCount +newStart,newCount @@
      const m = line.match(/@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
      if (m) { oldNo = parseInt(m[1]) - 1; newNo = parseInt(m[2]) - 1; }
    } else if (line.startsWith('+++') || line.startsWith('---')) {
      type = 'header';
    } else if (line.startsWith('+')) {
      type = 'add'; newNo++;
      content = line.substring(1);
      newCell = newNo;
    } else if (line.startsWith('-')) {
      type = 'del'; oldNo++;
      content = line.substring(1);
      oldCell = oldNo;
    } else if (line.startsWith(' ')) {
      type = 'ctx'; oldNo++; newNo++;
      content = line.substring(1);
      oldCell = oldNo; newCell = newNo;
    }

    tr.className = 'diff-row diff-' + type;
    tr.innerHTML =
      '<td class="diff-ln">' + oldCell + '</td>' +
      '<td class="diff-ln">' + newCell + '</td>' +
      '<td class="diff-sign">' +
        (type === 'add' ? '+' : type === 'del' ? '-' : '') + '</td>' +
      '<td class="diff-code">' + _escapeHtml(content) + '</td>';
    tbl.appendChild(tr);
  }
  el.appendChild(tbl);
}

/** 侧并排显示本地/远程内容（内容相同时的对比） */
function _renderSideBySide(el, local, remote) {
  const wrap = document.createElement('div');
  wrap.className = 'diff-sidebyside';
  const localLines = local.split('\n');
  const remoteLines = remote.split('\n');
  const maxLines = Math.max(localLines.length, remoteLines.length);

  const tbl = document.createElement('table');
  tbl.className = 'diff-table diff-sidebyside-table';
  for (let i = 0; i < maxLines; i++) {
    const tr = document.createElement('tr');
    const l = localLines[i] ?? '';
    const r = remoteLines[i] ?? '';
    const same = l === r;
    tr.className = 'diff-row ' + (same ? 'diff-ctx' : 'diff-changed');
    tr.innerHTML =
      '<td class="diff-ln">' + (i + 1) + '</td>' +
      '<td class="diff-code">' + _escapeHtml(l) + '</td>' +
      '<td class="diff-ln">' + (i + 1) + '</td>' +
      '<td class="diff-code">' + _escapeHtml(r) + '</td>';
    tbl.appendChild(tr);
  }
  wrap.appendChild(tbl);
  el.appendChild(wrap);
}

/** 渲染单侧纯文本 */
function _renderPlain(el, content, side) {
  const pre = document.createElement('pre');
  pre.className = 'diff-plain diff-plain-' + side;
  pre.textContent = content;
  el.appendChild(pre);
}

function _escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function mergeOne() {
  if (!diffState.selectedPath) return;
  const path = diffState.selectedPath;
  const btn = document.getElementById('btn-diff-merge-one');
  btn.disabled = true;
  btn.textContent = '合并中…';
  try {
    const res = await apiPost('/api/diff/merge', { local_dir: diffState.localDir, path });
    if (res.ok) {
      log(`已合并到本地：${path}`);
      // 从列表中移除
      diffState.entries = diffState.entries.filter(e => e.path !== path);
      renderDiffList();
      document.getElementById('diff-content').innerHTML = '<div class="empty-hint">已合并 ✓</div>';
      document.getElementById('btn-diff-merge-one').style.display = 'none';
      document.getElementById('diff-file-title').textContent = '已合并';
    } else {
      log(`合并失败：${path}`, 'error');
    }
  } catch (ex) {
    log(`合并失败：${ex.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '合并到本地';
  }
}

function updateMergeAllButton() {
  const btn = document.getElementById('btn-diff-merge-all');
  if (!btn) return;
  if (diffState.mergeRemoteOnly) {
    btn.title = '仅合并「仅远程」的云端差异项';
  } else {
    btn.title = '合并所有远程与本地不同的文件';
  }
}

async function mergeAll() {
  let entries;
  if (diffState.mergeRemoteOnly) {
    entries = diffState.entries.filter(e => e.status === 'remote_only');
  } else {
    entries = diffState.entries.filter(e => {
      if (e.status === 'whitespace_only' && diffState.ignoreLineEndings) return false;
      return e.status === 'modified' || e.status === 'remote_only' || e.status === 'whitespace_only';
    });
  }
  if (!entries.length) {
    log(diffState.mergeRemoteOnly ? '没有需要合并的云端差异项' : '没有需要合并的文件', 'warning');
    return;
  }
  const btn = document.getElementById('btn-diff-merge-all');
  btn.disabled = true;
  btn.textContent = `合并中 (0/${entries.length})…`;
  const modeHint = diffState.mergeRemoteOnly ? '（仅云端差异项）' : '';
  log(`开始批量合并 ${entries.length} 个文件${modeHint}…`);
  try {
    const reqs = entries.map(e => ({
      local_dir: diffState.localDir,
      path: e.path,
      status: e.status,
    }));
    const query = diffState.mergeRemoteOnly ? '?status_filter=remote_only' : '';
    const res = await apiPost(`/api/diff/merge-batch${query}`, reqs);
    const okCount = (res.results || []).filter(r => r.ok).length;
    const failCount = (res.results || []).length - okCount;
    log(`批量合并完成${modeHint}：成功 ${okCount}，失败 ${failCount}`);
    // 从列表中移除成功的
    const okPaths = new Set((res.results || []).filter(r => r.ok).map(r => r.path));
    diffState.entries = diffState.entries.filter(e => !okPaths.has(e.path));
    renderDiffList();
    // 更新摘要
    document.getElementById('diff-summary').textContent = `合并完成${modeHint}：成功 ${okCount}，失败 ${failCount}`;
  } catch (ex) {
    log(`批量合并失败：${ex.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '全部合并到本地';
  }
}

// ===== 初始化 =====
log('应用就绪。先点「连接设置」配置 Jira 地址 / 账号 / 模式，再在仓库面板选择或指定仓库。');

// 应用已保存的主题偏好（localStorage 持久化）
try {
  const saved = localStorage.getItem('jgg-theme');
  if (saved) applyTheme(saved);
} catch (_) {}

updateStatus();
connectSSE();
loadRepoMappings(); // 后台加载 .env 仓库映射，供后续自动填充本地目录

// Tauri 特有：接收 Rust 主进程和 Python 日志，同步到 UI 日志面板
if (window.__TAURI__) {
  try {
    import('@tauri-apps/api/core').then(m =>
      m.invoke('get_app_info').then(info => {
        if (info?.log_file) {
          log(`[Tauri] 运行环境：${info.platform}  日志文件：${info.log_file}`);
        }
      }).catch(() => {})
    ).catch(() => {});
    import('@tauri-apps/api/event').then(m =>
      m.listen('log:append', (event) => {
        const text = event.payload?.text || '';
        if (!text) return;
        const el = document.getElementById('log-content');
        if (!el) return;
        const line = document.createElement('div');
        line.className = 'log-line';
        if (/\[error\]|\[py:err\]/.test(text)) line.classList.add('error');
        else if (/\[warning\]/.test(text)) line.classList.add('warning');
        line.textContent = text;
        el.appendChild(line);
        while (el.children.length > 3000) el.removeChild(el.firstChild);
        el.scrollTop = el.scrollHeight;
      })
    ).catch(() => {});
  } catch (_) {}
}

// Electron 特有：接收主进程和 Python 日志，同步到 UI 日志面板
if (window.electronAPI?.onAppLog) {
  try {
    window.electronAPI.getAppInfo?.().then(info => {
      if (info?.logFile) {
        log(`[Electron] 运行环境：${info.platform}  日志文件：${info.logFile}`);
      }
    }).catch(() => {});
    window.electronAPI.onAppLog(text => {
      // 纯 Electron/主进程日志直接写入 UI（不加 [renderer] 的前缀以避免重复）
      const el = document.getElementById('log-content');
      if (!el) return;
      const line = document.createElement('div');
      line.className = 'log-line';
      if (/\[error\]|\[py:err\]/.test(text)) line.classList.add('error');
      else if (/\[warning\]/.test(text)) line.classList.add('warning');
      line.textContent = text;
      el.appendChild(line);
      while (el.children.length > 3000) el.removeChild(el.firstChild);
      el.scrollTop = el.scrollHeight;
    });
  } catch (_) {}
}
