# 打包与发布指南（jira-git-gui）

本项目**发布两种桌面形态**，两者共用同一个冻结后的 Python 后端（`api/server.py`）：

| 形态 | 入口 | 适用场景 | 打包方式 |
|------|------|---------|---------|
| **Electron 桌面版** | `electron/main.js` | 原生壳 + Chromium/WebKit 引擎，体验接近独立 App | electron-builder（内嵌冻结后的后端） |
| **Tauri 桌面版** | `tauri/src-tauri/` | 原生壳 + 系统 WebView，包体小（几十 MB） | `cargo tauri build`（内嵌冻结后的后端） |

> 跨平台结论：**可以跨平台，但必须按目标系统分别构建**。electron-builder 与 `cargo tauri`
> 均**不支持交叉编译**——macOS 出 `.app`/`.dmg`、Windows 出 `.exe`/`.msi`、Linux 出
> `AppImage`/`deb` 需在各自系统（或对应 CI runner）上构建。推荐用 GitHub Actions
> 三条 runner 自动出包（见 `.github/workflows/release.yml`）。

---

## 零、一键本地构建（推荐）

项目内置跨平台编排脚本 `build/build.py`，会**自动识别当前操作系统**并构建该平台的产物
（在 macOS 上出 `.app`/`.dmg`、Windows 上出 `.exe`/`.msi`、Linux 上出可执行/`AppImage`）。
缺失的 Python 依赖会被自动补装，Electron / Tauri 形态会自动先把冻结后端嵌入资源目录。

```bash
# macOS / Linux
./scripts/build.sh --flavor electron   # 构建 Electron 桌面版
./scripts/build.sh --flavor tauri      # 构建 Tauri 桌面版
./scripts/build.sh --flavor all        # 构建本机支持的全部发布形态
./scripts/build.sh --list              # 列出本机可构建形态

# Windows (PowerShell)
.\scripts\build.ps1 --flavor electron
.\scripts\build.ps1 --flavor tauri

# 也可直接调 Python（等价）
python build/build.py --flavor tauri
python build/build.py --no-deps    # 跳过依赖自动安装（假定已装好 requirements.txt + pyinstaller）
```

| 参数 | 含义 |
|------|------|
| `--flavor electron` | Electron 桌面版 |
| `--flavor tauri` | Tauri 桌面版 |
| `--flavor all` | 本机支持的全部发布形态（默认） |
| `--list` | 仅列出本机可构建形态 |
| `--no-deps` | 跳过依赖自动安装 |

> 脚本逻辑见 `build/build.py`；`scripts/build.sh` / `scripts/build.ps1` 只是切到项目根后转发参数的薄包装。
> **仍需按 OS 构建**：它不是交叉编译器，只负责「在本机一键出本机包」。三端齐发请用 CI。

---

## 一、前置准备（两种形态通用）

```bash
# 1) Python 依赖
python -m pip install -r requirements.txt
pip install pyinstaller                       # 冻结后端用

# 2) Node 依赖（仅 Electron 形态）
cd electron && npm install && cd ..

# 3) Rust 工具链（仅 Tauri 形态）
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   # 安装 rustup
cargo install tauri-cli                                        # 安装 Tauri CLI
```

- **Python 版本**：代码使用 PEP 604 联合类型（`X | None`），需 **Python ≥ 3.10**；
  CI 使用 `3.11`，本地建议 ≥ 3.10。
- **运行时数据目录**：冻结后程序位于只读包内，日志 / 缓存 / 下载 / 会话统一写到
  `~/.jira-git-gui/`（开发态写到项目根）。由 `core/app_paths.get_data_root()` 控制，
  可通过环境变量 `JIRA_GIT_DATA_DIR` 覆盖（桌面壳据此与后端对齐目录）。

---

## 二、冻结 Python 后端（两种形态共用的前置步骤）

Electron 与 Tauri 都不直接打包 Python 源码，而是把**冻结好的后端可执行**作为资源嵌入安装包。

```bash
pyinstaller build/pyinstaller_backend.spec --noconfirm
# 产出：dist/jira-git-backend（macOS / Linux）或 dist/jira-git-backend.exe（Windows）
# 浏览器打开 http://127.0.0.1:8787 即可验证
```

`web/` 静态资源与 `.env` 已被收集进包内（`sys._MEIPASS`），后端自动从包内加载。

---

## 三、Electron 桌面版

Electron 把**第二节冻结好的后端可执行**作为 `extraResource` 嵌入安装包。

```bash
# 1) 先冻结后端（见第二节），放到 Electron 资源目录
mkdir -p electron/resources/backend
cp dist/jira-git-backend electron/resources/backend/jira-git-backend   # Windows 用 .exe

# 2) 打包 Electron 安装包
cd electron
npm install
npm run dist:mac      # 或 dist:win / dist:linux
# 产出在 electron/dist-electron/
```

`electron/main.js` 的查找逻辑：
- **打包态**（`app.isPackaged`）：`process.resourcesPath/backend/jira-git-backend[.exe]`
- **开发态**：`venv/bin/python -m api.server`

---

## 四、Tauri 桌面版

Tauri 同样内嵌冻结后的后端，区别在于壳用 Rust 实现、窗口用系统 WebView。

```bash
# 1) 先冻结后端（见第二节），放到 Tauri 资源目录
mkdir -p tauri/src-tauri/resources/backend
cp dist/jira-git-backend tauri/src-tauri/resources/backend/jira-git-backend   # Windows 用 .exe

# 2) 打包 Tauri 安装包
./scripts/build-tauri.sh          # release 构建（macOS 出 .app/.dmg；Windows 出 .msi；Linux 出 .AppImage/.deb）
# 或等价：
cd tauri && cargo tauri build
```

> 注：Tauri 自带的 DMG 打包依赖 `create-dmg` 的 support 模板，未安装时 `cargo tauri build`
> 会在最后一步报 "failed to run bundle_dmg.sh"。此时可改用系统 `hdiutil` 兜底生成 DMG，
> `.app` 本身已构建成功。`scripts/build-tauri.sh` 已内置该兜底逻辑。

---

## 五、CI 一键发布

推送 `vX.Y.Z` 标签（或手动 `workflow_dispatch`）触发 `.github/workflows/release.yml`：
三条 runner（macos / windows / ubuntu）各自构建 **后端 + Electron + Tauri** 并上传 artifact。
产物的跨平台对应：

| Runner | 后端 | Electron | Tauri |
|--------|------|----------|-------|
| macos-latest | `jira-git-backend` | `*.dmg` | `*.app` + `*.dmg` |
| windows-latest | `jira-git-backend.exe` | `*-setup.exe` (nsis) | `*.msi` |
| ubuntu-latest | `jira-git-backend` | `*.AppImage` / `*.deb` | `*.AppImage` / `*.deb` |

---

## 六、已知限制 / 注意

- **代码签名**：CI 未配置证书，macOS/Windows 产物为「ad-hoc / 未签名」，首次打开会被
  Gatekeeper / SmartScreen 拦截，需用户手动允许。正式发布请配置 `codesign` / 证书。
- **Linux 系统库**：Electron 需 `libnss3` 等；Tauri 需系统 WebView 开发库（如
  `libwebkit2gtk-4.1-dev`）。CI 已 `apt-get install`，终端用户若缺库会启动失败。
- **不动原始字节**：无论哪种形态，合并时的写入内容永远来自**原始远程字节**，格式化/规范化
  仅用于 diff 展示，不会污染远端仓库风格。
- **`.env` 含敏感信息**：虽被收集进包，但建议正式发布时改用「连接设置」UI 录入，避免硬编码密钥。
- **PyQt6 桌面版（`main.py` / `gui/`）为遗留实现**：已不再作为发布目标，发行版仅 Electron + Tauri。
