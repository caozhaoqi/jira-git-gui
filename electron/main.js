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
const { app, BrowserWindow, dialog, ipcMain } = electron;
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const http = require('http');

let pyProc = null;
let mainWindow = null;
const BACKEND_PORT = 8787;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const isDev = process.argv.includes('--dev');
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
log(`Backend URL: ${BACKEND_URL}  Dev mode: ${isDev}`);

// Check if we're running in a proper Electron environment
if (!app || typeof app.whenReady !== 'function') {
  logErr('ERROR: Electron app module not properly loaded.');
  logErr('Possible causes: Running in VS Code sandbox, or improper Electron setup.');
  logErr('To run Electron from command line:');
  logErr('  1. Open a native terminal (not VS Code terminal)');
  logErr('  2. cd /Users/caozhaoqi/PycharmProjects/jira-git-gui');
  logErr('  3. ./run_web.sh --electron');
  process.exit(1);
}

// ---- IPC Handlers (registered in app.whenReady) ----
function registerIpcHandlers() {
  ipcMain.on('log:from-renderer', (_ev, payload) => {
    const { level = 'info', msg } = payload || {};
    _logRaw(`[renderer] [${level}] ${msg ?? ''}`);
  });

  ipcMain.handle('log:get-path', () => LOG_FILE);
  ipcMain.handle('app:get-info', () => ({
    platform: process.platform,
    isElectron: true,
    backendUrl: BACKEND_URL,
    logFile: LOG_FILE,
    isDev,
  }));
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
  return { cmd: path.join(PROJECT_ROOT, 'venv', 'bin', 'python'), args: ['-m', 'api.server', '--port', port] };
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
    if (!wasIntentional && mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox(
        '后端已退出',
        `Python API 服务器意外退出（code=${code}, signal=${signal}）。\n请查看日志：${LOG_FILE}`
      );
      mainWindow.close();
    }
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
  app.whenReady().then(async () => {
    initializePaths(); // Initialize paths now that app is ready
    registerIpcHandlers(); // Register IPC handlers
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
    if (pyProc) {
      try { pyProc.kill('SIGTERM'); } catch (_) {}
      pyProc = null;
    }
    app.quit();
  }
});
} // Close the if (app && app.whenReady) block

if (app) {
  app.on('before-quit', () => {
    log('app before-quit：关闭 Python 后端');
    if (pyProc) {
      try { pyProc.kill('SIGTERM'); } catch (_) {}
      pyProc = null;
    }
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
