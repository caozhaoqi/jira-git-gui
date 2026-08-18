# Jira Git GUI

> 📗 English docs: [README.md](README.md)

面向 **Jira Git 集成插件**（Xiplink / BigBrassBand）**与 Kubernetes 日常运维**的统一桌面控制台。提供两种桌面形态，共用同一个 Python 后端：

- **PyQt6 桌面版**（`main.py`）：纯 Python + PyQt6，无需浏览器；所有网络请求在后台线程执行，界面不卡顿。
- **Electron / Web 版**（`electron/` + `web/`）：Electron 加载同一套 Web 前端，便于跨平台打包；也可以直接在浏览器里打开访问本地后端。

> 两种前端共用同一后端（`api/server.py`，默认端口 8787），功能等价。

## 功能总览

### Jira / Git 模块

- **双认证模式**：在 **PAT**（完整 `git clone`）与 **Cookie**（网页抓取 / 递归下载）之间自由切换。
- **高性能引擎**：增量扫描（约 2.7×）、集合式差异对比、并行合并（约 8×）、O(1) 文件树索引、全局令牌桶限流（速率可在界面调节）。
- **智能差异对比**：自动识别 CRLF / LF 行尾与纯空白差异（归类为"行尾差异"而非"已修改"）；JSON / JSONC / XML 家族文件自动格式化展开，单行压缩文件也能逐行可读对比。
- **可断点续传下载**：Cookie 模式支持整仓递归下载（含嵌套文件与二进制），支持续传、取消与并发控制（默认 4 线程）。

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
| 📜 **日志查看** | 主页面预览 + 独立全屏页（`log_viewer.html`）：搜索高亮、级别着色、容器与 Pod 切换、tail 行数、实时自动刷新、下载 |

- **多环境**：dev / test / prod 各自独立 kubeconfig，环境彩色标签（dev=蓝 / test=橙 / prod=红）。
- **健壮性**：kubectl 二进制自动定位（Homebrew / Docker / 系统 PATH 兜底），即使从 GUI 以最小 PATH 启动也能正常工作。

## 界面布局

- 侧边栏标签：仓库 / 文件树 / 文件预览 / 提交记录 / 差异对比 / 日志 / **K8s 快照**
- 浅色 / 深色双主题（一键切换，`localStorage` 记忆）
- 全局操作条按标签页感知——K8s 页自动隐藏仓库相关操作

## 项目结构

```
jira-git-gui/
├── main.py                 # 入口：创建 QApplication + MainWindow（PyQt6 桌面版）
├── run_merge.py            # CLI：把远端仓库最新代码合并到本地（缓存优先 + 同步历史）
├── k8s_preview.html        # K8s YAML 清洗的自包含演示页（无需集群）
├── requirements.txt        # PyQt6 / httpx / fastapi / uvicorn
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
│   ├── logger.py           # 轮转文件日志 + LogBridge（UI 桥）+ 全局 excepthook（PyQt6 惰性加载）
│   ├── safe.py             # safe_slot 装饰器：捕获槽异常，防止 UI 崩溃
│   ├── errors.py           # 统一异常类型
│   ├── k8s_manager.py      # K8s 核心：环境/kubeconfig 解析、YAML get-apply 清洗、
│   │                       #   事件 / 描述 / Top、exec 与文件操作、kubectl 自动定位
│   └── k8s_snapshot.py     # 快照引擎：Pod 状态 + 日志抓取、分级、HTML/JSON 报告
├── gui/                    # UI 层（PyQt6 控件）
│   ├── main_window.py      # 布局 + 信号绑定 + 异步任务编排
│   ├── connect_dialog.py   # 连接设置（url / 账号 / 模式 / PAT / Cookie / 仓库）
│   ├── repo_panel.py       # 发现仓库 / 手动指定仓库
│   ├── tree_panel.py       # 惰性文件树（O(1) 索引）
│   ├── preview_panel.py    # 代码预览
│   ├── diff_panel.py       # 差异视图（零依赖语法高亮）
│   ├── highlighter.py      # 零依赖语法高亮器（QSyntaxHighlighter）
│   ├── styles.py           # 浅色 / 深色双主题 QSS
│   ├── commit_panel.py     # 提交记录
│   ├── log_panel.py        # 日志
│   └── k8s_panel.py        # K8s 标签页（快照 / YAML / 网络 / 事件 / Top / Shell / 文件）
├── workers/                # 异步任务层
│   └── tasks.py            # 通用 QThread Worker（自动 on_log 回调；错误带完整堆栈）
├── api/                    # Web / Electron / Tauri 共用的后端
│   └── server.py           # FastAPI：50+ REST 端点 + SSE 推送 + WebSocket Shell，端口 8787
├── electron/               # Electron 桌面应用
│   ├── main.js             # 主进程：Python 后端生命周期 + BrowserWindow + 日志桥
│   ├── preload.js          # 暴露 window.electronAPI（contextIsolation 隔离）
│   └── package.json        # name / version / start|dev|dist 脚本 + electron-builder 配置
├── web/                    # Web 前端（Electron / 浏览器共用，零框架依赖）
│   ├── index.html          # 页面结构（标签页 + K8s 面板 + 连接设置弹窗）
│   ├── app.js              # 前端逻辑（REST + SSE + WebSocket，纯 vanilla JS）
│   ├── styles.css          # 设计系统（CSS 变量，浅色 / 深色双主题）
│   ├── k8s.css             # K8s 专属布局与视觉
│   └── log_viewer.*        # 独立全屏日志页（搜索 / 高亮 / Pod 与容器切换）
├── tauri/                  # Tauri 壳（可选的第三种桌面形态）
├── build/                  # PyInstaller 配置（gui / backend）
├── tests/                  # 单元测试（先单测后集成，受版本控制）
├── store/                  # 运行时产物（git clone / 下载，gitignored）
├── logs/                   # 运行时日志（完整堆栈，gitignored）
└── docs/
    └── PACKAGING.md        # 打包与跨平台发布细节
```

依赖方向：`gui → workers → core`；`core` 不反向依赖 GUI，可独立复用与测试。

## 运行方式

### PyQt6 桌面版

```bash
# 1. 创建并激活虚拟环境（已存在可跳过）
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动（任选其一）
./venv/bin/python main.py     # 直接用项目 venv
python3 main.py              # 任意 python 均可：main.py 会自动切换进 venv
./run.sh                     # 一键启动（macOS / Linux）
```

> **自愈式启动**：`main.py` 内置 venv 自检——若当前解释器缺少 PyQt6，会自动 re-exec 进项目自己的 `venv` 解释器再启动。

### Web / Electron 版（共用后端）

```bash
# 启动后端（浏览器打开 http://127.0.0.1:8787）
PYTHONPATH=. ./venv/bin/python -m api.server                # 默认端口 8787
PYTHONPATH=. ./venv/bin/python -m api.server --port 9000    # 自定义端口
```

```bash
cd electron
npm install        # 仅首次需要
npm start          # 启动（自动拉起 Python 后端并打开窗口）
npm run dev        # 开发模式（自动打开 DevTools）
npm run dist:mac   # 打包 macOS 安装包（dmg）
npm run dist:win   # 打包 Windows 安装包（nsis）
npm run dist:linux # 打包 Linux 安装包（AppImage + deb）
```

> 若 `npm` / Electron 下载被网络阻断，直接启动后端后在任意浏览器打开 `http://127.0.0.1:8787/` 即可——与 Electron 加载的是同一页面。

## K8s 运维模块

### 环境管理

环境（dev / test / prod …）保存在 `~/.config/jira-git-gui/k8s_envs.json`，各自独立 kubeconfig 路径、可选 context 与默认命名空间。K8s 页的环境选择器带彩色标签，切换环境时 **YAML** / **事件** / **Top** 面板自动刷新列表。

### 日志查看（`web/log_viewer.html`）

从快照日志面板点「⧉ 新页面打开」，或直接访问：

```
http://127.0.0.1:8787/web/log_viewer.html?pod=<pod>&env=<env>
```

- **Pod 自由切换**：顶栏下拉选择任意 Pod，日志、容器、命名空间自动切换。
- **容器切换**：多容器 Pod 逐容器查看；支持 `--previous` 查看重启前容器日志。
- **搜索**：关键字（支持正则）、忽略大小写、`N/M` 匹配计数、▲▼ 在匹配间跳转。
- **级别高亮**：ERROR/FATAL 红、WARN 黄、DEBUG 弱化。
- **tail 行数**：50 / 200 / 500 / 1000 / 全量（5000）。
- **实时跟踪**：每 3 / 5 / 10 秒自动刷新，自动跟随底部。
- **行号、换行切换、字号 ±、下载 .txt、主题切换**。

### Shell 与文件（Xshell / Xftp 风格）

- **Shell**：WebSocket 终端进入 Pod 容器（`/ws/k8s/exec`）。选 环境 → Pod → 容器 → 连接；命令执行、工作目录跨命令持久化（`cd` 后保持）、↑/↓ 命令历史。
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
- **相等性判断与合并都用原始字节**：压缩单行与美化多行（内容相同）仍按原始 MD5 / 大小判为"已修改"；合并始终写入远端原始字节，绝不"顺手格式化"污染远端。
- 解析失败一律原样返回，不抛异常。支持：JSON / JSONC / JSON5 / GeoJSON / tfstate / ipynb + XML / XHTML / SVG / WSDL / plist / RSS / Atom / XSL。

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
| `cookie` | 会话 Cookie | `JSESSIONID=...; atlassian.xsrf.token=...` |

> 真实环境变量（大写键，如 `JIRA_URL`）优先于 `.env`，便于 CI / 临时覆盖。打包后 `.env` 会在用户数据目录 `~/.jira-git-gui` 与可执行文件目录两处查找。

## 打包与发布（跨平台）

三种发布形态，共用同一 Python 后端：

| 形态 | 入口 | 打包方式 | 产物 |
| --- | --- | --- | --- |
| PyQt6 桌面版 | `main.py` | `pyinstaller build/pyinstaller_gui.spec` | `.app`（macOS）/ `.exe`（Windows） |
| Web 版 | 浏览器 | `pyinstaller build/pyinstaller_backend.spec` | 单文件后端 `jira-git-backend` |
| Electron 桌面版 | `electron/` | electron-builder（内嵌冻结后端） | `.dmg` / `.exe`(nsis) / `.AppImage`+`.deb` |

**关键约束**：PyInstaller 与 electron-builder 均**不支持交叉编译**——各平台产物必须在对应系统上构建。已配置 `.github/workflows/release.yml`：推送 `vX.Y.Z` 标签时在 macOS / Windows / Ubuntu runner 上自动构建。

本地构建步骤、CI 流程、产物清单与签名说明详见 **[docs/PACKAGING.md](docs/PACKAGING.md)**。

## 测试

```bash
# 先激活装有 PyQt6 的 venv
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

覆盖：续传下载、客户端优化（二进制下载 / 分支缓存 / 并行下载）、差异性能与格式化、CRLF / 行尾过滤、令牌桶限流、文件树索引、提交记录、Worker 异常保护、K8s YAML 清洗、`kubectl top` 解析、exec `cwd` 追踪、`ls -la` 解析、文件写入（文本与二进制 base64 路径）。集成测试需要真实凭据 / 集群，缺失时自动跳过。

## 已知限制

- **未签名**：本地 / CI 产物为 ad-hoc 签名，首次打开会被 Gatekeeper / SmartScreen 拦截。正式发布需配置证书。
- **根目录 `server.py` 已废弃**：硬编码绝对路径，仅保留历史兼容；新功能与打包均使用 `api/server.py`。
- **Python 版本**：开发环境 3.9 已做兼容加固；CI 与正式打包推荐 **Python ≥ 3.10**（3.11 已验证）。
- **Linux 运行依赖**：桌面版需要系统库 `libgl1` / `libnss3` 等（CI 中已安装）。
- **K8s Shell 非 TTY**：命令经 `sh -c` 管道执行（不支持交互式编辑器 / `top` 全屏）；交互式终端列入 P2 路线。
