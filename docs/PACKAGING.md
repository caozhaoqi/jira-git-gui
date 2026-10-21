# 打包与发布指南（jira-git-gui）

本项目**发布两种桌面形态**，两者共用同一个冻结后的 Python 后端（`api/server.py`）：

| 形态 | 入口 | 适用场景 | 打包方式 |
|------|------|---------|---------|
| **Electron 桌面版** | `electron/main.js` | 原生壳 + Chromium/WebKit 引擎，体验接近独立 App | electron-builder（内嵌冻结后的后端） |
| **Tauri 桌面版** | `tauri/src-tauri/` | 原生壳 + 系统 WebView，包体小（几十 MB） | `cargo tauri build`（内嵌冻结后的后端） |

> 跨平台结论：**支持跨平台，且 macOS 可交叉出 Windows 包（已验证）**。
> - **Electron**：macOS 上可直接交叉构建 Windows `.exe`（NSIS）——electron-builder 自带 NSIS 工具链，无需 Wine。
> - **Tauri**：macOS 上可通过实验性 GNU target（`x86_64-pc-windows-gnu` + mingw-w64）交叉构建 Windows NSIS 包。
> - **仍需在 Windows（或对应 CI runner）构建的**：`.msi`（WiX，两种形态都是）、MSVC target 的 Tauri 构建（官方推荐路线）。
> - 推荐用 GitHub Actions 三条 runner 自动出全平台产物（见 `.github/workflows/release.yml`）。
> - macOS → Windows 交叉构建的完整命令与镜像配置见下方「交叉编译（macOS → Windows）」小节。

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

## 〇·五、交叉编译（macOS → Windows，已验证 2026-08）

在 Apple Silicon / Intel Mac 上可直接产出 Windows x64 安装包，无需 Windows / Wine。

### Electron（推荐，最简单）

```bash
cd electron
# 国内镜像（关键！不加会在下载 winCodeSign/NSIS 时 GitHub EOF 反复失败）
export ELECTRON_MIRROR="https://registry.npmmirror.com/-/binary/electron/"
export ELECTRON_BUILDER_BINARIES_MIRROR="https://registry.npmmirror.com/-/binary/electron-builder-binaries/"

# 必须显式 --x64！Apple Silicon 默认打 win-arm64，多数 Windows 机器跑不了
npm run dist:win      # = electron-builder --win --x64
# 产物：dist-electron/JiraGitGUI-<version>-setup.exe
```

- 坑：`win.arch` 不是合法 schema 字段，指定架构必须用 CLI `--x64`，不要写进 `build.win`。
- `.msi`（WiX）仍需 Windows / CI；NSIS `.exe`、`zip` 便携版可本机直出。

### Tauri（实验性 GNU target）

```bash
# 一次装齐
brew install mingw-w64 nsis          # llvm 非必需（GNU 用 mingw gcc 链接）
rustup target add x86_64-pc-windows-gnu

# ~/.cargo/config.toml（GNU 链接器 + 国内 crates 镜像）
# [target.x86_64-pc-windows-gnu]
# linker = "x86_64-w64-mingw32-gcc"
# ar = "x86_64-w64-mingw32-ar"
# rustflags = ["-C", "linker=x86_64-w64-mingw32-gcc"]
# [source.crates-io]
# replace-with = "mirror"
# [source.mirror]
# registry = "sparse+https://rsproxy.cn/index/"

cd tauri
cargo tauri build --target x86_64-pc-windows-gnu
# 产物：src-tauri/target/x86_64-pc-windows-gnu/release/app.exe
#        + bundle/nsis/JiraGitGUI_<version>_x64-setup.exe
```

- 坑：**不要给 mingw linker 传 MSVC 风格参数**（`-Wl,/NXCOMPAT`、`-fuse-ld=lld`）→ 报 `cannot find /NXCOMPAT`。GNU target 裸 `linker = "x86_64-w64-mingw32-gcc"` 即可。
- 坑：Rust 装在 `~/.cargo/bin`，非登录 shell 需 `export PATH="$HOME/.cargo/bin:$PATH"`。
- 警告 `Wrong package type msi for platform macOS` 无害（`targets:"all"` 在 macOS 无法产 msi）。
- MSVC target（官方推荐）在 macOS 需 `cargo-xwin`，配置更重；GNU 适合快速自测。
- 完整细节见 skill：`~/.workbuddy/skills/macos-win-cross-compile/SKILL.md`。

### 功能可用性提醒

交叉构建只解决「Windows 包能打出来」。**`resources/backend/` 里的冻结后端是 macOS Mach-O，Windows 包内不会运行**——正式交付前需在 Windows / CI 上用 PyInstaller 冻结 `jira-git-backend.exe`（见「二、冻结 Python 后端」）并重新打包。无签名产物 SmartScreen 会警告「未知发布者」。

---

## 一、前置准备（两种形态通用）

```bash
# 1) Python 依赖
python -m pip install -r requirements.txt
pip install pyinstaller                       # 冻结后端用

# 2) Node.js/npm（后端需要它构建 React 前端；Electron 还需 Electron 依赖）
cd frontend/web-react && npm ci && cd ../..
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
冻结前先构建 React 前端。PyInstaller 会把 `frontend/web-react/dist` 放进可执行包的同一路径，
供 `api/server.py` 的静态资源路由读取。若 React 产物缺失或不完整，PyInstaller 会直接停止，
不会回退到旧版 `web/` 生成启动后 bundle 404 的白屏包。

```bash
npm ci --prefix frontend/web-react
npm run build --prefix frontend/web-react
pyinstaller build/pyinstaller_backend.spec --noconfirm
# 产出：dist/jira-git-backend（macOS / Linux）或 dist/jira-git-backend.exe（Windows）
# 浏览器打开 http://127.0.0.1:8787 即可验证
```

仅前端静态资源与无敏感信息的示例 JSON 会被打进包。`.env`、账号密码和 Cookie 均不会打包；用户凭据保存在操作系统凭据存储中（macOS Keychain、Windows Credential Manager/DPAPI、Linux Secret Service）。

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
CI 先在单独 job 中构建一次 React 前端，再把同一份静态资源交给 macOS、Windows、Linux
runner 打进各自的后端和桌面包；平台 runner 随后分别构建 **后端 + Electron + Tauri** 并上传 artifact。
因此同一次 CI 运行中的应用页面使用完全相同的前端资源；系统窗口标题栏仍按操作系统原生显示。
产物的跨平台对应：

| Runner | 后端 | Electron | Tauri |
|--------|------|----------|-------|
| macos-latest | `jira-git-backend` | `*.dmg` | `*.app` + `*.dmg` |
| windows-latest | `jira-git-backend.exe` | `*-setup.exe` (nsis) | `*.msi` |
| ubuntu-latest | `jira-git-backend` | `*.AppImage` / `*.deb` | `*.AppImage` / `*.deb` |

### CI 代码签名

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中配置下列 secrets。证书只在对应 runner 的临时目录和证书存储中使用，不要提交到仓库。

**macOS（Electron + Tauri）**

| Secret | 内容 |
|--------|------|
| `APPLE_CERTIFICATE` | Developer ID Application `.p12` 证书的单行 Base64 |
| `APPLE_CERTIFICATE_PASSWORD` | 导出 `.p12` 时设置的密码 |
| `KEYCHAIN_PASSWORD` | CI 临时 keychain 的自定义密码 |
| `APPLE_SIGNING_IDENTITY` | 证书完整身份名，可用 `security find-identity -v -p codesigning` 查询 |

签名所需四项齐全后，macOS job 会导入证书并签署 Electron 与 Tauri 包。公证是可选的；若配置则必须同时设置 `APPLE_ID`、`APPLE_PASSWORD`（Apple app-specific password）和 `APPLE_TEAM_ID`。workflow 会把 `APPLE_PASSWORD` 映射到 Electron Builder 所需的 `APPLE_APP_SPECIFIC_PASSWORD`。

导出 Developer ID Application 证书为 `.p12` 后，可用 `base64 -i certificate.p12 | tr -d '\n'` 生成 `APPLE_CERTIFICATE` 的单行值；不要把导出的证书或密码写入仓库文件。

**Windows（Electron + Tauri）**

| Secret | 内容 |
|--------|------|
| `WINDOWS_PFX_BASE64` | Windows 代码签名 `.pfx` 文件的单行 Base64 |
| `WINDOWS_PFX_PASSWORD` | `.pfx` 密码 |
| `WINDOWS_CERT_THUMBPRINT` | 证书 thumbprint（不含空格） |

三项齐全后，Windows job 会把 PFX 导入当前用户证书存储；Electron Builder 使用 PFX，Tauri 使用 thumbprint、SHA-256 和 DigiCert 时间戳服务签名。任一平台的签名 secrets 完全未配置时，该平台仍构建未签名产物；只配置了部分必需项则 job 会明确失败，避免产生误认为已签名的包。

在 PowerShell 中可用 `[Convert]::ToBase64String([IO.File]::ReadAllBytes("certificate.pfx"))` 生成 `WINDOWS_PFX_BASE64`；thumbprint 可在 Windows 证书管理器中查看。

---

## 六、已知限制 / 注意

- **代码签名**：CI 签名取决于是否配置上述证书 secrets；未配置时 macOS/Windows 产物未签名，系统可能显示 Gatekeeper / SmartScreen 警告。只在确认下载来源可信、校验发布校验和后手动放行：macOS 可在「系统设置 → 隐私与安全性」中对该应用选择“仍要打开”；Windows 在 SmartScreen 页面选择“更多信息 → 仍要运行”。不要为此关闭系统级安全保护。
- **Linux 系统库**：Electron 需 `libnss3` 等；Tauri 需系统 WebView 开发库（如
  `libwebkit2gtk-4.1-dev`）。CI 已 `apt-get install`，终端用户若缺库会启动失败。
- **不动原始字节**：无论哪种形态，合并时的写入内容永远来自**原始远程字节**，格式化/规范化
  仅用于 diff 展示，不会污染远端仓库风格。
- **凭据存储**：首次读取旧版明文配置时，PAT、Cookie、云函数密码、HCM 预设令牌和 K8s SSH 密码会迁移到系统凭据存储；源配置文件仅保留凭据引用。若系统凭据存储不可用，迁移会明确失败，不会悄悄继续写明文。
- **PyQt6 桌面版已剥离**：2026-10 起移至 `pyqt-legacy` 分支存档，CI/CD 仅出 Electron + Tauri 双端产物。
