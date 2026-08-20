/* ============================================================
   K8s 运维
   快照、环境管理、子标签切换、YAML、网络检测、事件/资源Top/描述、Shell 终端、文件浏览器（含搜索/排序/全屏编辑）。
   （由 web/app.js 拆分而来，保持全局作用域，按 index.html 顺序加载）
   ============================================================ */
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
  // 排序：目录优先（所有列都保持），再按 key/dir
  const { key, dir } = state.k8s.files.sort;
  const dirMul = dir === 'desc' ? -1 : 1;
  const rows = (entries || []).slice().sort((a, b) => {
    const ad = a.type === 'dir' ? 0 : 1, bd = b.type === 'dir' ? 0 : 1;
    if (ad !== bd) return ad - bd;
    let cmp = 0;
    if (key === 'name') cmp = String(a.name).localeCompare(String(b.name), undefined, { numeric: true });
    else if (key === 'type') cmp = String(a.type).localeCompare(String(b.type));
    else if (key === 'size') cmp = (a.size || 0) - (b.size || 0);
    else if (key === 'mtime') cmp = String(a.modtime || '').localeCompare(String(b.modtime || ''));
    return cmp * dirMul;
  });
  // 更新表头排序指示
  document.querySelectorAll('#k8s-sub-files .k8s-sortable').forEach(th => {
    const ind = th.querySelector('.k8s-sort-ind');
    if (!ind) return;
    if (th.dataset.sort === key) ind.textContent = dir === 'desc' ? '▼' : '▲';
    else ind.textContent = '';
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

// 点击列头切换排序
function initK8sFilesSort() {
  document.querySelectorAll('#k8s-sub-files .k8s-sortable').forEach(th => {
    th.style.cursor = 'pointer';
    th.onclick = () => {
      const key = th.dataset.sort;
      if (!key) return;
      const cur = state.k8s.files.sort;
      if (cur.key === key) cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
      else { cur.key = key; cur.dir = 'asc'; }
      // 用最近一次拉到的 entries 重新渲染
      renderK8sFiles(state.k8s.files.entries || []);
    };
  });
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
  const modal = document.getElementById('k8s-file-edit-modal');
  const box = document.getElementById('k8s-file-edit-box');
  modal.style.display = 'none';
  modal.classList.remove('modal-fullscreen');
  box.classList.remove('modal-fullscreen');
  document.getElementById('k8s-file-edit-maximize').textContent = '⛶';
  document.getElementById('k8s-file-edit-msg').textContent = '';
}

function k8sFileEditToggleMaximize() {
  const modal = document.getElementById('k8s-file-edit-modal');
  const box = document.getElementById('k8s-file-edit-box');
  const btn = document.getElementById('k8s-file-edit-maximize');
  const isFs = box.classList.toggle('modal-fullscreen');
  modal.classList.toggle('modal-fullscreen', isFs);
  btn.textContent = isFs ? '🗗' : '⛶';
  btn.title = isFs ? '还原' : '最大化';
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

// K8s 容器内文件内容搜索（kubectl exec grep）
async function k8sFilesSearch() {
  const q = document.getElementById('k8s-files-search-input').value.trim();
  const statusEl = document.getElementById('k8s-files-search-status');
  const resultsEl = document.getElementById('k8s-files-search-results');
  if (!q) { statusEl.textContent = '请输入搜索关键词'; return; }
  const t = k8sTarget();
  if (!t.pod) { statusEl.textContent = '请先选择 Pod'; return; }
  statusEl.textContent = '搜索中…';
  resultsEl.style.display = 'block';
  resultsEl.innerHTML = '<div class="tree-search-loading">搜索中…</div>';
  try {
    const d = await apiPost('/api/k8s/file/search', {
      env: t.env, pod: t.pod, container: t.container, namespace: t.namespace,
      q, path: state.k8s.files.path || '/',
    });
    if (!d.ok) {
      resultsEl.innerHTML = `<div class="tree-search-error">搜索失败：${esc(d.error || '未知错误')}</div>`;
      statusEl.textContent = '';
      return;
    }
    const rs = d.results || [];
    statusEl.textContent = `共 ${d.total} 处匹配`;
    if (!rs.length) {
      resultsEl.innerHTML = '<div class="tree-search-empty">没有匹配结果</div>';
      return;
    }
    resultsEl.innerHTML = rs.map(r => `
      <div class="tree-search-item" data-path="${esc(r.path)}">
        <span class="tsr-icon">📄</span>
        <span class="tsr-path">${esc(r.path)}</span>
        <span class="tsr-line">:${r.line}</span>
        <div class="tsr-snippet">${esc((r.snippet || '').slice(0, 200))}</div>
      </div>`).join('');
    resultsEl.querySelectorAll('.tree-search-item').forEach(item => {
      item.onclick = async () => {
        // 直接尝试在编辑弹窗中打开（复用 k8sFileOpen 逻辑）
        const base = state.k8s.files.path || '/';
        const full = item.dataset.path;
        const name = (full.split('/').pop()) || full;
        // 切到文件所在目录再打开
        const dir = full.includes('/') ? full.slice(0, full.lastIndexOf('/')) : base;
        await k8sFilesList(dir);
        // 定位到该文件后打开编辑
        try {
          const t2 = k8sTarget();
          const d2 = await apiPost('/api/k8s/file/read', {
            env: t2.env, pod: t2.pod, container: t2.container, namespace: t2.namespace,
            path: full, max_bytes: 200000,
          });
          if (!d2.ok) { toast('打开失败：' + (d2.error || ''), 'error'); return; }
          if (d2.is_binary) { toast('这是二进制文件，不支持在线编辑。', 'info'); return; }
          state.k8s.files.editPath = full;
          document.getElementById('k8s-file-edit-area').value = d2.content || '';
          document.getElementById('k8s-file-edit-title').textContent = '编辑 · ' + name + (d2.truncated ? '（已截断）' : '');
          document.getElementById('k8s-file-edit-msg').textContent = '';
          document.getElementById('k8s-file-edit-modal').style.display = 'flex';
        } catch (ex) { toast('打开失败：' + ex.message, 'error'); }
      };
    });
  } catch (ex) {
    resultsEl.innerHTML = `<div class="tree-search-error">搜索失败：${esc(ex.message)}</div>`;
    statusEl.textContent = '';
  }
}

