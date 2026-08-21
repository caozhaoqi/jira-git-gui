/* ============================================================
   事件绑定与初始化
   所有按钮/输入事件绑定、主题初始化、SSE 连接、Tauri/Electron 日志监听。必须最后加载。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
// ===== 事件绑定 =====
document.getElementById('btn-connect').onclick = openConnectModal;
document.getElementById('connect-close').onclick = closeConnectModal;
document.getElementById('btn-connect-cancel').onclick = closeConnectModal;
document.getElementById('btn-test-connect').onclick = testConnect;
document.getElementById('btn-connect-ok').onclick = applyConnect;
document.getElementById('btn-theme').onclick = toggleTheme;
document.querySelectorAll('input[name="mode"]').forEach(r => r.onchange = onModeChange);

document.getElementById('btn-discover').onclick = () => discoverRepos(false);
document.getElementById('btn-repo-refresh').onclick = () => discoverRepos(true);
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

// 文件树工具栏
document.getElementById('tree-search-input').addEventListener('input', onTreeSearchInput);
document.querySelectorAll('.tree-scope-btn').forEach(b => {
  b.onclick = () => onTreeScopeChange(b.dataset.scope);
});
document.getElementById('tree-sort-key').onchange = onTreeSortChange;
document.getElementById('tree-sort-dir').onclick = onTreeSortDirToggle;

// 文件预览栏：最大化 / 复制路径
function previewToggleMaximize() {
  const col = document.getElementById('repo-preview-col');
  const btn = document.getElementById('preview-maximize');
  const isFs = col.classList.toggle('repo-col-fullscreen');
  btn.textContent = isFs ? '🗗' : '⛶';
  btn.title = isFs ? '还原' : '最大化';
}
function previewCopyPath() {
  const p = state.selectedFile;
  if (!p) { toast('当前未选中文件', 'warn'); return; }
  writeClipboardText(p);
  toast('已复制路径到剪贴板', 'info');
}
document.getElementById('preview-maximize').onclick = previewToggleMaximize;
document.getElementById('preview-copy-path').onclick = previewCopyPath;

// 三栏拖拽调整列宽（宽度记忆在 localStorage）
(function initRepoResizers() {
  const LEFT_KEY = 'repo.colLeft.width', RIGHT_KEY = 'repo.colRight.width';
  const leftCol = document.querySelector('.repo-col-left');
  const rightCol = document.querySelector('.repo-col-right');
  // 恢复记忆
  try {
    const lw = localStorage.getItem(LEFT_KEY);
    const rw = localStorage.getItem(RIGHT_KEY);
    if (lw && leftCol) leftCol.style.width = `${Math.min(Math.max(+lw, 180), 480)}px`;
    if (rw && rightCol) rightCol.style.width = `${Math.min(Math.max(+rw, 320), 900)}px`;
  } catch (_) {}

  document.querySelectorAll('.repo-resizer').forEach(rz => {
    rz.addEventListener('mousedown', e => {
      e.preventDefault();
      const side = rz.dataset.side;
      rz.classList.add('dragging');
      document.body.style.userSelect = 'none';
      // 拖拽中禁用宽度 transition，避免卡顿
      const col = side === 'left' ? leftCol : rightCol;
      if (col) col.style.transition = 'none';

      const onMove = ev => {
        if (!col) return;
        const rect = document.querySelector('.repo-three-col').getBoundingClientRect();
        if (side === 'left') {
          col.style.width = `${Math.min(Math.max(ev.clientX - rect.left - 3, 180), 480)}px`;
        } else {
          col.style.width = `${Math.min(Math.max(rect.right - ev.clientX - 3, 320), 900)}px`;
        }
      };
      const onUp = () => {
        rz.classList.remove('dragging');
        document.body.style.userSelect = '';
        if (col) col.style.transition = '';
        if (col && side === 'left') localStorage.setItem(LEFT_KEY, parseFloat(col.style.width));
        if (col && side === 'right') localStorage.setItem(RIGHT_KEY, parseFloat(col.style.width));
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });
})();

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
document.getElementById('k8s-file-edit-maximize').onclick = k8sFileEditToggleMaximize;
document.getElementById('k8s-files-search-btn').onclick = k8sFilesSearch;
document.getElementById('k8s-files-search-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') k8sFilesSearch();
});
// K8s 文件列表表头点击排序
initK8sFilesSort();
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
// （withGlobalTauri 注入 window.__TAURI__，纯静态页无需打包器即可调用 IPC）
if (window.__TAURI__?.core) {
  try {
    window.__TAURI__.core.invoke('get_app_info').then(info => {
      if (info?.log_file) {
        log(`[Tauri] 运行环境：${info.platform}  日志文件：${info.log_file}`);
      }
    }).catch(() => {});
    window.__TAURI__.event.listen('log:append', (event) => {
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
    }).catch(() => {});
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
