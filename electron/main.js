/**
 * Electron 主进程
 *
 * 日志策略：
 *  1) console.log/error — 输出到终端（启动 Electron 的控制台）
 *  2) 同时写入 logs/electron-YYYYMMDD.log — 方便事后排错
 *  3) 主进程 -> 渲染进程：通过 IPC "log:append" 把后端日志和主进程日志推到前端
 *  4) 渲染进程 -> 主进程：通过 IPC "log:from-renderer" 让前端日志也落盘
 */
const electron = require('electron');
const { app, BrowserWindow, dialog, ipcMain, clipboard, Menu, shell } = electron;
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const http = require('http');
const net = require('net');

let pyProc = null;
let mainWindow = null;
let BACKEND_PORT = 8787;
let BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const isDev = process.argv.includes('--dev');

// ---- 进程守卫：后端意外退出时自动重启（指数退避） ----
// OOM / 未捕获异常 / 崩溃都会触发；主动 quit 或用户点击「停止后端」不重启。
let backendRestartCount = 0;      // 连续重启次数（用于退避与上限）
let backendRestartTimer = null;   // 退避定时器
const BACKEND_MAX_RESTARTS = 8;   // 单次会话最多自动重启次数，超过后弹窗提示
const BACKEND_RESTART_BASE_MS = 1500;

// 退出当前后端进程；restart=false 时仅清理（app 退出场景）
function stopPythonBackend() {
  if (backendRestartTimer) {
    clearTimeout(backendRestartTimer);
    backendRestartTimer = null;
  }
  const p = pyProc;
  pyProc = null;                  // 置 null 标记「主动停止」，exit 回调不再触发重启
  if (p && p.exitCode === null && p.signalCode === null) {
    try { p.kill('SIGTERM'); } catch (_) {}
  }
}

function scheduleBackendRestart(code, signal) {
  if (backendRestartTimer) return; // 已在排队
  backendRestartCount += 1;
  if (backendRestartCount > BACKEND_MAX_RESTARTS) {
    logErr(`后端已连续退出 ${BACKEND_MAX_RESTARTS} 次，停止自动重启。请检查日志：${LOG_FILE}`);
    dialog.showErrorBox(
      '后端持续崩溃',
      `Python API 服务器在短时间内反复退出（已尝试自动重启 ${BACKEND_MAX_RESTARTS} 次）。\n` +
      `最近一次：code=${code}, signal=${signal}\n请查看日志：${LOG_FILE}`
    );
    return;
  }
  const delay = BACKEND_RESTART_BASE_MS * Math.min(2 ** (backendRestartCount - 1), 16); // 1.5s→3s→6s→12s→24s…
  log(`后端退出（code=${code}, signal=${signal}），${Math.round(delay / 1000)}s 后自动重启（第 ${backendRestartCount} 次）`);
  backendRestartTimer = setTimeout(() => {
    backendRestartTimer = null;
    log(`自动重启后端（第 ${backendRestartCount} 次）…`);
    try {
      startPythonBackend();
      waitForBackend(60).then(() => {
        log('后端自动重启成功，/api/status 就绪。');
        backendRestartCount = 0;  // 恢复成功后重置计数
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send('backend:status', { status: 'up', restarts: backendRestartCount });
        }
      }).catch((err) => {
        logErr(`自动重启后后端仍未就绪：${err.message}`);
      });
    } catch (err) {
      logErr(`自动重启失败：${err.message}`);
    }
  }, delay);
}

// ---- 日志：终端 + 文件 + 渲染进程广播 ----
const PROJECT_ROOT = path.join(__dirname, '..');

// 运行时数据目录：与后端 core/app_paths.get_data_root() 对齐。
// - 打包态：~/.jira-git-gui（可写，避免写入只读的 app 包）
// - 开发态：项目根
function getDataDir() {
  // app.isPackaged may be undefined during module load; fallback to false for dev mode
  const isPacked = app?.isPackaged ?? false;
  if (isPacked) {
    return path.join(os.homedir(), '.jira-git-gui');
  }
  return PROJECT_ROOT;
}

// 端口探测：优先 8787，被占用则向后顺延，最多尝试 20 个（与 Tauri 版行为一致）。
// 返回实际可用端口（Promise<number>）。
function pickFreePort(startPort = 8787, maxAttempts = 20) {
  return new Promise((resolve, reject) => {
    let attempt = 0;
    const tryPort = (port) => {
      attempt += 1;
      const server = net.createServer();
      server.unref();
      server.on('error', () => {
        try { server.close(); } catch (_) {}
        if (attempt >= maxAttempts) {
          reject(new Error(`未找到可用端口：从 ${startPort} 起尝试 ${maxAttempts} 个均被占用`));
        } else {
          tryPort(port + 1);
        }
      });
      server.listen(port, '127.0.0.1', () => {
        const used = server.address().port;
        // 探测成功即释放，随后由 Python 后端占用；竞态窗口极小，可接受
        server.close(() => resolve(used));
      });
    };
    tryPort(startPort);
  });
}

// Initialize paths lazily to avoid accessing app before it's ready
let DATA_DIR = null;
let LOG_DIR = null;
let LOG_FILE = null;

function initializePaths() {
  if (DATA_DIR) return; // Already initialized
  DATA_DIR = getDataDir();
  LOG_DIR = path.join(DATA_DIR, 'logs');
  if (!fs.existsSync(LOG_DIR)) {
    try { fs.mkdirSync(LOG_DIR, { recursive: true }); } catch (_) {}
  }
  LOG_FILE = path.join(
    LOG_DIR,
    `electron-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}.log`
  );
}

function _ts() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}

function _logRaw(line) {
  const text = `${_ts()} ${line}`;
  process.stdout.write(text + '\n');
  try {
    fs.appendFileSync(LOG_FILE, text + '\n', 'utf8');
  } catch (_) {}
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send('log:append', { text }); } catch (_) {}
  }
}

const log = (msg, lvl = 'info') => _logRaw(`[main] [${lvl}] ${msg}`);
const logErr = (msg) => _logRaw(`[main] [error] ${msg}`);
const logPython = (stream, chunk) => {
  const tag = stream === 'stdout' ? 'py:out' : 'py:err';
  const lines = chunk.toString().replace(/\n$/, '').split('\n');
  for (const line of lines) {
    if (!line.trim()) continue;  // 跳过纯空行
    _logRaw(`[${tag}] ${line}`);
  }
};

log(`========== Jira Git GUI (Electron) 启动 ==========`);
log(`Node.js: ${process.version}  Electron: ${process.versions.electron ?? 'unknown'}  Platform: ${process.platform}`);
log(`Project root: ${PROJECT_ROOT}`);
log(`Log file: ${LOG_FILE}`);
log(`Backend URL: http://127.0.0.1:8787 (默认端口，被占用时自动顺延)  Dev mode: ${isDev}`);

// Check if we're running in a proper Electron environment
if (!app || typeof app.whenReady !== 'function') {
  logErr('ERROR: Electron app module not properly loaded.');
  logErr('Possible causes: Running in VS Code sandbox, or improper Electron setup.');
  logErr('To run Electron from command line:');
  logErr('  1. Open a native terminal (not VS Code terminal)');
  logErr('  2. cd /Users/caozhaoqi/PycharmProjects/jira-git-gui');
  logErr('  3. ./scripts/run_web.sh --electron');
  process.exit(1);
}

// ---- 首选项菜单：打开服务配置管理页（Jira / HCM / 云函数） ----
// 以「独立窗口」打开：主窗口始终保持主界面，首选项窗口改完直接关闭即返回。
// 配置在保存时已即时落盘，关闭窗口不会丢失任何修改。
function openPreferences() {
  const prefWin = new BrowserWindow({
    width: 1080,
    height: 760,
    minWidth: 800,
    minHeight: 560,
    title: '首选项 · 系统配置',
    parent: mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined,
    modal: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    }
  });
  prefWin.loadURL(`${BACKEND_URL}/services-config`);
  prefWin.on('closed', () => {
    log('首选项窗口已关闭。');
  });
}

// ---- HCM 元数据浏览器：独立窗口打开 web/hcm-meta.html ----
// 主窗口始终保持主界面；元数据浏览器窗口可独立关闭返回，便于搜索 / 排查。
function openHcmMeta() {
  const metaWin = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 860,
    minHeight: 560,
    title: 'HCM 元数据浏览器',
    parent: mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined,
    modal: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    }
  });
  metaWin.loadURL(`${BACKEND_URL}/hcm-meta`);
  metaWin.on('closed', () => {
    log('HCM 元数据窗口已关闭。');
  });
}

function buildAppMenu() {
  const isMac = process.platform === 'darwin';
  const template = [];

  if (isMac) {
    template.push({
      label: app.name || 'Jira Git GUI',
      submenu: [
        { label: '关于 Jira Git GUI', role: 'about' },
        { type: 'separator' },
        { label: '首选项…', accelerator: 'CmdOrCtrl+,', click: openPreferences },
        // { label: 'HCM 元数据…', accelerator: 'CmdOrCtrl+Shift+M', click: openHcmMeta },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { label: '退出', role: 'quit' },
      ],
    });
  }

  // 设置（非 macOS 下作为顶层菜单承载首选项 / HCM 元数据）
  const prefItem = { label: '首选项…', click: openPreferences };
  if (!isMac) prefItem.accelerator = 'Ctrl+,';
  const hcmItem = { label: 'HCM 元数据…', click: openHcmMeta };
  if (!isMac) hcmItem.accelerator = 'Ctrl+Shift+M';
  template.push({ label: '设置', submenu: [prefItem, hcmItem] });

  // 编辑（标准角色，保证复制 / 粘贴等可用）
  template.push({
    label: '编辑',
    submenu: [
      { role: 'undo' }, { role: 'redo' },
      { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
    ],
  });

  // 视图
  template.push({
    label: '视图',
    submenu: [
      { role: 'reload' },
      { role: 'toggleDevTools' },
      { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' },
      { role: 'togglefullscreen' },
    ],
  });

  // 帮助
  template.push({
    label: '帮助',
    submenu: [
      { label: '打开日志文件', click: () => { try { shell.openPath(LOG_FILE); } catch (_) {} } },
      { type: 'separator' },
      { label: '关于 Jira Git GUI', role: 'about' },
    ],
  });

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---- IPC Handlers (registered in app.whenReady) ----
function registerIpcHandlers() {
  ipcMain.on('log:from-renderer', (_ev, payload) => {
    const { level = 'info', msg } = payload || {};
    _logRaw(`[renderer] [${level}] ${msg ?? ''}`);
  });

  ipcMain.handle('app:get-info', () => ({
    platform: process.platform,
    isElectron: true,
    backendUrl: BACKEND_URL,
    logFile: LOG_FILE,
    isDev,
  }));
  // 剪贴板：用 Electron 原生 clipboard 模块（不受浏览器 clipboard-read 权限限制）
  ipcMain.handle('clipboard:read-text', () => clipboard.readText());
  ipcMain.handle('clipboard:write-text', (_ev, text) => {
    clipboard.writeText(String(text ?? ''));
    return true;
  });
}

// ---- Python 后端启动 ----
// 返回后端启动命令：打包态用冻结后的单文件可执行；开发态用 venv 里的 python。
function getBackendLaunch() {
  const port = String(BACKEND_PORT);
  const isPacked = app?.isPackaged ?? false;
  if (isPacked) {
    const exeName = process.platform === 'win32' ? 'jira-git-backend.exe' : 'jira-git-backend';
    const cmd = path.join(process.resourcesPath, 'backend', exeName);
    return { cmd, args: ['--port', port] };
  }
  // 开发态 Python：Windows 的 venv 布局是 Scripts/python.exe，macOS/Linux 是 bin/python；
  // venv 不存在时回退到 PATH 上的 python/python3，避免直接 spawn 失败。
  const venvRoot = path.join(PROJECT_ROOT, 'venv');
  const winPy = path.join(venvRoot, 'Scripts', 'python.exe');
  const unixPy = path.join(venvRoot, 'bin', 'python');
  let cmd;
  if (fs.existsSync(winPy)) {
    cmd = winPy;
  } else if (fs.existsSync(unixPy)) {
    cmd = unixPy;
  } else {
    cmd = process.platform === 'win32' ? 'python' : 'python3';
  }
  return { cmd, args: ['-m', 'api.server', '--port', port] };
}

function startPythonBackend() {
  const { cmd, args } = getBackendLaunch();

  log(`Launching backend: ${cmd} ${args.join(' ')}`);

  pyProc = spawn(cmd, args, {
    cwd: PROJECT_ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    // 把数据目录透传给后端，使前后端日志 / 缓存落在同一可写目录
    env: { ...process.env, JIRA_GIT_DATA_DIR: DATA_DIR },
  });

  log(`Python pid=${pyProc.pid}`);

  pyProc.stdout.on('data', (data) => logPython('stdout', data));
  pyProc.stderr.on('data', (data) => logPython('stderr', data));

  pyProc.on('spawn', () => log('Python 进程已 spawn。'));

  pyProc.on('exit', (code, signal) => {
    logErr(`Python 退出 code=${code} signal=${signal} pid=${pyProc?.pid ?? 'n/a'}`);
    const wasIntentional = pyProc === null;  // 我们主动 kill 后置 null
    pyProc = null;
    if (wasIntentional) {
      log('后端为主动停止，不自动重启。');
      return;
    }
    // 意外退出 → 进程守卫自动重启（OOM / 异常 / 崩溃）
    scheduleBackendRestart(code, signal);
  });

  pyProc.on('error', (err) => {
    logErr(`启动 Python 失败：${err.message}`);
    dialog.showErrorBox('后端启动失败', `无法启动 Python 后端：${err.message}\n\n日志位置：${LOG_FILE}`);
  });

  return pyProc;
}

function waitForBackend(maxRetries = 30) {
  return new Promise((resolve, reject) => {
    let retries = 0;

    const check = () => {
      const req = http.get(`${BACKEND_URL}/api/status`, (res) => {
        if (res.statusCode === 200) {
          log(`/api/status 200 响应，后端就绪（重试 ${retries} 次）`);
          resolve();
        } else {
          // 读取 body 便于诊断（有时返回 500 带错误）
          let body = '';
          res.on('data', (c) => { body += c; });
          res.on('end', () => {
            log(`/api/status 返回 HTTP ${res.statusCode}（重试 ${retries}）。响应头=${JSON.stringify(res.headers)}，body=${body.slice(0,200)}`);
            retry();
          });
        }
        res.resume();
      });

      req.on('error', (err) => {
        // 连接被拒绝等，属于正常未就绪，不要打印整段 trace
        if (retries === 0 || retries % 5 === 4) {
          log(`/api/status 尚未可达（重试 ${retries}，原因：${err.code || err.message}）`);
        }
        retry();
      });

      req.setTimeout(800, () => {
        req.destroy();
        retry();
      });
    };

    const retry = () => {
      retries += 1;
      if (retries >= maxRetries) {
        reject(new Error(`后端在 ${maxRetries} 次重试后仍未就绪（约 ${Math.round(maxRetries * 0.5)} 秒）。请查看 ${LOG_FILE}`));
      } else {
        setTimeout(check, 500);
      }
    };

    check();
  });
}

function createWindow() {
  log(`创建 BrowserWindow 1280x800`);
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    title: 'Jira Git GUI',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    }
  });

  log(`加载 ${BACKEND_URL}/`);
  mainWindow.loadURL(`${BACKEND_URL}/`);

  if (isDev) {
    log('Dev 模式：自动打开 DevTools');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url) => {
    logErr(`加载页面失败 code=${code} desc=${desc} url=${url}`);
  });

  mainWindow.webContents.on('did-finish-load', () => {
    log(`页面加载完成：${BACKEND_URL}/`);
  });

  mainWindow.on('closed', () => {
    log('主窗口已关闭。');
    mainWindow = null;
  });
}

// Only set up event listeners if app is available (it should be after require)
if (app && app.whenReady) {
  // 单实例锁：第二次启动时聚焦已有窗口并退出新实例，
  // 避免双份后端进程/双份日志写入同一数据目录（与 Tauri 版行为一致）。
  if (!app.requestSingleInstanceLock()) {
    log('已有实例在运行，退出当前实例');
    app.quit();
  } else {
    app.on('second-instance', () => {
      log('second-instance：聚焦已有窗口');
      if (mainWindow) {
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.focus();
      }
    });
  }

  app.whenReady().then(async () => {
    initializePaths(); // Initialize paths now that app is ready

    // 端口探测：8787 被占用时顺延（与 Tauri 版行为一致），避免启动即失败
    try {
      BACKEND_PORT = await pickFreePort();
      BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
      log(`后端端口探测完成：${BACKEND_PORT}`);
    } catch (err) {
      logErr(`端口探测失败：${err.message}`);
      dialog.showErrorBox('端口不可用', `${err.message}\n\n请释放 8787 附近的端口后重试。`);
      app.quit();
      return;
    }

    registerIpcHandlers(); // Register IPC handlers
    buildAppMenu();        // 构建原生菜单（含「首选项…」）
    log('app.whenReady() 触发');
    startPythonBackend();

  const timeoutMs = 15000;
  const timeoutPromise = new Promise((_, reject) => {
    setTimeout(() => reject(new Error(`等待后端就绪超时（${timeoutMs / 1000} 秒）`)), timeoutMs);
  });

  try {
    await Promise.race([waitForBackend(30), timeoutPromise]);
    log('后端就绪，创建窗口。');
    createWindow();
  } catch (err) {
    logErr(err.message);
    dialog.showErrorBox('后端启动失败', `${err.message}\n\n日志文件：${LOG_FILE}`);
    stopPythonBackend();
    app.quit();
  }
});
} // Close the if (app && app.whenReady) block

if (app) {
  app.on('before-quit', () => {
    log('app before-quit：关闭 Python 后端');
    stopPythonBackend();
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
      log('window-all-closed：退出应用');
      app.quit();
    }
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      log('activate：重建窗口');
      if (pyProc) {
        createWindow();
      } else {
        (async () => {
          startPythonBackend();
          try {
            await Promise.race([
              waitForBackend(30),
              new Promise((_, reject) => setTimeout(() => reject(new Error('超时')), 15000))
            ]);
            createWindow();
          } catch (err) {
            dialog.showErrorBox('后端启动失败', `${err.message}\n\n日志文件：${LOG_FILE}`);
            app.quit();
          }
        })();
      }
    }
  });
}
