// Electron 原生菜单模板的「不启动 Electron」校验。
//
// 为什么需要它：菜单模板里写错一个 accelerator 或 role，Electron 会在
// Menu.buildFromTemplate 时直接抛异常 —— 后果是整个应用菜单（含「首选项…」）
// 全部消失，而这类错误在 code review 里极难看出来（字符串长得都对）。
// 这里用 vm + 桩 electron 模块把 main.js 跑一遍，只调 buildAppMenu()，
// 拿到真实模板后逐项校验：顶层菜单齐全、accelerator 合法、role 合法、
// 调试菜单的开发者工具项确实挂了 click。
//
// 运行：node tests/test_electron_menu.mjs
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const MAIN_JS = path.join(ROOT, 'electron', 'main.js');

let failed = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

// ---- Electron 官方文档里的合法取值（用于校验模板） --------------------------- //
const MODIFIERS = new Set([
  'Command', 'Cmd', 'Control', 'Ctrl', 'CommandOrControl', 'CmdOrCtrl',
  'Alt', 'Option', 'AltGr', 'Shift', 'Super', 'Meta',
]);
const KEY_NAMES = new Set([
  'Plus', 'Space', 'Tab', 'Capslock', 'Numlock', 'Scrolllock', 'Backspace', 'Delete', 'Insert',
  'Return', 'Enter', 'Up', 'Down', 'Left', 'Right', 'Home', 'End', 'PageUp', 'PageDown',
  'Escape', 'Esc', 'VolumeUp', 'VolumeDown', 'VolumeMute', 'MediaNextTrack',
  'MediaPreviousTrack', 'MediaStop', 'MediaPlayPause', 'PrintScreen',
  ...Array.from({ length: 24 }, (_, i) => `F${i + 1}`),
  ...Array.from({ length: 10 }, (_, i) => `${i}`),
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''),
  // Electron 允许的标点键
  ',', '.', '/', '`', '-', '=', '[', ']', ';', "'", '\\',
]);
const ROLES = new Set([
  'undo', 'redo', 'cut', 'copy', 'paste', 'pasteAndMatchStyle', 'delete', 'selectAll',
  'reload', 'forceReload', 'toggleDevTools', 'resetZoom', 'zoomIn', 'zoomOut',
  'togglefullscreen', 'window', 'minimize', 'close', 'help', 'about', 'services',
  'hide', 'hideOthers', 'unhide', 'quit', 'startSpeaking', 'stopSpeaking',
  'front', 'zoom', 'toggleTabBar', 'selectNextTab', 'selectPreviousTab',
  'showAllTabs', 'mergeAllWindows', 'moveTabToNewWindow', 'appMenu', 'fileMenu',
  'editMenu', 'viewMenu', 'windowMenu',
]);

function acceleratorProblem(acc) {
  const parts = String(acc).split('+');
  const key = parts.pop();
  if (!key) return '缺少按键';
  if (!KEY_NAMES.has(key)) return `未知按键 "${key}"`;
  for (const m of parts) if (!MODIFIERS.has(m)) return `未知修饰键 "${m}"`;
  return null;
}

// ---- 桩 electron 模块 -------------------------------------------------------- //
let capturedTemplate = null;
let focusedWindow = null;
const openDevToolsCalls = [];
const whenReadyCalls = [];
const windowOptions = [];
const titleGuardHits = [];

function makeWebContents() {
  let opened = false;
  return {
    isDevToolsOpened: () => opened,
    openDevTools: (opts) => {
      opened = true;
      openDevToolsCalls.push({ kind: 'open', mode: opts?.mode });
    },
    closeDevTools: () => {
      opened = false;
      openDevToolsCalls.push({ kind: 'close' });
    },
    setWindowOpenHandler: () => {},
    on: () => {},
    send: () => {},
  };
}

function makeWindow(opts = {}) {
  windowOptions.push(opts);
  return {
    webContents: makeWebContents(),
    loadURL: () => {},
    on: (ev, fn) => {
      // 页面 <title> 更新会触发 page-title-updated；记录 preventDefault 是否被调用
      if (ev === 'page-title-updated') fn({ preventDefault: () => titleGuardHits.push(1) });
    },
    once: () => {},
    isDestroyed: () => false,
    isMinimized: () => false,
    restore: () => {},
    focus: () => {},
    show: () => {},
    maximize: () => {},
  };
}

const electronStub = {
  app: new Proxy(
    {
      name: 'Jira Git GUI',
      commandLine: { appendSwitch: () => {} },
      dock: { hide: () => {}, show: () => {} },
      isPackaged: false,
      // 返回 pending promise：模块级 whenReady().then(...) 的副作用（起后端、建窗口）永不执行
      whenReady: () => {
        whenReadyCalls.push(1);
        return new Promise(() => {});
      },
      // 返回 false → main.js 走 app.quit() 分支，不注册 whenReady 里的副作用
      requestSingleInstanceLock: () => false,
      getPath: () => ROOT,
      getAppPath: () => ROOT,
      quit: () => {},
      on: () => {},
      once: () => {},
    },
    {
      // 未显式桩到的 app.* 一律 no-op 函数，避免 main.js 新增调用时测试碎掉
      get: (target, prop) => {
        if (prop in target) return target[prop];
        if (typeof prop === 'string') return () => {};
        return undefined;
      },
    },
  ),
  BrowserWindow: Object.assign(function BrowserWindow(opts) { return makeWindow(opts); }, {
    getFocusedWindow: () => focusedWindow,
    getAllWindows: () => [],
  }),
  dialog: { showErrorBox: () => {}, showMessageBox: () => Promise.resolve({ response: 0 }) },
  ipcMain: { on: () => {}, handle: () => {} },
  clipboard: { writeText: () => {}, readText: () => '' },
  Menu: {
    buildFromTemplate: (tpl) => {
      capturedTemplate = tpl;
      return { __fakeMenu: true, template: tpl };
    },
    setApplicationMenu: () => {},
  },
  shell: { openPath: () => Promise.resolve(''), openExternal: () => Promise.resolve() },
  screen: { getPrimaryDisplay: () => ({ workAreaSize: { width: 1440, height: 900 } }) },
};

// ---- 在 vm 里跑 main.js（只执行模块级代码），再显式调用 buildAppMenu ---------- //
const nodeRequire = createRequire(import.meta.url);
const context = {
  require: (id) => (id === 'electron' ? electronStub : nodeRequire(id)),
  module: { exports: {} },
  exports: {},
  __dirname: path.join(ROOT, 'electron'),
  __filename: MAIN_JS,
  // 转发真实 process，但吞掉 main.js 启动日志（避免测试输出被 boot log 淹没）
  process: new Proxy(process, {
    get: (target, prop) => {
      if (prop === 'stdout' || prop === 'stderr') return { write: () => {}, isTTY: false };
      return Reflect.get(target, prop);
    },
  }),
  console: { log: () => {}, warn: () => {}, error: () => {}, info: () => {}, debug: () => {} },
  Buffer,
  URL,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  Promise,
  Date,
  Math,
  JSON,
  Error,
  Object,
  Array,
  String,
  Number,
  Boolean,
  RegExp,
  Map,
  Set,
};
context.globalThis = context;

const source = fs.readFileSync(MAIN_JS, 'utf8');
vm.runInNewContext(`${source}\n;buildAppMenu();`, context, { filename: MAIN_JS });

// ---- 断言 ------------------------------------------------------------------- //
console.log('buildAppMenu 产出的模板');
check('模板被真正构建（Menu.buildFromTemplate 被调用）', Array.isArray(capturedTemplate));
check('模块级 whenReady 副作用未执行（桩返回 pending）', whenReadyCalls.length === 1);

const topLabels = capturedTemplate.map((m) => m.label || `[${m.role || m.type}]`);
console.log(`  顶层菜单：${topLabels.join(' / ')}`);
for (const need of ['设置', '新窗口', '编辑', '视图', '调试', '帮助']) {
  check(`顶层菜单包含「${need}」`, topLabels.includes(need));
}

const debugMenu = capturedTemplate.find((m) => m.label === '调试');
check('调试菜单有子项', Array.isArray(debugMenu?.submenu) && debugMenu.submenu.length > 0);
const devToolsItem = debugMenu?.submenu?.find((i) => typeof i.label === 'string' && i.label.includes('开发者工具'));
check('调试菜单里有「开发者工具」项', Boolean(devToolsItem));
check('开发者工具项挂了 click 处理函数', typeof devToolsItem?.click === 'function');
check(
  '开发者工具项带快捷键（mac 用 Cmd+Alt+I，其余 F12）',
  ['Cmd+Alt+I', 'F12'].includes(devToolsItem?.accelerator),
  `实际 ${devToolsItem?.accelerator}`,
);
const detachItem = debugMenu?.submenu?.find((i) => typeof i.label === 'string' && i.label.includes('独立窗口'));
check('调试菜单里有「开发者工具（独立窗口）」项', typeof detachItem?.click === 'function');

// 全模板校验：accelerator 合法 / role 合法
const badAcc = [];
const badRole = [];
const walk = (items) => {
  for (const item of items || []) {
    if (item.accelerator) {
      const p = acceleratorProblem(item.accelerator);
      if (p) badAcc.push(`${item.label || item.role}: ${item.accelerator} (${p})`);
    }
    if (item.role && !ROLES.has(item.role)) badRole.push(`${item.label || ''}: ${item.role}`);
    if (item.submenu) walk(item.submenu);
  }
};
walk(capturedTemplate);
check('所有 accelerator 都是 Electron 合法写法', badAcc.length === 0, badAcc.join('; '));
check('所有 role 都是 Electron 合法角色', badRole.length === 0, badRole.join('; '));
check(
  '开发者工具不再与视图菜单的 toggleDevTools 重复',
  !capturedTemplate.find((m) => m.label === '视图')?.submenu?.some((i) => i.role === 'toggleDevTools'),
);

// 行为验证：点击「开发者工具」对聚焦窗口开/关 DevTools
console.log('开发者工具行为（桩窗口）');
focusedWindow = makeWindow();
devToolsItem.click();
check('首次点击 → openDevTools(mode=right)（类似 Chrome F12 停靠右侧）',
  openDevToolsCalls.at(-1)?.kind === 'open' && openDevToolsCalls.at(-1)?.mode === 'right');
devToolsItem.click();
check('再次点击 → closeDevTools()', openDevToolsCalls.at(-1)?.kind === 'close');
detachItem.click();
check('独立窗口项 → openDevTools(mode=detach)',
  openDevToolsCalls.at(-1)?.kind === 'open' && openDevToolsCalls.at(-1)?.mode === 'detach');
focusedWindow = null;
const beforeNoFocus = openDevToolsCalls.length;
devToolsItem.click();
check('无聚焦窗口时不抛异常', openDevToolsCalls.length === beforeNoFocus);

// 主窗口标题：用户要求标题栏不显示应用名
console.log('主窗口标题（createWindow）');
windowOptions.length = 0;
context.createWindow();
const mainOpts = windowOptions[0] || {};
check('主窗口 title 置空（标题栏不显示应用名）', mainOpts.title === '', `实际 ${JSON.stringify(mainOpts.title)}`);
check('主窗口仍保留最小尺寸等既有配置', mainOpts.minWidth === 900 && mainOpts.minHeight === 600);
check('主窗口注册了 page-title-updated 拦截', titleGuardHits.length > 0);
check(
  '拦截确实调用 preventDefault（否则 <title> 会把标题改回来）',
  titleGuardHits.every((x) => x === 1),
);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
process.exit(failed === 0 ? 0 : 1);
