# Jira Git 通用拉取工具

针对 Jira Git Integration 插件（Xiplink / BigBrassBand）的通用桌面客户端。提供两种桌面形态：

- **PyQt 桌面版**（`main.py`）：纯 Python + PyQt6，无需浏览器，所有网络请求在后台线程执行，界面不卡顿。
- **Electron 桌面端**（`electron/`）：以 Electron 加载同一套 Web 前端（`web/`），跨平台打包，UI 特性见下文「Electron 桌面端」。

> 两套前端共用同一 Python 后端（`api/server.py`，默认端口 8787），能力完全一致。

## 两种模式

| 模式 | 认证 | 能力 | 局限 |
| --- | --- | --- | --- |
| **PAT 模式** | Personal Access Token | `git clone` 全量拉取（含嵌套文件），本地浏览/预览 | 需要该账号名下有效 PAT 与仓库名 |
| **Cookie 模式** | `JSESSIONID` 会话 | 浏览文件树（懒加载）、预览文本文件、批量/递归下载整库（含嵌套文件与二进制）、断点续传、并行下载 | 二进制文件仅支持「下载」到本地，不支持直接预览；依赖会话 Cookie 有效 |

> Cookie 模式已支持**递归整库下载**（插件接口本身支持任意 path，含子目录与嵌套文件），
> 不再受「仅根目录」限制。断点续传 + 有界并发（默认 4 线程）让整库抓取可续、可取消、更快。

## 项目结构

```
jira-git-gui/
├── main.py                 # 入口：创建 QApplication + MainWindow
├── core/                   # 核心逻辑层（无 GUI 依赖，可独立测试）
│   ├── constants.py        # 目录 / 代理 / 超时
│   ├── models.py           # ConnectConfig / RepoInfo / TreeEntry
│   ├── config.py           # 从 .env 自动载入默认连接配置
│   └── client.py           # JiraGitClient：connect / discover / list_level / get_file / clone / download
├── gui/                    # 界面层（PyQt6 组件）
│   ├── main_window.py      # 布局 + 信号绑定 + 异步任务编排
│   ├── connect_dialog.py   # 连接设置（地址/账号/模式/PAT/Cookie/仓库）
│   ├── repo_panel.py       # 发现仓库 / 手动指定仓库
│   ├── tree_panel.py       # 懒加载文件树（QTreeWidget）
│   ├── preview_panel.py    # 代码预览
│   └── log_panel.py        # 日志
├── workers/                # 异步任务层
│   └── tasks.py            # 通用 QThread Worker（自动注入 on_log 回调；异常输出完整 traceback）
├── tests/                  # 测试（先单测后集成，已纳入版本控制）
│   ├── test_download_resume.py    # 单元测试：断点续传 / 进度 / 取消（不联网）
│   ├── test_client_optimizations.py # 单元测试：二进制下载 / 分支缓存 / PAT 轻量测试 / 并行下载
│   └── test_integration.py   # 集成测试：真实访问 jira（需凭据，无则自动跳过）
├── core/                   # 核心逻辑层（含统一日志中枢 logger.py / 槽异常保护 safe.py）
│   ├── logger.py           # 文件轮转日志 + LogBridge(UI 桥) + 全局异常钩子
│   └── safe.py             # safe_slot 装饰器：拦截槽函数异常，防止界面闪退
├── store/                  # 运行期产物（git 克隆 / 下载，已 gitignore）
├── logs/                   # 运行期日志（含完整 traceback，已 gitignore）
├── server.py               # FastAPI 后端（Electron / Web 前端共用，默认端口 8787）
├── electron/               # Electron 桌面端
│   ├── main.js             # 主进程：拉起 Python 后端并承载 BrowserWindow
│   └── preload.js          # 渲染进程桥接（日志上报等）
├── web/                    # Web 前端（Electron / 浏览器通用，零框架依赖）
│   ├── index.html          # 页面结构（标签页 + 弹窗）
│   ├── styles.css          # 设计系统（浅色 / 深色双主题）
│   └── app.js              # 前端逻辑（REST + SSE）
└── requirements.txt
```

依赖方向：`gui → workers → core`，`core` 不反向依赖 GUI，便于单独复用与测试。

> **关于 `server.py`**：早期曾以 FastAPI 提供一个 Web 版后端，与桌面端逻辑重复。当前主路径是
> PyQt6 桌面端（`main.py`）。`server.py` 仅作备选保留，不再随桌面端维护，请勿混用。

## 运行

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
open run.command             # macOS 双击启动
```

> **启动自愈**：`main.py` 顶部内置 venv 自检——若当前解释器缺少 `PyQt6`，
> 会自动 `re-exec` 到项目自带的 `venv` 解释器再启动。因此用系统 `python3`
> 直接跑也不会再出现 `ModuleNotFoundError: No module named 'PyQt6'`。
> 若 venv 本身缺失 PyQt6，请先执行上面的第 2 步安装依赖。

## Electron 桌面端

以 Electron 打包的独立桌面应用：主进程（`electron/main.js`）负责拉起 Python 后端
（`api/server.py`，端口 8787）并承载一个 `BrowserWindow`，窗口内加载 `web/` 下的前端页面。
后端就绪失败会弹窗提示并退出，避免白屏。

### 启动

```bash
cd electron
npm install        # 仅首次，安装 electron
npm start          # 启动（自动拉起 Python 后端并打开窗口，1280×800）
npm run dev        # 开发模式（自动打开 DevTools）
```

> 若本机 `npm`/Electron 下载受阻，也可直接以任意浏览器访问 `http://127.0.0.1:8787/`
> （先在项目根目录启动后端：`PYTHONPATH=. ./venv/bin/python -m api.server`），
> 前端（`web/`）与 Electron 内加载的是同一套页面。

### 界面特性

- **浅色 / 深色双主题**：工具栏「🌓 主题」一键切换，偏好经 `localStorage` 持久化，下次启动自动恢复。
- **品牌头**：工具栏左侧显示应用标识（🌿）+ 名称「Jira Git GUI」，与裸网页区分。
- **实时状态点**：底部状态栏左侧指示点——绿=凭证已配置 / 黄=未配置，后端状态一目了然。
- **视觉打磨**：主按钮渐变、列表项 hover 微抬升、卡片柔和阴影、GitHub 风格 diff 表格，整体更现代统一。
- **标签页布局**：仓库 / 文件树 / 文件预览 / 提交记录 / 差异对比 / 日志，与 PyQt 版功能等价。

### 目录结构

```
electron/
├── main.js       # 主进程：Python 后端生命周期 + BrowserWindow + 日志桥接
├── preload.js    # 暴露 window.electronAPI（日志上报等，contextIsolation 隔离）
└── package.json  # name / version / start|dev 脚本
web/
├── index.html    # 结构（工具栏、标签页、连接设置弹窗）
├── styles.css    # 设计系统：CSS 变量驱动，含 body.dark 深色覆盖
└── app.js        # 逻辑：REST 调用 + SSE 日志/进度 + 纯 vanilla JS
```

---

## 配置文件（`.env`）

应用启动时**自动读取项目根目录的 `.env`** 作为默认连接配置（无需每次在「连接设置」里重填）。
文件已被 `.gitignore` 忽略，**请勿提交真实凭据**。支持的键名（兼容别名与拼写误差）：

| `.env` 键 | 含义 | 备注 |
| --- | --- | --- |
| `jira_url` | Jira 基址
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
