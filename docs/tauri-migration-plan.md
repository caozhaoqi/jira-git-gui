# Tauri 迁移方案

## 概述

将 Electron 桌面版迁移到 Tauri 2.x，运行时从 231MB 降到 ~3MB（系统 WebView），总包体从 247MB 降到 ~20MB。

前端 HTML/CSS/JS **完全复用**，仅需重写 Electron 主进程的少量 Rust 代码。

---

## 架构对比

```
Electron 架构                          Tauri 架构
───────                                ──────
┌─────────────────────┐                ┌─────────────────────┐
│  main.js (Node.js)  │                │  main.rs (Rust)     │
│  ├─ spawn 后端进程    │                │  ├─ spawn 后端进程    │
│  ├─ IPC 日志桥接      │                │  ├─ Tauri 命令       │
│  └─ 窗口管理          │                │  └─ 窗口管理          │
├─────────────────────┤                ├─────────────────────┤
│  preload.js         │                │  无需 preload        │
│  (contextBridge)    │                │  (Tauri 内置 invoke) │
├─────────────────────┤                ├─────────────────────┤
│  Chromium (231MB)   │                │  系统 WebView (0MB)  │
│  └─ web/ 前端        │                │  └─ web/ 前端 (复用)  │
├─────────────────────┤                ├─────────────────────┤
│  Python 后端 (16MB)  │                │  Python 后端 (16MB)  │
└─────────────────────┘                └─────────────────────┘
     总包体: 247MB                           总包体: ~20MB
```

---

## 文件变更清单

### 一、新增文件（需编写）

| 文件 | 说明 | 行数估算 |
|------|------|----------|
| `tauri/src-tauri/Cargo.toml` | Rust 依赖声明 | ~30 行 |
| `tauri/src-tauri/src/main.rs` | Rust 主进程（替代 electron/main.js） | ~120 行 |
| `tauri/src-tauri/tauri.conf.json` | Tauri 配置（窗口、资源、构建） | ~50 行 |
| `tauri/src-tauri/capabilities/default.json` | Tauri 2.x 权限声明 | ~10 行 |
| `tauri/src-tauri/icons/` | 应用图标（自动生成） | 0 行（CLI 生成） |

### 二、修改文件（小幅改动）

| 文件 | 改动内容 | 改动量 |
|------|----------|--------|
| `web/app.js` | 三处 `window.electronAPI` 替换为 `window.__TAURI__` 兼容层 | ~10 行 |
| `build/build.py` | 新增 `build_tauri()` 函数，集成 `cargo tauri build` | ~40 行 |
| `build.sh` | 无需改动（自动调用 build.py） | 0 行 |

### 三、不变文件（完全复用）

| 文件 | 说明 |
|------|------|
| `web/index.html` | 前端入口，无需改动 |
| `web/app.js` | 99% 不变，仅 3 处 Electron API 调用改为条件兼容 |
| `web/styles.css` | 样式完全不变 |
| `api/server.py` | 后端完全不变 |
| `core/*.py` | 核心逻辑完全不变 |
| `build/pyinstaller_backend.spec` | 后端打包完全不变 |
| `build/run_backend.py` | 后端启动入口不变 |

### 四、删除文件（Electron 专属）

| 文件 | 说明 |
|------|------|
| `electron/main.js` | 被 `tauri/src-tauri/src/main.rs` 替代 |
| `electron/preload.js` | Tauri 内置 IPC，无需 contextBridge |
| `electron/package.json` | 被 `tauri/src-tauri/Cargo.toml` 替代 |
| `electron/node_modules/` | 不再需要 npm 依赖 |

---

## 逐文件迁移说明

### 1. `electron/main.js` → `tauri/src-tauri/src/main.rs`

| Electron 功能 | Tauri 等价实现 |
|---------------|---------------|
| `spawn(cmd, args)` 启动 Python 后端 | `std::process::Command::new(cmd).args(args).spawn()` |
| `http.get()` 等待后端就绪 | `reqwest::get()` 轮询 `/api/status` |
| `BrowserWindow` 创建窗口 | `tauri::WebviewWindowBuilder` |
| `mainWindow.loadURL()` | `tauri.conf.json` 中 `frontendDist` 配置自动加载 |
| `ipcMain.handle('app:get-info')` | `#[tauri::command] fn get_app_info()` |
| `mainWindow.webContents.send('log:append')` | `window.app_handle().emit("log:append", payload)` |
| `dialog.showErrorBox()` | `tauri::api::dialog::message()` |
| `app.on('before-quit')` 杀进程 | `Drop` trait 或 `app.on_event(|e| ...)` |
| `app.on('activate')` 重建窗口 | Tauri 自带 macOS 窗口恢复 |
| `fs.appendFileSync()` 写日志 | `std::fs::OpenOptions::new().append(true)` |
| `process.platform` | `std::env::consts::OS` |
| `app.isPackaged` | `cfg!(not(debug_assertions))` |

### 2. `electron/preload.js` → 无需文件

Tauri 2.x 前端通过 `@tauri-apps/api` 直接调用 Rust 命令：

```javascript
// Electron 方式
const info = await window.electronAPI.getAppInfo();

// Tauri 方式
import { invoke } from '@tauri-apps/api/core';
const info = await invoke('get_app_info');
```

### 3. `web/app.js` 改动（仅 3 处）

**改动 1：日志桥接（第 59-61 行）**

```javascript
// 旧：Electron
if (window.electronAPI?.log) {
  try { window.electronAPI.log(level, msg); } catch (_) {}
}

// 新：兼容 Electron + Tauri + Web
if (window.__TAURI__) {
  try { await import('@tauri-apps/api/core').then(m => m.invoke('log_message', { level, msg })); } catch (_) {}
} else if (window.electronAPI?.log) {
  try { window.electronAPI.log(level, msg); } catch (_) {}
}
```

**改动 2：主进程日志接收（第 1267-1288 行）**

```javascript
// 旧：Electron
if (window.electronAPI?.onAppLog) { ... }

// 新：兼容
if (window.__TAURI__) {
  import('@tauri-apps/api/event').then(m => {
    m.listen('log:append', (event) => { /* 同 Electron 逻辑 */ });
  });
} else if (window.electronAPI?.onAppLog) { ... }
```

**改动 3：启动信息（第 1269-1273 行）**

```javascript
// 旧
window.electronAPI.getAppInfo?.().then(info => { ... });

// 新：兼容
const getInfo = window.__TAURI__
  ? import('@tauri-apps/api/core').then(m => m.invoke('get_app_info'))
  : window.electronAPI?.getAppInfo?.();
```

### 4. `build/build.py` 新增 `build_tauri()`

```python
def build_tauri(venv_py, os_name, no_deps):
    """构建 Tauri 桌面版"""
    print("\n=== 构建 Tauri 桌面版 (tauri) ===")
    if shutil.which("cargo") is None:
        print("  ✗ 未找到 Rust，请先安装 rustup (https://rustup.rs)")
        return False
    # 确保后端已构建
    if not (ROOT / "dist" / backend_exe_name(os_name)).exists():
        print("  ↳ 后端尚未构建，先构建 backend ...")
        if not build_backend(venv_py, os_name, no_deps):
            return False
    copy_backend_to_tauri(os_name)
    # 安装前端 npm 依赖（@tauri-apps/api）
    if not no_deps:
        run(["npm", "install", "@tauri-apps/api"], cwd=ROOT)
    # Tauri 构建
    run(["cargo", "tauri", "build"], cwd=TAURI_DIR)
    return True
```

---

## 构建流程对比

| 步骤 | Electron | Tauri |
|------|----------|-------|
| 1. 冻结后端 | `pyinstaller` → `dist/jira-git-backend` | 同 |
| 2. 复制后端 | 复制到 `electron/resources/backend/` | 复制到 `tauri/src-tauri/resources/` |
| 3. 安装前端依赖 | `npm install`（Electron） | `npm install @tauri-apps/api` |
| 4. 编译 Rust | 无 | `cargo build --release` |
| 5. 打包 | `electron-builder --mac` | `cargo tauri build` |
| 产物 macOS | `.app` + `.dmg`（247MB） | `.app` + `.dmg`（~20MB） |
| 产物 Windows | `.exe` 安装包（~250MB） | `.msi` 安装包（~20MB） |
| 产物 Linux | `.AppImage` + `.deb`（~250MB） | `.AppImage` + `.deb`（~20MB） |

---

## 前置依赖

| 工具 | macOS | Windows | Linux |
|------|-------|---------|-------|
| Rust | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` | 下载 rustup-init.exe | 同 macOS |
| Tauri CLI | `cargo install tauri-cli` | 同 | 同 |
| 系统依赖 | 无（WebKit 内置） | WebView2（Win10+ 已预装） | `apt install libwebkit2gtk-4.1-dev` |

---

## 风险与注意事项

1. **Rust 编译**：首次 `cargo build` 需要下载依赖（~200MB），需网络可达 `crates.io`
2. **WebView 兼容性**：Tauri 2.x 用系统 WebView，不同 OS 版本可能有细微渲染差异，但你的纯 CSS 界面兼容性很好
3. **Python 后端不变**：完全不影响现有后端逻辑，Tauri 仅负责启动进程 + 加载页面
4. **Electron 可保留**：两个版本可以共存，`build.sh --flavor electron` 和 `build.sh --flavor tauri` 互不干扰
5. **日志**：Tauri 的日志写入 `~/.jira-git-gui/logs/`，与 Electron 版对齐