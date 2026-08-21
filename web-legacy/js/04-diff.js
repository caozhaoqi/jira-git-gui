/* ============================================================
   差异对比
   本地/远程差异扫描、diff 渲染（unified/side-by-side）、合并到本地。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
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

