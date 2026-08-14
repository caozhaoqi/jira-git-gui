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
};

const API = location.origin;

// ===== API 封装 =====
async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !data.error) throw new Error(`HTTP ${res.status}`);
  return data;
}

async function apiPost(path, body) {
  return api(path, { method: 'POST', body: JSON.stringify(body) });
}

async function apiDelete(path) {
  return api(path, { method: 'DELETE' });
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
  btn.textContent = '测试中…';
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
    btn.textContent = '测试连接';
  }
}

async function applyConnect() {
  const cfg = getConnectConfig();
  await apiPost('/api/connect', cfg);
  closeConnectModal();
  updateStatus();
  log('连接配置已更新。');
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

// ===== 事件绑定 =====
document.getElementById('btn-connect').onclick = openConnectModal;
document.getElementById('connect-close').onclick = closeConnectModal;
document.getElementById('btn-connect-cancel').onclick = closeConnectModal;
document.getElementById('btn-test-connect').onclick = testConnect;
document.getElementById('btn-connect-ok').onclick = applyConnect;
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

// ===== 初始化 =====
log('应用就绪。先点「连接设置」配置 Jira 地址 / 账号 / 模式，再在仓库面板选择或指定仓库。');
updateStatus();
connectSSE();

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
