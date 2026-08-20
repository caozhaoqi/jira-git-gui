/* ============================================================
   独立日志查看页逻辑
   - 全屏浏览 / 分析：搜索高亮、级别高亮、容器切换、tail 控制、
     自动刷新(live tail)、字号、换行、行号、下载
   - API 统一用 location.origin：Tauri/Electron 窗口加载的就是后端
     URL（http://127.0.0.1:<port>），本页也由后端 HTTP 提供，同源。
     不硬编码 8787，避免端口顺延时连错。
   ============================================================ */
const API = location.origin;
const qs = new URLSearchParams(location.search);

const params = {
  pod: qs.get('pod') || '',
  env: qs.get('env') || '',
  container: qs.get('container') || '',
  namespace: qs.get('namespace') || '',
};

const state = {
  raw: '',
  re: null,            // 当前搜索正则（无 g 标志，用于 test）
  matches: [],         // 匹配行索引数组
  cur: -1,             // 当前匹配位置
  lineEls: [],         // 行 DOM 引用（与行号对齐）
  font: 13,
  autoFollow: false,   // 自动刷新时强制跟随底部
  timer: null,
};

const $ = (id) => document.getElementById(id);

/* ---------- 工具 ---------- */
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
async function apiGet(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error('HTTP ' + r.status + ' · ' + (await r.text()).slice(0, 200));
  return r;
}

/* ---------- 主题 ---------- */
function applyTheme(theme) {
  const dark = theme === 'dark';
  document.body.classList.toggle('dark', dark);
  const b = $('lv-theme'); if (b) b.textContent = dark ? '☀' : '🌓';
  try { localStorage.setItem('jgg-theme', theme); } catch (_) {}
}
function initTheme() {
  let t = null;
  try { t = localStorage.getItem('jgg-theme'); } catch (_) {}
  if (!t && window.matchMedia) t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(t || 'light');
}

/* ---------- UI 初始化 ---------- */
function applyParamsToUI() {
  // Pod 选择器由 loadPods() 填充（select 不能直接设 textContent）
  $('lv-env').textContent = 'env: ' + (params.env || '—');
  $('lv-ns').textContent  = 'ns: ' + (params.namespace || '—');
  const ct = $('lv-ct');
  if (params.container) ct.textContent = '容器: ' + params.container;
  else ct.style.display = 'none';
}

/* ---------- Pod 列表（自由切换） ---------- */
async function loadPods() {
  const sel = $('lv-pod');
  sel.innerHTML = '<option value="">选择 Pod…</option>';
  if (!params.env) {
    setStatus('未指定 env，无法加载 Pod 列表。', true);
    return;
  }
  try {
    let q = '/api/k8s/pods?env=' + encodeURIComponent(params.env);
    if (params.namespace) q += '&namespace=' + encodeURIComponent(params.namespace);
    const r = await apiGet(q);
    const d = await r.json();
    if (!d.ok || !Array.isArray(d.pods)) {
      setStatus('加载 Pod 列表失败：' + (d.error || '未知错误'), true);
      return;
    }
    d.pods.forEach(p => {
      const o = document.createElement('option');
      o.value = p.name;
      o.textContent = p.name + (p.namespace ? ' [' + p.namespace + ']' : '') +
                      ' · ' + (p.phase || '?');
      if (p.name === params.pod) o.selected = true;
      sel.appendChild(o);
    });
    if (!params.pod) setStatus('请选择上方 Pod 查看日志。');
  } catch (ex) {
    setStatus('加载 Pod 列表失败：' + ex.message, true);
  }
}

function switchPod() {
  const sel = $('lv-pod');
  const pod = sel.value;
  const ct = $('lv-ct');
  ct.style.display = 'none';
  state.raw = '';
  $('lv-log').innerHTML = '';
  if (!pod) {
    params.pod = ''; params.container = ''; params.namespace = '';
    $('lv-ns').textContent = 'ns: —';
    setStatus('请选择 Pod 查看日志。');
    return;
  }
  params.pod = pod;
  params.container = '';
  // 从选项文本解析命名空间（「name [ns] · phase」格式）
  const opt = sel.selectedOptions[0];
  const nsMatch = opt && opt.textContent.match(/\[([^\]]+)\]/);
  params.namespace = nsMatch ? nsMatch[1] : '';
  $('lv-ns').textContent = 'ns: ' + (params.namespace || '—');
  loadContainers().finally(refresh);
}

/* ---------- 容器列表 ---------- */
async function loadContainers() {
  if (!params.pod) return;
  const sel = $('lv-container');
  sel.innerHTML = '<option value="">全部容器</option>';
  try {
    const r = await apiGet('/api/k8s/pod-containers?name=' + encodeURIComponent(params.pod) +
                           '&env=' + encodeURIComponent(params.env));
    const d = await r.json();
    if (d.ok && Array.isArray(d.containers)) {
      if (d.namespace && !params.namespace) {
        params.namespace = d.namespace;
        $('lv-ns').textContent = 'ns: ' + d.namespace;
      }
      d.containers.forEach(c => {
        const o = document.createElement('option');
        o.value = c; o.textContent = c;
        sel.appendChild(o);
      });
      if (params.container && d.containers.includes(params.container)) sel.value = params.container;
    }
  } catch (ex) {
    console.warn('获取容器列表失败（不影响日志拉取）：', ex.message);
  }
}

/* ---------- 拉取日志 ---------- */
function setStatus(msg, isErr) {
  const el = $('lv-status');
  el.textContent = msg || '';
  el.style.color = isErr ? 'var(--danger)' : 'var(--muted)';
}
function isAtBottom() {
  const b = $('lv-body');
  return b.scrollHeight - b.scrollTop - b.clientHeight < 48;
}
function scrollBottom() {
  const b = $('lv-body');
  b.scrollTop = b.scrollHeight;
}

async function refresh() {
  if (!params.pod) { setStatus('未指定 Pod，无法加载日志。', true); return; }
  const follow = state.autoFollow || isAtBottom();
  setStatus('加载中…');
  try {
    const q = new URLSearchParams();
    q.set('name', params.pod);
    if (params.env) q.set('env', params.env);
    if (params.container) q.set('container', params.container);
    if (params.namespace) q.set('namespace', params.namespace);
    q.set('tail', $('lv-tail').value);
    if ($('lv-prevlog').checked) q.set('previous', '1');
    const r = await apiGet('/api/k8s/log?' + q.toString());
    state.raw = await r.text();
    render();
    if (follow) scrollBottom();
    setStatus('');
  } catch (ex) {
    setStatus('加载失败：' + ex.message, true);
  }
}

/* ---------- 级别识别 ---------- */
function detectLevel(line) {
  const u = line.toUpperCase();
  if (/\b(FATAL|CRITICAL)\b/.test(u)) return 'FATAL';
  if (/\b(ERROR|ERR|EXCEPTION|TRACEBACK|UNCAUGHT)\b/.test(u)) return 'ERROR';
  if (/\b(WARN|WARNING)\b/.test(u)) return 'WARN';
  if (/\bINFO\b/.test(u)) return 'INFO';
  if (/\bDEBUG\b/.test(u)) return 'DEBUG';
  return '';
}

/* ---------- 高亮（整行转义，仅匹配处加 mark，防 XSS） ---------- */
function buildHlRe(re) {
  if (!re) return null;
  return new RegExp(re.source, re.flags.includes('g') ? re.flags : re.flags + 'g');
}
function highlight(line, hlRe) {
  if (!hlRe) return escapeHtml(line) || '&nbsp;';
  let out = '', last = 0, m;
  hlRe.lastIndex = 0;
  while ((m = hlRe.exec(line)) !== null) {
    if (m.index > last) out += escapeHtml(line.slice(last, m.index));
    out += '<mark class="lv-hl">' + escapeHtml(m[0]) + '</mark>';
    last = m.index + m[0].length;
    if (m.index === hlRe.lastIndex) hlRe.lastIndex++;
  }
  out += escapeHtml(line.slice(last));
  return out || '&nbsp;';
}

/* ---------- 渲染 ---------- */
function render() {
  const body = $('lv-log');
  const lineno = $('lv-lineno').checked;
  const levelOn = $('lv-level').checked;
  const hlRe = buildHlRe(state.re);

  const lines = state.raw.split(/\r\n|\r|\n/);
  const frag = document.createDocumentFragment();
  const lineEls = [];
  const matches = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const div = document.createElement('div');

    if (/^=+\s/.test(line)) {            // 分隔行 ===== container: x =====
      div.className = 'lv-line lv-sep';
      div.innerHTML = '<span class="lv-text">' + escapeHtml(line) + '</span>';
      frag.appendChild(div);
      lineEls.push(div);
      continue;
    }

    div.className = 'lv-line';
    if (levelOn) {
      const lv = detectLevel(line);
      if (lv) div.classList.add('lev-' + lv);
    }
    if (state.re && state.re.test(line)) matches.push(i);

    const no = lineno ? '<span class="lv-no">' + (i + 1) + '</span>' : '';
    const textHtml = highlight(line, hlRe);
    div.innerHTML = no + '<span class="lv-text">' + textHtml + '</span>';
    frag.appendChild(div);
    lineEls.push(div);
  }

  body.innerHTML = '';
  body.appendChild(frag);
  body.classList.toggle('wrap', $('lv-wrap').checked);
  $('lv-log').style.fontSize = state.font + 'px';

  // 更新匹配状态
  state.lineEls = lineEls;
  state.matches = matches;
  if (matches.length) {
    if (state.cur < 0 || state.cur >= matches.length) state.cur = 0;
    const el = lineEls[matches[state.cur]];
    if (el) el.classList.add('lv-curmatch');
  } else {
    state.cur = -1;
  }
  updateMatchUI();
}

function updateMatchUI() {
  const m = state.matches.length;
  $('lv-match').textContent = (m ? state.cur + 1 : 0) + '/' + m;
}
function scrollToMatch() {
  const el = state.lineEls[state.matches[state.cur]];
  if (el) el.scrollIntoView({ block: 'center' });
}
function gotoMatch(dir) {
  if (!state.matches.length) return;
  state.lineEls.forEach(e => e.classList.remove('lv-curmatch'));
  state.cur = (state.cur + dir + state.matches.length) % state.matches.length;
  const el = state.lineEls[state.matches[state.cur]];
  if (el) { el.classList.add('lv-curmatch'); el.scrollIntoView({ block: 'center' }); }
  updateMatchUI();
}

/* ---------- 搜索 ---------- */
let searchDebounce = null;
function doSearch() {
  const q = $('lv-search').value;
  if (!q) {
    state.re = null; state.matches = []; state.cur = -1; render(); return;
  }
  let pattern;
  if ($('lv-regex').checked) {
    pattern = q;
  } else {
    pattern = escapeRegExp(q);
  }
  try {
    const flags = $('lv-ci').checked ? 'i' : '';
    state.re = new RegExp(pattern, flags);
  } catch (_) {
    state.re = null; render(); return;
  }
  state.cur = -1;
  render();
  if (state.matches.length) { state.cur = 0; scrollToMatch(); }
}

/* ---------- 自动刷新 ---------- */
function setAuto(sec) {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  state.autoFollow = sec > 0;
  if (sec > 0) state.timer = setInterval(refresh, sec * 1000);
}

/* ---------- 下载 ---------- */
function download() {
  if (!state.raw) return;
  const blob = new Blob([state.raw], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (params.pod || 'pod') + (params.container ? '__' + params.container : '') + '.log';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* ---------- 字号 ---------- */
function applyFont() { $('lv-log').style.fontSize = state.font + 'px'; }

/* ---------- 事件绑定 ---------- */
function bind() {
  $('lv-pod').onchange = switchPod;
  $('lv-container').onchange = e => { params.container = e.target.value; refresh(); };
  $('lv-tail').onchange = refresh;
  $('lv-prevlog').onchange = refresh;
  $('lv-wrap').onchange = () => $('lv-log').classList.toggle('wrap', $('lv-wrap').checked);
  $('lv-lineno').onchange = render;
  $('lv-level').onchange = render;

  $('lv-search').oninput = () => {
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(doSearch, 200);
  };
  $('lv-regex').onchange = doSearch;
  $('lv-ci').onchange = doSearch;
  $('lv-prev').onclick = () => gotoMatch(-1);
  $('lv-next').onclick = () => gotoMatch(1);

  $('lv-auto').onchange = () => setAuto(parseInt($('lv-auto').value, 10));
  $('lv-refresh').onclick = refresh;
  $('lv-download').onclick = download;
  $('lv-back').onclick = () => { if (window.opener) window.close(); else history.back(); };
  $('lv-theme').onclick = () => applyTheme(document.body.classList.contains('dark') ? 'light' : 'dark');
  $('lv-font-inc').onclick = () => { state.font = Math.min(22, state.font + 1); applyFont(); };
  $('lv-font-dec').onclick = () => { state.font = Math.max(10, state.font - 1); applyFont(); };
}

/* ---------- 启动 ---------- */
initTheme();
applyParamsToUI();
bind();
applyFont();
loadPods().finally(() => loadContainers().finally(refresh));
