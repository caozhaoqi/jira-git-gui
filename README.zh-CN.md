# Jira Git 通用拉取工具

> 📗 English documentation: [README.md](README.md)

针对 Jira Git Integration 插件（Xiplink / BigBrassBand）的通用桌面客户端。提供两种桌面形态：

- **PyQt6 桌面版**（`main.py`）：纯 Python + PyQt6，无需浏览器，所有网络请求在后台线程执行，界面不卡顿。
- **Electron / Web 桌面端**（`electron/` + `web/`）：以 Electron 加载同一套 Web 前端，跨平台打包；也可直接用浏览器访问本地后端，UI 特性见下文「Electron / Web 桌面端」。

> 两套前端共用同一 Python 后端（`api/server.py`，默认端口 8787），能力完全一致。

## 功能特性

- **双前端 / 双模式**：PyQt6 原生桌面与 Electron+Web 任选；PAT（git clone 整库）与 Cookie（Web 抓取 / 递归下载）两种认证随心切换。
- **统一设计系统**：Web 端采用 CSS 变量驱动的浅色 / 深色双主题，品牌头、实时状态点、GitHub 风格 diff 表格，现代且一致。
- **高性能引擎**：增量扫描（~2.7×）、集合化差异计算、并行合并（~8×）、O(1) 文件树索引；全局令牌桶限流，Web 端可调速。
- **智能差异对比**：自动识别 CRLF / LF 行尾与纯空白差异（判为「行尾差异」而非「已修改」）；JSON / JSONC / XML 等结构化文件在对比视图中自动格式化展开，单行压缩文件也能逐行可读。
- **断点续传与有界并发**：Cookie 模式支持递归整库下载（含嵌套文件与二进制），断点续传、可取消、默认 4 线程并发。

## 两种模式

| 模式 | 认证 | 能力 | 局限 |
| --- | --- | --- | --- |
| **PAT 模式** | Personal Access Token | `git clone` 全量拉取（含嵌套文件），本地浏览 / 预览 | 需要该账号名下有效 PAT 与仓库名 |
| **Cookie 模式** | `JSESSIONID` 会话 | 浏览文件树（懒加载）、预览文本文件、批量 / 递归下载整库（含嵌套文件与二进制）、断点续传、并行下载 | 二进制文件仅支持「下载」到本地，不支持直接预览；依赖会话 Cookie 有效 |

> Cookie 模式已支持**递归整库下载**（插件接口本身支持任意 path，含子目录与嵌套文件），
> 不再受「仅根目录」限制。断点续传 + 有界并发（默认 4 线程）让整库抓取可续、可取消、更快。

## 项目结构

```
jira-git-gui/
├── main.py                 # 入口：创建 QApplication + MainWindow（PyQt6 桌面版）
├── run_merge.py            # 命令行：合并远程仓库最新代码到本地（缓存优先 + 同步历史）
├── server.py               # ⚠️ 旧版单体后端（已废弃，请勿使用；主路径见 api/server.py）
├── requirements.txt        # PyQt6 / httpx / fastapi / uvicorn
├── core/                   # 核心逻辑层（无 GUI 依赖，可独立测试）
│   ├── app_paths.py        # 运行时可写目录（冻结包下落到 ~/.jira-git-gui）
│   ├── constants.py        # 目录 / 代理 / 超时
│   ├── models.py           # ConnectConfig / RepoInfo / TreeEntry / DiffResult
│   ├── config.py           # 从 .env 自动载入默认连接配置
│   ├── client.py           # JiraGitClient：connect / discover / list_level / get_file / clone / download
│   ├── cache.py            # 远程文件树 / 内容 JSON 缓存（带锁，避免重复拉取）
│   ├── differ.py           # 差异对比：compute_diff / scan_local / merge_to_local / file_diff / canonical_text
│   ├── throttle.py         # 全局令牌桶限流（DEFAULT_REQUEST_QPS）
│   ├── sync_history.py     # 同步历史（类 git log）
│   ├── logger.py           # 文件轮转日志 + LogBridge(UI 桥) + 全局异常钩子（PyQt6 懒加载）
│   ├── safe.py             # safe_slot 装饰器：拦截槽函数异常，防止界面闪退
│   └── errors.py           # 统一异常类型
├── gui/                    # 界面层（PyQt6 组件）
│   ├── main_window.py      # 布局 + 信号绑定 + 异步任务编排
│   ├── connect_dialog.py   # 连接设置（地址 / 账号 / 模式 / PAT / Cookie / 仓库）
│   ├── repo_panel.py       # 发现仓库 / 手动指定仓库
│   ├── tree_panel.py       # 懒加载文件树（O(1) 索引）
│   ├── preview_panel.py    # 代码预览
│   ├── diff_panel.py       # 差异对比视图（零依赖语法高亮）
│   ├── highlighter.py      # 零依赖语法高亮器（QSyntaxHighlighter）
│   ├── styles.py           # 浅色 / 深色双主题 QSS
│   ├── commit_panel.py     # 提交记录
│   └── log_panel.py        # 日志
├── workers/                # 异步任务层
│   └── tasks.py            # 通用 QThread Worker（自动注入 on_log 回调；异常输出完整 traceback）
├── api/                    # Web / Electron 共用后端
│   └── server.py           # FastAPI：REST + SSE，默认端口 8787（主路径）
├── electron/               # Electron 桌面端
│   ├── main.js             # 主进程：Python 后端生命周期 + BrowserWindow + 日志桥接
│   ├── preload.js          # 暴露 window.electronAPI（contextIsolation 隔离）
│   └── package.json        # name / version / start|dev|dist 脚本 + electron-builder 配置
├── web/                    # Web 前端（Electron / 浏览器通用，零框架依赖）
│   ├── index.html          # 页面结构（工具栏 + 标签页 + 连接设置弹窗）
│   ├── styles.css          # 设计系统（CSS 变量，浅色 / 深色双主题）
│   └── app.js              # 前端逻辑（REST + SSE，纯 vanilla JS）
├── build/                  # PyInstaller 打包配置（gui / backend 两套 spec）
├── tests/                  # 单元测试（先单测后集成，已纳入版本控制）
├── store/                  # 运行期产物（git 克隆 / 下载，已 gitignore）
├── logs/                   # 运行期日志（含完整 traceback，已 gitignore）
└── docs/
    └── PACKAGING.md        # 打包与跨平台发布详细说明
```

依赖方向：`gui → workers → core`，`core` 不反向依赖 GUI，便于单独复用与测试。

## 运行

### PyQt6 桌面版

```bash
# 1. 创建并激活虚拟环境（已存在 venv 可跳过）
python3 -m venv venv
source venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动（任选其一）
./venv/bin/python main.py     # 直接用项目 venv
python3 main.py              # 任意 python 亦可：main.py 会自动切到 venv
./run.sh                     # 一键启动脚本（macOS / Linux）
```

> **启动自愈**：`main.py` 顶部内置 venv 自检——若当前解释器缺少 `PyQt6`，
> 会自动 `re-exec` 到项目自带的 `venv` 解释器再启动。因此用系统 `python3`
> 直接跑也不会再出现 `ModuleNotFoundError: No module named 'PyQt6'`。
> 若 venv 本身缺失 PyQt6，请先执行上面的第 2 步安装依赖。

### Web / Electron 版（共享后端）

```bash
# 启动后端（浏览器访问 http://127.0.0.1:8787）
PYTHONPATH=. ./venv/bin/python -m api.server            # 默认端口 8787
PYTHONPATH=. ./venv/bin/python -m api.server --port 9000   # 自定义端口
```

## Electron / Web 桌面端

以 Electron 打包的独立桌面应用：主进程（`electron/main.js`）负责拉起 Python 后端
（`api/server.py`，端口 8787）并承载一个 `BrowserWindow`，窗口内加载 `web/` 下的前端页面。
后端就绪失败会弹窗提示并退出，避免白屏。打包时 Electron 会内置冻结后的后端可执行，
开发态回退到 `venv/bin/python` 与系统 `python`。

### 启动

```bash
cd electron
npm install        # 仅首次，安装 electron + electron-builder
npm start          # 启动（自动拉起 Python 后端并打开窗口，1280×800）
npm run dev        # 开发模式（自动打开 DevTools）
npm run dist:mac   # 打包 macOS 安装包（dmg）
npm run dist:win   # 打包 Windows 安装包（nsis）
npm run dist:linux # 打包 Linux 安装包（AppImage + deb）
```

> 若本机 `npm` / Electron 下载受阻，也可直接以任意浏览器访问 `http://127.0.0.1:8787/`
> （先在项目根目录启动后端：`PYTHONPATH=. ./venv/bin/python -m api.server`），
> 前端（`web/`）与 Electron 内加载的是同一套页面。

### 界面特性

- **浅色 / 深色双主题**：工具栏「🌓 主题」一键切换，偏好经 `localStorage` 持久化，下次启动自动恢复。
- **品牌头**：工具栏左侧显示应用标识（🌿）+ 名称「Jira Git GUI」，与裸网页区分。
- **实时状态点**：底部状态栏左侧指示点——绿 = 凭证已配置 / 黄 = 未配置，后端状态一目了然。
- **视觉打磨**：主按钮渐变、列表项 hover 微抬升、卡片柔和阴影、GitHub 风格 diff 表格，整体更现代统一。
- **设计系统**：`web/styles.css` 以 CSS 变量驱动配色、间距、圆角、阴影，浅色 / 深色通过 `body.dark` 覆盖，新增组件即可复用，避免散落硬编码。
- **标签页布局**：仓库 / 文件树 / 文件预览 / 提交记录 / 差异对比 / 日志，与 PyQt 版功能等价。

## 智能差异对比

差异对比（`core/differ.py`）针对「内容相同但格式不同」与「单行压缩文件」两类常见痛点做了专门处理：

### 行尾 / 空白差异过滤

- 对文本文件额外计算**归一化哈希**（先把 `\r\n` 统一成 `\n` 再取 MD5）。
- 当本地（如 CRLF）与远程（如 LF）仅行尾 / 空白不同、内容语义一致时，状态判为 **`行尾差异`（`WHITESPACE_ONLY`）**，不计入「已修改」，合并时自动跳过，避免污染远端风格。
- Web 端「差异对比」面板默认勾选 **「忽略行尾差异」**；取消勾选则恢复为「已修改」，便于精细核对。

### 结构化文件格式化展示

- 对 JSON / JSONC / XML 系列，在生成 unified diff **前**先 `canonical_text()` 规范化展开（JSON `indent=2`、XML `minidom.toprettyxml`）。
- 单行压缩（minified）文件由此变为行级可读 diff——只把真正变化的字段那一行高亮，而非整行标红。
- **相等判定与合并均按原始字节**：minified 单行 vs pretty 多行（同内容）仍按原始 MD5/size 判为「已修改」；合并写入的永远是原始远程字节，绝不顺手「格式化」污染远端。
- 解析失败一律原样返回，绝不抛异常。支持范围：JSON / JSONC / JSON5 / GeoJSON / tfstate / ipynb + XML / XHTML / SVG / WSDL / plist / RSS / Atom / XSL。

## 性能优化

差异与合并引擎做了多轮加固（实测基于万级文件仓库）：

- **增量扫描**：仅对变更子树重新扫描，整体约 **2.7×** 提速。
- **集合化差异计算**：用集合运算替代逐条线性比较，大仓库对比显著更快。
- **并行合并**：本地写入阶段多文件并行，约 **8×** 提速。
- **O(1) 文件树索引**：`tree_panel` 以字典索引节点，定位 / 展开不再遍历整棵树。
- **全局令牌桶限流**：`core/throttle.py` 的 `DEFAULT_REQUEST_QPS=6` 防止触发远端限流；Web 端「合并速率」旋钮可在 15~30 QPS 间调整，过载自动退避。
- **缓存优先**：远程文件树 / 内容优先走 `core/cache.py` 的 JSON 缓存（带锁），避免重复拉取；`run_merge.py` 同样缓存优先。

> 性能相关决策与设计权衡见 `deliverables/gstack/` 下的 ADR 与修复报告（如 `fix-crlf-whitespace-only-*.md`）。

## 配置文件（`.env`）

应用启动时**自动读取项目根目录的 `.env`** 作为默认连接配置（无需每次在「连接设置」里重填）。
文件已被 `.gitignore` 忽略，**请勿提交真实凭据**。支持的键名（兼容别名与拼写误差）：

| `.env` 键 | 含义 | 备注 |
| --- | --- | --- |
| `jira_url` | Jira 基址 | 也可用大写 `JIRA_URL` |
| `username` | 账号名 | PAT 克隆建议使用 PAT 所属账号 |
| `mode` | 模式 | `pat`（默认）或 `cookie` |
| `personal_access_token` | PAT | 兼容旧拼写 `persoanl_access_token` |
| `cookie` | 会话 Cookie | `JSESSIONID=...; atlassian.xsrf.token=...` |

示例：

```ini
jira_url=https://jira.cn
personal_access_token=YOUR_PAT
cookie=JSESSIONID=...; atlassian.xsrf.token=...
```

> 真实环境变量（大写键名，如 `JIRA_URL`）优先级高于 `.env`，便于 CI / 临时覆盖。
> 冻结打包后，`.env` 会同时沿「用户数据目录 `~/.jira-git-gui`」与「可执行文件所在目录」查找。

## 打包与发布（跨平台）

本工具支持 **三种发布形态**，共用同一 Python 后端：

| 形态 | 入口 | 打包 | 产物 |
|------|------|------|------|
| PyQt6 桌面版 | `main.py` | `pyinstaller build/pyinstaller_gui.spec` | `.app`（macOS）/ `.exe`（Windows） |
| Web 版 | 浏览器 | `pyinstaller build/pyinstaller_backend.spec` | 单文件后端 `jira-git-backend` |
| Electron 桌面版 | `electron/` | electron-builder（嵌入冻结后的后端） | `.dmg` / `.exe`(nsis) / `.AppImage`+`.deb` |

**关键约束**：PyInstaller 与 electron-builder 均**不支持交叉编译**——各平台产物须在对应系统（或 CI runner）构建。
已配置 `.github/workflows/release.yml`，推送 `vX.Y.Z` 标签即在 macOS / Windows / Ubuntu 三条 runner 自动出包。

打包前的两项关键重构（已完成）：

1. **`core/logger.py` 与 PyQt6 解耦**：改为懒加载，无头后端彻底脱离 GUI 框架，冻结后仅约 8 MB。
2. **运行时目录可写化**：新增 `core/app_paths.get_data_root()`，日志 / 缓存 / 下载 / 会话统一写到 `~/.jira-git-gui`（开发态仍为项目根），避免冻结包写入只读区失败。

详细的本地构建步骤、CI 流程、产物表与签名注意点见 **[docs/PACKAGING.md](docs/PACKAGING.md)**。

## 测试

```bash
# 需先激活含 PyQt6 的 venv
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

覆盖：断点续传、客户端优化（二进制下载 / 分支缓存 / 并行下载）、差异对比性能与格式化、CRLF / 行尾过滤、令牌桶限流、文件树索引、提交记录、Worker 异常保护等。集成测试需真实凭据，缺失时自动跳过。

## 已知限制

- **未签名**：本地 / CI 产物为 ad-hoc，首次打开会被 Gatekeeper / SmartScreen 拦截，正式发布请配证书。
- **根 `server.py` 已废弃**：硬编码了绝对路径，仅作历史兼容保留；新功能与打包均以 `api/server.py` 为准。
- **Python 版本**：开发环境 3.9 已做兼容兜底，CI 与正式打包建议 **Python ≥ 3.10**（3.11 验证通过）。
- **Linux 运行依赖**：桌面版需系统库 `libgl1` / `libnss3` 等（CI 已装）。
- **YAML 格式化**：结构化文件 diff 暂未纳入 YAML（需 PyYAML），后续可扩展。
