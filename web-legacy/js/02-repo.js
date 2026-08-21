/* ============================================================
   仓库 / 文件树 / 预览 / 克隆下载
   连接设置弹窗、仓库列表、文件树（含排序/搜索工具栏）、文件预览（含最大化/复制路径）、克隆/下载、速率并发。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
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
async function discoverRepos(force) {
  const btn = document.getElementById('btn-discover');
  btn.disabled = true;
  btn.textContent = '发现中…';
  log('【发现仓库】开始…');
  try {
    // 默认命中后端 10 分钟缓存（秒开）；force=true 绕过缓存重新发现
    const res = await api(force ? '/api/repos?refresh=1' : '/api/repos');
    if (res.error) {
      log(`发现仓库错误：${res.error}`, 'warning');
      // Cookie 可能过期
      if (/cookie|登录|login|未配置/i.test(res.error)) {
        log('Cookie 可能已过期，请重新打开「连接设置」获取新 Cookie。', 'error');
      }
    }
    state.repos = res.repos || [];
    renderRepoList();
    log(`【发现仓库】返回 ${state.repos.length} 个${force ? '（强制刷新）' : ''}`);
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
  // 排序：目录优先（同级时），再按 key/dir
  const { key, dir } = state.treeSort;
  const dirMul = dir === 'desc' ? -1 : 1;
  const sorted = [...entries].sort((a, b) => {
    // 目录永远在文件前面
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    let cmp = 0;
    if (key === 'name') cmp = a.name.localeCompare(b.name, undefined, { numeric: true });
    else if (key === 'type') cmp = (a.type || '').localeCompare(b.type || '');
    else if (key === 'size') cmp = (a.size || 0) - (b.size || 0);
    else if (key === 'mtime') cmp = (a.mtime || 0) - (b.mtime || 0);
    return cmp * dirMul;
  });
  sorted.forEach(e => {
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

    const mtime = document.createElement('span');
    mtime.className = 'tree-mtime';
    mtime.textContent = e.mtime ? fmtMtime(e.mtime) : '';
    mtime.title = e.mtime ? new Date(e.mtime * 1000).toLocaleString() : '';

    row.appendChild(toggle);
    row.appendChild(icon);
    row.appendChild(name);
    row.appendChild(size);
    row.appendChild(mtime);
    // 供拖拽重排（reloadTreeChildren）读取排序键
    row.dataset.mtime = e.mtime != null ? String(e.mtime) : '';

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


// ===== 文件树工具栏：搜索 / 排序 =====
let _treeSearchTimer = null;

function onTreeSearchInput() {
  const q = document.getElementById('tree-search-input').value;
  state.treeSearch.q = q;
  clearTimeout(_treeSearchTimer);
  // 简单 debounce 200ms
  _treeSearchTimer = setTimeout(() => runTreeSearch(), 200);
}

async function runTreeSearch() {
  const q = (state.treeSearch.q || '').trim();
  const scope = state.treeSearch.scope;
  const resultsEl = document.getElementById('tree-search-results');
  const statusEl = document.getElementById('tree-search-status');
  if (!q) {
    resultsEl.style.display = 'none';
    resultsEl.innerHTML = '';
    statusEl.textContent = '';
    return;
  }
  if (scope === 'filename') {
    // 前端纯内存：递归遍历已加载树，标记匹配项
    const matches = filterTreeByName(q);
    renderTreeSearchResults(matches, 'filename');
    statusEl.textContent = `已加载目录中匹配 ${matches.length} 项`;
  } else {
    // 后端全文搜索
    statusEl.textContent = '搜索中…';
    resultsEl.style.display = 'block';
    resultsEl.innerHTML = '<div class="tree-search-loading">搜索中…</div>';
    try {
      const res = await api(`/api/search?q=${encodeURIComponent(q)}&scope=content&limit=200`);
      if (res.error) {
        resultsEl.innerHTML = `<div class="tree-search-error">${esc(res.error)}</div>`;
        statusEl.textContent = '';
        return;
      }
      state.treeSearch.results = res.results || [];
      renderTreeSearchResults(state.treeSearch.results, 'content');
      statusEl.textContent = `共 ${res.total} 处匹配${res.truncated ? '（已截断）' : ''}`;
    } catch (ex) {
      resultsEl.innerHTML = `<div class="tree-search-error">搜索失败：${esc(ex.message)}</div>`;
      statusEl.textContent = '';
    }
  }
}

function filterTreeByName(q) {
  const re = new RegExp(escapeRegExp(q), 'i');
  const results = [];
  // 通过 state.treeNodes 已注册的所有 path 找（不论展开与否），匹配后仅高亮
  Object.keys(state.treeNodes).forEach(path => {
    const base = path.split('/').pop() || path;
    if (re.test(base)) {
      results.push({ path, name: base });
    }
  });
  return results;
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function renderTreeSearchResults(results, scope) {
  const el = document.getElementById('tree-search-results');
  if (!results.length) {
    el.style.display = 'block';
    el.innerHTML = '<div class="tree-search-empty">没有匹配结果</div>';
    return;
  }
  el.style.display = 'block';
  el.innerHTML = results.slice(0, 100).map(r => {
    if (scope === 'filename') {
      return `<div class="tree-search-item" data-path="${esc(r.path)}">
        <span class="tsr-icon">📄</span>
        <span class="tsr-path">${esc(r.path)}</span>
      </div>`;
    }
    return `<div class="tree-search-item" data-path="${esc(r.path)}">
      <span class="tsr-icon">📄</span>
      <span class="tsr-path">${esc(r.path)}</span>
      <span class="tsr-line">:${r.line || 0}</span>
      <div class="tsr-snippet">${esc((r.snippet || '').slice(0, 200))}</div>
    </div>`;
  }).join('');
  // 点击结果：在右栏预览
  el.querySelectorAll('.tree-search-item').forEach(item => {
    item.onclick = () => {
      const path = item.dataset.path;
      if (typeof openFile === 'function') openFile(path);
    };
  });
  if (results.length > 100) {
    el.innerHTML += `<div class="tree-search-more">仅显示前 100 条，共 ${results.length} 条</div>`;
  }
}

function onTreeScopeChange(scope) {
  state.treeSearch.scope = scope;
  document.querySelectorAll('.tree-scope-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.scope === scope);
  });
  if (state.treeSearch.q) runTreeSearch();
}

function onTreeSortChange() {
  const key = document.getElementById('tree-sort-key').value;
  state.treeSort.key = key;
  // 排序变化：重排已加载的根树（递归子层）
  const root = document.getElementById('tree-container');
  if (root) {
    reloadTreeChildren(root);
    Object.values(state.treeNodes).forEach(n => {
      const children = n.element?.querySelector(':scope > .tree-children');
      if (children) reloadTreeChildren(children);
    });
  }
}

function onTreeSortDirToggle() {
  const btn = document.getElementById('tree-sort-dir');
  const dir = btn.dataset.dir === 'asc' ? 'desc' : 'asc';
  btn.dataset.dir = dir;
  state.treeSort.dir = dir;
  btn.textContent = dir === 'asc' ? '↑' : '↓';
  btn.title = dir === 'asc' ? '当前升序，点击降序' : '当前降序，点击升序';
  onTreeSortChange();
}

function reloadTreeChildren(container) {
  if (!container) return;
  const nodeEls = Array.from(container.querySelectorAll(':scope > .tree-node'));
  if (!nodeEls.length) return;
  const items = nodeEls.map(nodeEl => {
    const name = nodeEl.querySelector(':scope > .tree-row .tree-name')?.textContent || '';
    const icon = nodeEl.querySelector(':scope > .tree-row .tree-icon')?.textContent || '';
    const sizeText = nodeEl.querySelector(':scope > .tree-row .tree-size')?.textContent || '';
    const mtimeText = nodeEl.querySelector(':scope > .tree-row')?.dataset?.mtime || '';
    return {
      nodeEl, type: icon === '📁' ? 'dir' : 'file',
      name, size: sizeText ? parseSizeText(sizeText) : 0,
      mtime: mtimeText ? parseFloat(mtimeText) : 0,
    };
  });
  const { key, dir } = state.treeSort;
  const dirMul = dir === 'desc' ? -1 : 1;
  items.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    let cmp = 0;
    if (key === 'name') cmp = a.name.localeCompare(b.name, undefined, { numeric: true });
    else if (key === 'size') cmp = a.size - b.size;
    else if (key === 'mtime') cmp = a.mtime - b.mtime;
    return cmp * dirMul;
  });
  items.forEach(it => container.appendChild(it.nodeEl));
}

function parseSizeText(s) {
  const m = s.match(/^([\d.]+)\s*(B|KB|MB|GB)?$/i);
  if (!m) return 0;
  const n = parseFloat(m[1]);
  const u = (m[2] || 'B').toUpperCase();
  const mult = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3 }[u] || 1;
  return n * mult;
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
  // 三栏布局：右栏直接渲染，不再切换 tab
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

