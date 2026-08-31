# Jira Git GUI

> 📗 English docs: [README.md](README.md)

面向 **Jira Git 集成插件**（Xiplink / BigBrassBand）**与 Kubernetes 日常运维**的统一桌面控制台。提供 **两种桌面形态**，共用同一个 Python 后端与同一套 Web 前端（由 `frontend/web-react/` 构建）：

- **Electron 版**（`electron/` + `web/`）：基于 Electron + Chromium/WebKit webview 的跨平台桌面应用。
- **Tauri 版**（`tauri/` + `web/`）：使用操作系统原生 WebView 的轻量桌面应用——包体小得多（几十 MB 对比几百 MB）。

两种形态都加载同一套 Web 前端（由 `frontend/web-react/` 构建）、对接同一个共享 Python 后端（`api/server.py`，默认端口 8787），功能等价。Python 后端（FastAPI）会被打包进每个桌面应用，最终用户无需单独起服务或开浏览器。

## 功能总览

### Jira / Git 模块

- **双认证模式**：在 **PAT**（完整 `git clone`）与 **Cookie**（网页抓取 / 递归下载）之间自由切换。
- **高性能引擎**：增量扫描（约 2.7×）、集合式差异对比、并行合并（约 8×）、O(1) 文件树索引、全局令牌桶限流（速率可在界面调节）。
- **智能差异对比**：自动识别 CRLF / LF 行尾与纯空白差异（归类为"行尾差异"而非"已修改"）；JSON / JSONC / XML 家族文件自动格式化展开，单行压缩文件也能逐行可读对比。
- **可断点续传下载**：Cookie 模式支持整仓递归下载（含嵌套文件与二进制），支持续传、取消与并发控制（默认 4 线程）。
- **git 风格仓库对比合并**：选择远端仓库（下拉同时显示「仓库名 · ID」，同名仓库可区分）与对比目录，按大小快扫（不下载内容）、查看 git log 风格「最近更新」、每条差异带「已合并 ✓」徽标，单文件 / 批量合并带 SSE 进度；大文件 / 二进制走插件原始文件 REST 端点合并，不再因 viewer 大小限制整批失败。

### K8s 运维模块（☸ K8s 标签页）

| 子标签 | 功能 |
| --- | --- |
| 📸 **快照** | 批量抓取 Pod 状态 + 日志 → 分级（HIGH / MED / OK）→ 交互式 HTML 报告 + JSON |
| 📝 **Pod YAML** | 任意资源（pod / deployment / service / configmap / ingress / statefulset）get / apply，apply 前自动清洗服务端噪声字段（`status`、`managedFields`、`last-applied` 等） |
| 🌐 **网络检测** | 一键链路检测：kubectl → kubeconfig → 集群连通 → 内网 TCP 探测 → 外网出口 |
| 📡 **事件流** | 集群事件流（Warning 置红），按命名空间 / 对象 / 类型过滤，支持 `--all-namespaces`，自动刷新 |
| 📊 **资源 Top** | `kubectl top` — CPU / 内存占用条形图，Pod / Node 范围切换，自动刷新 |
| 💻 **Shell** | Pod 容器内 Xshell 式交互终端（WebSocket），工作目录跨命令持久化，命令历史 |
| 📁 **文件** | Pod 容器内 Xftp 式文件浏览器：列表 / 打开编辑 / 保存回写 / 上传 / 下载 / 新建目录 / 删除 |
| 🔍 **描述** | `kubectl describe` 弹窗 + 相关事件，可从快照、YAML 页、手动输入三处触发 |
| 📜 **日志查看** | 主页面预览 + 独立全屏页（`?view=log`）：搜索高亮、级别着色、容器与 Pod 切换、tail 行数、实时自动刷新、下载 |

- **多环境**：dev / test / prod 各自独立 kubeconfig，环境彩色标签（dev=蓝 / test=橙 / prod=红）。
- **健壮性**：kubectl 二进制自动定位（Homebrew / Docker / 系统 PATH 兜底），即使从 GUI 以最小 PATH 启动也能正常工作。

## 界面布局

- 侧边栏标签：仓库 / 文件树 / 文件预览 / 提交记录 / 差异对比 / 日志 / **K8s 快照**
- 浅色 / 深色双主题（一键切换，`localStorage` 记忆）
- 全局操作条按标签页感知——K8s 页自动隐藏仓库相关操作

## 项目结构

```
jira-git-gui/
├── run_merge.py            # CLI：把远端仓库最新代码合并到本地（缓存优先 + 同步历史）
├── scripts/                # 启动/构建脚本（跨平台：*.sh + *.ps1）
├── config/                 # 本地配置 JSON（cf_accounts.*、hcm_whitelist.*）—— 见 .gitignore
│                           # 安全说明：含真实凭证/IP/域名的连接信息只存在于 `*.local.json`
│                           # （如 config/hcm_whitelist.local.json、config/cf_accounts.local.json），
│                           # 已被 .gitignore 忽略、永不入库；仓库内仅保留占位模板。
├── tools/k8s_preview.html  # K8s YAML 清洗的自包含演示页（无需集群）
├── requirements.txt        # fastapi / uvicorn / httpx / pyinstaller
├── core/                   # 核心逻辑层（无 GUI 依赖，可独立测试）
│   ├── app_paths.py        # 运行时可写目录（打包后重定位到 ~/.jira-git-gui）
│   ├── constants.py        # 目录 / 代理 / 超时
│   ├── models.py           # ConnectConfig / RepoInfo / TreeEntry / DiffResult
│   ├── config.py           # 自动从 .env 加载默认连接配置
│   ├── client.py           # JiraGitClient：连接 / 发现 / 列目录 / 取文件 / clone / 下载
│   ├── cache.py            # 远端树 / 内容 JSON 缓存（带锁，避免重复拉取）
│   ├── differ.py           # 差异引擎：compute_diff / scan_local / merge_to_local / file_diff / canonical_text
│   ├── throttle.py         # 全局令牌桶限流器（DEFAULT_REQUEST_QPS）
│   ├── sync_history.py     # 同步历史（git-log 风格）
│   ├── logger.py           # 轮转文件日志 + LogBridge（UI 桥）+ 全局 excepthook
│   ├── safe.py             # safe_slot 装饰器：捕获槽异常，防止 UI 崩溃
│   ├── errors.py           # 统一异常类型
│   ├── k8s_manager.py      # K8s 核心：环境/kubeconfig 解析、YAML get-apply 清洗、
│   │                       #   事件 / 描述 / Top、exec 与文件操作、kubectl 自动定位
│   └── k8s_snapshot.py     # 快照引擎：Pod 状态 + 日志抓取、分级、HTML/JSON 报告
├── api/                    # Web / Electron / Tauri 共用的后端
│   └── server.py           # FastAPI：50+ REST 端点 + SSE 推送 + WebSocket Shell，端口 8787
├── web/                    # Web 前端生产构建产物（由 frontend/web-react/ 的 vite build 生成：
│                          #   index.html + assets/），Electron / Tauri / 浏览器共用。
│                          #   原生 vanilla-JS 版本已归档到 frontend/web-legacy/ 用于回退。
├── frontend/               # Web 前端源码（按功能归组）
│   ├── web-react/          # React + TypeScript + Vite 前端——web/ 的实际源码来源
│   │   ├── src/components/ # 功能面板：RepoPanel / CommitsPanel / DiffPanel / k8s/* / CfPanel
│   ├── src/api/           # 统一 API 客户端、SSE 事件管理器、类型化模型
│   ├── src/store/         # Zustand 全局状态（logs / toasts / progress / activeTab）
│   └── src/utils/         # 格式化（diff / 相对时间 / 体积）+ 剪贴板（Electron/Tauri/Web）
├── electron/               # Electron 桌面应用（已发布）
│   ├── main.js             # 主进程：Python 后端生命周期 + BrowserWindow + 日志桥
│   ├── preload.js          # 暴露 window.electronAPI（contextIsolation 隔离）
│   └── package.json        # name / version / start|dev|dist 脚本 + electron-builder 配置
├── tauri/                  # Tauri 桌面应用（已发布）
│   └── src-tauri/          # Rust 壳：Python 后端生命周期 + WebView 窗口
├── build/                  # 冻结后端的 PyInstaller 配置（Electron / Tauri 共用）
├── tests/                  # 单元测试（先单测后集成，受版本控制）
├── store/                  # 运行时产物（git clone / 下载，gitignored）
├── logs/                   # 运行时日志（完整堆栈，gitignored）
└── docs/
    └── PACKAGING.md        # 打包与跨平台发布细节
```

> **遗留说明**：`main.py`、`gui/`、`workers/` 是较早上一代的 PyQt6 桌面实现，保留用于参考 / 本地开发，但**不属于**已发布的发行版（发行版为 Electron + Tauri）。根目录废弃的 `server.py` 同样仅作历史兼容保留，所有发行构建均使用 `api/server.py`。

依赖方向：`gui → workers → core`；`core` 不反向依赖 GUI，可独立复用与测试。

## 运行方式

### Web / Electron 版（共用后端）

```bash
# 1. 创建并激活虚拟环境（已存在可跳过）
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动后端（浏览器打开 http://127.0.0.1:8787）
PYTHONPATH=. ./venv/bin/python -m api.server                # 默认端口 8787
PYTHONPATH=. ./venv/bin/python -m api.server --port 9000    # 自定义端口

# 或一键启动
./scripts/run.sh                   # 起后端并打开浏览器（macOS / Linux / Windows-Git-Bash）
./scripts/run_web.sh               # 同上
./scripts/run_web.sh --electron    # 改为启动 Electron 桌面应用
```

若 `npm` / Electron 下载被网络阻断，直接启动后端后在任意浏览器打开 `http://127.0.0.1:8787/` 即可——与 Electron / Tauri 加载的是同一页面。

### Electron 桌面版

```bash
cd electron
npm install        # 仅首次需要
npm start          # 启动（自动拉起 Python 后端并打开窗口）
npm run dev        # 开发模式（自动打开 DevTools）
npm run dist:mac   # 打包 macOS 安装包（dmg）
npm run dist:win   # 打包 Windows 安装包（nsis）
npm run dist:linux # 打包 Linux 安装包（AppImage + deb）
```

### Tauri 桌面版

```bash
# 需要 Rust（https://rustup.rs）与系统 WebView 开发库
./scripts/build-tauri.sh            # release 构建 → .app/.dmg（macOS）、.msi（Windows）、.AppImage+.deb（Linux）
cargo tauri dev                     # 实时开发（热重载），在 tauri/ 下执行

# Windows (PowerShell)
.\scripts\build-tauri.ps1
```

> Tauri 构建会内嵌同一份冻结后的 Python 后端，运行时无需单独起服务。

## K8s 运维模块

### 环境管理

环境（dev / test / prod …）保存在 `~/.config/jira-git-gui/k8s_envs.json`，各自独立 kubeconfig 路径、可选 context 与默认命名空间。K8s 页的环境选择器带彩色标签，切换环境时 **YAML** / **事件** / **Top** 面板自动刷新列表。

#### kubeconfig 集中管理（推荐实践）

- **受控目录**：kubeconfig 默认散落在 `~/Downloads` 等任意位置，权限不受控。建议通过「环境管理 → 导入 kubeconfig」把内容导入受控目录
  `~/.config/jira-git-gui/kubeconfigs/<env>.kubeconfig`（自动 `chmod 600`，仅当前用户可读写），环境自动指向新路径。
- **导入 / 导出**：`POST /api/k8s/env/import-kubeconfig`（内容校验 + 权限 600）；`GET /api/k8s/env/export` 导出全部环境配置
  （含 kubeconfig 内容），便于团队共享 / 备份 / 迁移。**注意**：导出内容含集群凭据，请仅通过加密通道共享。
- **团队 / 多人共用建议**：
  1. **最小权限**：为每个开发者签发独立的 service account + RBAC（仅授予其所需命名空间的读 / 写权限），
     不要共享管理员 kubeconfig；kubeconfig 里不要内嵌 admin 私钥。
  2. **密钥轮换**：定期（如 90 天）轮换 token / client-cert；轮换后重新「导入 kubeconfig」即可，
     无需改代码。所有环境密钥集中在 `kubeconfigs/` 目录，轮换/审计一目了然。
  3. **集中托管**：生产集群推荐把 kubeconfig 放到团队的密钥管理系统（Vault / AWS Secrets Manager / 云 KMS），
     用受管拉取脚本下发到开发者本机的 `kubeconfigs/` 目录（权限 600），避免源码库或 IM 里明文传私钥。
  4. 需要临时给他人时，用 `GET /api/k8s/env/export` 导出，但**先确认里面没有长有效期管理员证书**。

### 日志查看（`/web/?view=log`）

从快照页点「⧉ 新页面打开完整日志」或 K8s Shell 进入，或直接访问（端口以实际运行端口为准，默认 8787）：

```
/web/?view=log&pod=<pod>&env=<env>&container=<container>&namespace=<namespace>
```

- **Pod 自由切换**：顶栏下拉选择任意 Pod，日志、容器、命名空间自动切换。
- **容器切换**：多容器 Pod 逐容器查看；支持 `--previous` 查看重启前容器日志。
- **搜索**：关键字（支持正则）、忽略大小写、`N/M` 匹配计数、▲▼ 在匹配间跳转。
- **级别高亮**：ERROR/FATAL 红、WARN 黄、DEBUG 弱化。
- **tail 行数**：50 / 200 / 500 / 1000 / 全量（5000）。
- **实时跟踪**：每 3 / 5 / 10 秒自动刷新，自动跟随底部。
- **行号、换行切换、字号 ±、下载 .txt、主题切换**。

### Shell 与文件（Xshell / Xftp 风格）

- **Shell（TTY 交互终端）**：WebSocket 终端进入 Pod 容器（`/ws/k8s/exec?tty=1`）。后端用本地 pty（`os.openpty`）
  为 `kubectl exec -it` 提供 TTY，前端 xterm.js 全双工 + 自动 resize（`TIOCSWINSZ`）：
  - **支持 vim / top / htop / less 等全屏交互程序**（真实 TTY 检测通过）；
  - 持久 shell 会话，`cd` 后工作目录保持，直接在终端输入命令即可；
  - 前端连接后自动按容器尺寸发送 resize，窗口缩放实时同步。
- **文件**：面包屑式浏览容器文件系统；双击文本文件内联编辑并保存回写；支持上传（base64）、下载、新建目录、确认后删除。

### 快照报告

`kubectl get pods` + 逐 Pod 抓日志 → 分级（HIGH / MED / OK）→ 自包含 HTML 报告 + `pods.json` / `summary.json`（位于 `~/k8s_snapshots/<时间戳>/`）。状态异常的 Pod 日志落盘保存；应用内日志面板在快照日志缺失时回退到**实时向集群抓取**。

## 智能差异对比

差异引擎（`core/differ.py`）专门解决两个痛点："内容相同格式不同"与"单行压缩文件"。

### 行尾 / 空白过滤

- 文本文件额外计算**归一化哈希**（先把 `\r\n` 归一为 `\n`，再 MD5）。
- 当本地（如 CRLF）与远端（如 LF）仅行尾 / 空白不同、语义一致时，状态记为 **`行尾差异`**——不计为"已修改"，合并时跳过，避免污染远端风格。
- Web「差异对比」页默认勾选"忽略行尾差异"；取消勾选可恢复"已修改"精细审查。

### 结构化文件格式化

- JSON / JSONC / XML 家族在生成统一 diff 前先归一化展开（JSON `indent=2`、XML `minidom.toprettyxml`）。
- 单行压缩文件因此变成逐行可读的 diff——只有实际变更的字段行被高亮，而非整行标红。
- **相等性判断与合并都用原始字节**：合并始终写入远端原始字节，绝不"顺手格式化"污染远端。
- 解析失败一律原样返回，不抛异常。支持：JSON / JSONC / JSON5 / GeoJSON / tfstate / ipynb + XML / XHTML / SVG / WSDL / plist / RSS / Atom / XSL。

## 差异对比 / 合并工作流（git 风格）

**差异对比**标签页（`frontend/web-react/src/components/DiffPanel.tsx`）支持「像 git 一样管理一个仓库」：选远端仓库与目录，对比本地↔远端，再把远端拉回本地工作副本。

### 仓库对比选择器（显示仓库 ID）

对比仓库下拉来自 `GET /api/repos`，每个选项渲染「仓库名 · ID `<repo_id>`」，**同名仓库可区分**（本环境存在同名仓库）。选中值仍为 `repo_id`，选中逻辑不变。选中后本地目录按 `.env` `MERGE_REPO_*` 映射自动填充（见[配置文件](#配置文件-env)与[已知限制](#已知限制)）。

### 对比目录 + 快扫

- **对比目录**：手动输入路径，或打开文件树弹层（`GET /api/tree`，限 `type==='dir'`）只对比某一子目录而非整仓。
- **快速扫描**（默认开）：`scan_remote(fast_hash=True)` 只记录文件 `size`、**不下载内容**。`compute_diff` 在两端 hash 皆空时退化为按 size 比较，零内容拉取完成差异判定——超大仓库显著更快。

### 最近更新 + 已合并徽标

- **最近更新**（`GET /api/diff/commits?path=compare_dir`）：git log 风格列出该目录近期提交。
- **已合并 ✓ 徽标**：每条差异在本地文件 md5 == 记录 `remote_hash` 时显示 ✓（`GET /api/diff/merge-manifest`），面板顶部显示已合并计数。

### 合并（单文件 / 批量）带 SSE 进度

- `POST /api/diff/merge`（单文件）与 `POST /api/diff/merge-batch`（批量并行）把远端字节写回本地目录；进度经 SSE 推送（`scan_stage` / `scan_progress` / `scan_done` / `merge_start` / `merge_progress` / `merge_done`）。
- **大文件 / 二进制**：`get_file(path, allow_binary=True)` 返回原始字节；当 web viewer 无法内嵌大文件时，`core/client/files.py::_fetch_raw_file` 回退到插件原始文件 REST 端点（`/rest/git/1.0/repositories/{repoId}/files/{ref}?path=` 与 `/rest/gitplugin/1.0/repository/{repoId}/files/{ref}?path=`）绕过 viewer 大小限制。**严格守门**：只接受原始字节或 JSON `content`/`rawFile` 字段（base64 或文本），HTML / 错误包一律拒绝，绝不把错误页写坏本地。预览接口 `api_diff_file` 保持 `allow_binary=False`。

### 续传 manifest（重跑跳过已合并）

- 合并后 manifest 写入**应用数据目录 sidecar** `get_data_root()/merge_state/<safe_local_dir>/manifest.json`（刻意不进 `local_dir`，不会出现在 `git status`）。
- 下次合并时，本地文件 md5 仍等于记录 `remote_hash` 的条目被跳过（不重抓、不重写）；本地被改（md5 变化）的文件则重抓并覆盖——合并正确重新同步。
- `is_already_merged(local_dir, rel_path, manifest)` 是两处合并路径跳过判定的唯一依据。

## 性能

在数万文件规模仓库上实测：

- **增量扫描**：只重扫变更子树，整体约 **2.7×** 提速。
- **集合式差异**：用集合运算替代逐项线性比较，大仓库更快。
- **并行合并**：本地写入阶段多文件并行，约 **8×** 提速。
- **O(1) 文件树索引**：`tree_panel` 用 dict 索引节点，定位 / 展开不再遍历整树。
- **令牌桶限流**：`core/throttle.py` 中 `DEFAULT_REQUEST_QPS=6`，防止触发远端限流；Web 合并速率旋钮可调 15–30 QPS，过载自动退避。
- **缓存优先**：远端树 / 内容优先读 `core/cache.py` 带锁 JSON 缓存，避免重复拉取；`run_merge.py` 同样缓存优先。

## 配置文件（`.env`）

应用启动时**自动读取项目根目录 `.env`** 作为默认连接配置（无需每次重填"连接设置"）。该文件已被 gitignore——**请勿提交真实凭据**。支持的键（容错别名与拼写）：

| `.env` 键 | 含义 | 备注 |
| --- | --- | --- |
| `jira_url` | Jira 基础地址 | 也接受 `JIRA_URL` |
| `username` | 账号名 | PAT clone 时用 PAT 持有者的账号 |
| `mode` | 模式 | `pat`（默认）或 `cookie` |
| `personal_access_token` | PAT | 也容忍旧拼写 `persoanl_access_token` |
| `cookie` | 会话 Cookie | 格式：`JSESSIONID=...; atlassian.xsrf.token=...` |

> 真实环境变量（大写键，如 `JIRA_URL`）优先于 `.env`，便于 CI / 临时覆盖。打包后 `.env` 会在用户数据目录 `~/.jira-git-gui` 与可执行文件目录两处查找。

## 打包与发布（跨平台）

仅发布**两种桌面形态**，两者均内嵌同一份冻结后的 Python 后端：

| 形态 | 入口 | 打包方式 | 产物 |
| --- | --- | --- | --- |
| Electron 桌面版 | `electron/` | electron-builder（内嵌冻结后端） | `.dmg`（macOS）/ `.exe`(nsis)（Windows）/ `.AppImage`+`.deb`（Linux） |
| Tauri 桌面版 | `tauri/` | `cargo tauri build`（内嵌冻结后端） | `.app`+`.dmg`（macOS）/ `.msi`（Windows）/ `.AppImage`+`.deb`（Linux） |

**交叉编译说明（2026-08 已在 Apple Silicon 上验证）**：Electron 可在 macOS 上直接交叉构建 Windows `.exe`（NSIS）——electron-builder 自带 NSIS 工具链，无需 Wine；Tauri 也可通过实验性 GNU target（`x86_64-pc-windows-gnu` + mingw-w64）在 macOS 交叉构建 Windows NSIS 包。仍需 Windows 机器 / CI runner 的：`.msi`（WiX，两种形态都是）与 MSVC target 的 Tauri 构建（官方推荐路线）。完整交叉构建配方（镜像、架构参数、cargo 链接器配置）见 **[docs/PACKAGING.md](docs/PACKAGING.md)**。已配置 `.github/workflows/release.yml`：推送 `vX.Y.Z` 标签时在 macOS / Windows / Ubuntu runner 上自动构建三端产物。

本地构建步骤、CI 流程、产物清单与签名说明详见 **[docs/PACKAGING.md](docs/PACKAGING.md)**。

## 测试

```bash
# 先激活装有 PyQt6 的 venv（部分用例需要）
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

覆盖：续传下载、客户端优化（二进制下载 / 分支缓存 / 并行下载）、差异性能与格式化、CRLF / 行尾过滤、令牌桶限流、文件树索引、提交记录、Worker 异常保护、K8s YAML 清洗、`kubectl top` 解析、exec `cwd` 追踪、`ls -la` 解析、文件写入（文本与二进制 base64 路径）。集成测试需要真实凭据 / 集群，缺失时自动跳过。

## 已知限制

- **未签名**：本地 / CI 产物为 ad-hoc 签名，首次打开会被 Gatekeeper / SmartScreen 拦截。正式发布需配置证书。
- **根目录 `server.py` 已废弃**：硬编码绝对路径，仅保留历史兼容；新功能与打包均使用 `api/server.py`。
- **PyQt6 桌面版（`main.py` / `gui/`）为遗留实现**：不再是发布目标，发行版为 Electron + Tauri。
- **Python 版本**：开发环境 3.9 已做兼容加固；CI 与正式打包推荐 **Python ≥ 3.10**（3.11 已验证）。
- **Linux 运行依赖**：桌面版需要系统库 `libnss3`（Electron）/ WebView 开发库（Tauri）等（CI 中已安装）。
- **K8s Shell 的 TTY 会话为单连接**：一个 Shell 标签页对应一条 `kubectl exec -it` 会话，断开即结束进程；
  多开请用「日志查看」的独立窗口模式。
- **`.env` `MERGE_REPO_*` 映射按仓库「名」建立**：`/api/diff/repo-mappings` 与选中自动填本地目录都按 `display_name||name` 查表。同名仓库会撞 key，只能匹配到其中一个。若要让同名仓库各自自动定位本地目录，需把映射改为按 `repo_id` 索引（后端 `load_merge_config` + `/api/diff/repo-mappings` 都要带 `repo_id`）。对比仓库下拉已显示 ID 用于区分，但自动填充仍需从名改 ID。
