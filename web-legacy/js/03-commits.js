/* ============================================================
   提交记录
   commit 查询/渲染（GitHub 风格）、commit diff 渲染、提交模式切换。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
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
    const shortMsg = msg.length > 60 ? msg.slice(0, 59) + '…' : msg;
    // 汇总：+N -M（N 总增行、M 总删行；统计该 commit 涉及的所有文件）
    let adds = 0, dels = 0;
    (c.files || []).forEach(f => {
      adds += (f.lines_added || 0);
      dels += (f.lines_removed || 0);
    });
    const statsHtml = (adds || dels)
      ? `<span class="commit-stats"><b class="add">+${adds}</b><b class="del">-${dels}</b></span>`
      : '';
    // 相对时间（X 天前 / X 小时前 / 刚刚）
    const relTime = formatRelativeTime(c.date);
    // 作者首字母徽章（用作者名前 2 字符）
    const authorBadge = buildAuthorBadge(c.author);
    const shortHash = (c.commit_id || '').slice(0, 7);
    item.innerHTML = `
      <div class="commit-item-row">
        ${authorBadge}
        <div class="commit-item-body">
          <div class="commit-msg" title="${esc(c.message || '')}">${esc(shortMsg)}</div>
          <div class="commit-meta">
            <span class="commit-author">${esc(c.author || '?')}</span>
            <span class="commit-hash">${esc(shortHash)}</span>
            <span class="commit-when" title="${esc(c.date || '')}">${esc(relTime)}</span>
            ${c.repository_name ? `<span class="commit-repo">${esc(c.repository_name)}</span>` : ''}
            <span class="commit-files-count">📄 ${(c.files || []).length}</span>
          </div>
        </div>
        ${statsHtml}
      </div>
    `;
    item.onclick = () => selectCommit(i);
    el.appendChild(item);
  });
}

// 作者徽章：根据作者名生成稳定的颜色 + 首字字符
function buildAuthorBadge(author) {
  const s = String(author || '?').trim();
  const ch = s ? s[0].toUpperCase() : '?';
  // 简单 hash 到 HSL 色相（避免名字直接当颜色，且稳定）
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffff;
  const hue = h % 360;
  const color = `hsl(${hue}, 55%, 45%)`;
  return `<span class="commit-badge" style="background:${color}">${esc(ch)}</span>`;
}

// 相对时间：'刚刚' / 'X 分钟前' / 'X 小时前' / 'X 天前' / 'X 周前' / yyyy-mm-dd
function formatRelativeTime(iso) {
  if (!iso) return '';
  const t = new Date(iso);
  if (isNaN(t.getTime())) return iso.slice(0, 10);
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / (86400 * 7))} 周前`;
  return t.toISOString().slice(0, 10);
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
    `变更文件（${c.files.length}）：单击文件查看行级 diff`,
  ].join('\n');
  document.getElementById('commit-detail-text').textContent = detail;

  const filesEl = document.getElementById('commit-files');
  filesEl.innerHTML = '';
  const signMap = { ADDED: '+', MODIFIED: 'M', DELETED: 'D', RENAMED: 'R', COPIED: 'C',
                    A: '+', M: 'M', D: 'D', R: 'R', C: 'C' };
  // 累计该 commit 的 +/- 统计
  let totalAdd = 0, totalDel = 0;
  (c.files || []).forEach(f => { totalAdd += (f.lines_added || 0); totalDel += (f.lines_removed || 0); });
  const summaryHtml = `<div class="commit-file-summary">
    <span>共 ${c.files.length} 个文件变更</span>
    <span class="commit-stats"><b class="add">+${totalAdd}</b><b class="del">-${totalDel}</b></span>
  </div>`;
  filesEl.innerHTML = summaryHtml;
  c.files.forEach(f => {
    const sign = signMap[f.change_type?.toUpperCase()] || '?';
    const item = document.createElement('div');
    item.className = 'commit-file-item';
    const stat = (f.lines_added || f.lines_removed)
      ? `<span class="commit-stats"><b class="add">+${f.lines_added || 0}</b><b class="del">-${f.lines_removed || 0}</b></span>`
      : '';
    item.innerHTML = `<span class="change-badge change-${sign}">${sign}</span>
                      <span class="commit-file-path">${esc(f.path)}</span>${stat}`;
    item.onclick = () => openFileAtCommit(c.commit_id, f.path);
    filesEl.appendChild(item);
  });
}

async function openFileAtCommit(commitId, path) {
  const diffEl = document.getElementById('commit-diff');
  if (!diffEl) {
    // 兜底：在仓库 tab 三栏的右栏直接渲染
    document.getElementById('preview-title').textContent = `加载中 · ${path} @ ${commitId.slice(0, 8)}`;
    document.getElementById('preview-content').textContent = '';
    switchTab('repo');
  }
  // 拉旧版（在 commit 处）与当前版本
  const shortHash = commitId.slice(0, 8);
  if (diffEl) {
    diffEl.style.display = 'block';
    diffEl.innerHTML = `<div class="diff-loading">加载 ${esc(path)} @ ${esc(shortHash)} 的 diff…</div>`;
  }
  try {
    const [oldRes, newRes] = await Promise.all([
      api(`/api/file-at-commit?commit_id=${encodeURIComponent(commitId)}&path=${encodeURIComponent(path)}`),
      api(`/api/file?path=${encodeURIComponent(path)}`),
    ]);
    if (oldRes.error && newRes.error) {
      if (diffEl) diffEl.innerHTML = `<div class="diff-error">加载失败：旧版本 ${esc(oldRes.error)}；新版本 ${esc(newRes.error)}</div>`;
      return;
    }
    if (oldRes.error) {
      // 新增文件（旧版本不存在）：整个文件视为 +
      if (diffEl) {
        diffEl.innerHTML = `<div class="diff-title">${esc(path)}（新增文件 · 旧版本不存在）</div>`
          + renderDiff('', newRes.content || '');
      }
      return;
    }
    if (newRes.error) {
      // 删除文件（新版本不存在）
      if (diffEl) {
        diffEl.innerHTML = `<div class="diff-title">${esc(path)}（文件已删除）</div>`
          + renderDiff(oldRes.content || '', '');
      }
      return;
    }
    if (diffEl) {
      diffEl.innerHTML = `<div class="diff-title">${esc(path)} @ ${esc(shortHash)} → 当前</div>` + renderDiff(oldRes.content || '', newRes.content || '');
    }
  } catch (ex) {
    if (diffEl) diffEl.innerHTML = `<div class="diff-error">加载失败：${esc(ex.message)}</div>`;
  }
}


// ===== 提交模式切换 =====
function onCommitModeChange() {
  const local = document.getElementById('commit-mode').value === 'local';
  document.getElementById('commit-issue-label').textContent = local ? '仓库' : 'Issue';
  document.getElementById('commit-issue').placeholder = local ? '(当前仓库)' : 'TST-234';
}

