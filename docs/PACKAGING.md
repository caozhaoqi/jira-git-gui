# 打包与发布指南（jira-git-gui）

本工具提供 **三种发布形态**，共用同一个 Python 后端（`api/server.py`）：

| 形态 | 入口 | 适用场景 | 打包方式 |
|------|------|---------|---------|
| **PyQt6 桌面版** | `main.py` → `gui/main_window.py` | 原生桌面体验，独立运行（不依赖后端服务） | PyInstaller（`build/pyinstaller_gui.spec`） |
| **Web 版** | 浏览器访问 `127.0.0.1:8787` | 最轻量、最易跨平台 | PyInstaller 冻结后端（`build/pyinstaller_backend.spec`） |
| **Electron 桌面版** | `electron/main.js` | 原生壳 + 浏览器引擎，体验接近独立 App | electron-builder（打包冻结后的后端） |

> 跨平台结论：**可以跨平台，但必须按目标系统分别构建**。PyInstaller 与 electron-builder
> 均**不支持交叉编译**——macOS 出 `.app`/`.dmg`、Windows 出 `.exe`/`.msi`、Linux 出
> `AppImage`/`deb` 需在各自系统（或对应 CI runner）上构建。推荐用 GitHub Actions
> 三条 runner 自动出包（见 `.github/workflows/release.yml`）。

---

## 一、前置准备（三种形态通用）

```bash
# 1) Python 依赖（fastapi/uvicorn 仅 Web/Electron 形态需要，本地开发也可能缺失）
python -m pip install -r requirements.txt
pip install pyinstaller                       # GUI / 后端冻结

# 2) Node 依赖（仅 Electron 形态）
cd electron && npm install && cd ..
```

- **Python 版本**：代码使用 PEP 604 联合类型（`X | None`），需 **Python ≥ 3.10**；
  `gui/highlighter.py`、`gui/repo_panel.py` 已加 `from __future__ import annotations` 以兼容 3.9。
  CI 使用 `3.11`，本地建议 ≥ 3.10。
- **运行时数据目录**：冻结后程序位于只读包内，日志 / 缓存 / 下载 / 会话统一写到
  `~/.jira-git-gui/`（开发态写到项目根）。由 `core/app_paths.get_data_root()` 控制，
  可通过环境变量 `JIRA_GIT_DATA_DIR` 覆盖（Electron 壳据此与后端对齐目录）。

---

## 二、PyQt6 桌面版

```bash
pyinstaller build/pyinstaller_gui.spec --noconfirm
```

产出：
- macOS：`dist/JiraGitGUI.app`
- Windows：`dist/JiraGitGUI/JiraGitGUI.exe`
- Linux ：`dist/JiraGitGUI/`（可再用 `linuxdeploy` 包成 AppImage）

验证（macOS，无显示器时用 offscreen）：
```bash
QT_QPA_PLATFORM=offscreen dist/JiraGitGUI.app/Contents/MacOS/JiraGitGUI &
# 日志落在 ~/.jira-git-gui/logs/jira_git_gui.log
```

---

## 三、Web 版（冻结后端）

```bash
pyinstaller build/pyinstaller_backend.spec --noconfirm
# 产出：dist/jira-git-backend（macOS / Linux）或 dist/jira-git-backend.exe（Windows）
./dist/jira-git-backend --port 8787
# 浏览器打开 http://127.0.0.1:8787
```

`web/` 静态资源与 `.env` 已被收集进包内（`sys._MEIPASS`），后端自动从包内加载。

---

## 四、Electron 桌面版

Electron 不直接打包 Python，而是把**第三节冻结好的后端可执行**作为 `extraResource` 嵌入安装包。

```bash
# 1) 先冻结后端，放到 Electron 资源目录
pyinstaller build/pyinstaller_backend.spec --noconfirm
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

## 五、CI 一键发布

推送 `vX.Y.Z` 标签（或手动 `workflow_dispatch`）触发 `.github/workflows/release.yml`：
三条 runner（macos / windows / ubuntu）各自构建 **后端 + GUI + Electron** 并上传 artifact。
产物的跨平台对应：

| Runner | 后端 | GUI | Electron |
|--------|------|-----|---------|
| macos-latest | `jira-git-backend` | `JiraGitGUI.app` | `*.dmg` |
| windows-latest | `jira-git-backend.exe` | `JiraGitGUI/` | `*-setup.exe` (nsis) |
| ubuntu-latest | `jira-git-backend` | `JiraGitGUI/` | `*.AppImage` / `*.deb` |

---

## 六、已知限制 / 注意

- **代码签名**：CI 未配置证书，macOS/Windows 产物为「ad-hoc / 未签名」，首次打开会被
  Gatekeeper / SmartScreen 拦截，需用户手动允许。正式发布请配置 `codesign` / 证书。
- **Linux 系统库**：PyQt6 需 `libgl1` 等；Electron 需 `libnss3` 等。CI 已 `apt-get install`，
  终端用户若缺库会启动失败。
- **不动原始字节**：无论哪种形态，合并时的写入内容永远来自**原始远程字节**，格式化/规范化
  仅用于 diff 展示，不会污染远端仓库风格。
- **`.env` 含敏感信息**：虽被收集进包，但建议正式发布时改用「连接设置」UI 录入，避免硬编码密钥。
