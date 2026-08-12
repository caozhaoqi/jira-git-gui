# Jira Git 通用拉取工具（PyQt 桌面版）

针对 Jira Git Integration 插件（Xiplink / BigBrassBand）的通用桌面客户端。纯 Python +
PyQt6，无需浏览器，所有网络请求在后台线程执行，界面不卡顿。

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
├── server.py               # 备选「Web 版」后端（FastAPI）——非主路径，桌面端以 main.py 为准
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

## 使用流程

1. 工具栏「连接设置」：填写 Jira 地址、用户名，选择 PAT 或 Cookie 模式，填入对应凭据，
   以及仓库 ID / 分支 / 仓库名。点「测试连接」可就地校验。
2. 仓库面板：若已配置 Cookie，点「发现仓库」会**翻页遍历**所有仓库并列出。逻辑：
   - **权威全量来源 = git 插件 REST 接口** `/rest/gitplugin/1.0/repository/all`
     （**不是** 复数的 `/repositories`，也不是管理员的 `/sources/repositories`）：
     返回信封 `{"success":true,"total":385,"offset":0,"count":100,"repositories":[...]}`，
     用 **offset/limit** 翻页（`limit` 上限 100，超过即 400；`startAt/maxResults` 对该端点无效），
     按 `total` 字段提前终止，可一次拉全所有仓库（实测 385 个）；
     clone 地址藏在 `gkRepoUrl`/`glRepoUrl` 的 `?url=` 参数里，客户端会自动提取；
   - 另有兜底候选端点（`/rest/gitplugin/1.0/repositories`、`/rest/gitplugin/latest/repositories`、
     `/rest/git/1.0/repository`，各试 `offset/limit` 与 `startAt/maxResults` 两种约定）应对其它版本/部署；
   - **HTML（AllRepositories 页）仅作信息补全/兜底**：在 6.x 中该页是 SPA 空壳，仓库列表由 JS 异步
     加载，静态 HTML 里没有仓库锚点（只会解析出 3 个噪声锚点）。因此 **以 REST 为权威**，HTML 仅用于
     补全 REST 已有仓库的默认分支等元信息；仅当 REST 完全不可用时才退化到 HTML；
   - **排查「为什么只返回 N 个」**：每次「发现仓库」都会把接口原始响应完整写入
     `logs/discover_raw_<时间戳>.txt`（含各 REST 端点的 URL / 状态码 / 响应体），并在主日志
     逐个端点打印「状态码 / 疑似登录页」。若发现数异常，先看该文件：REST 401/403=会话对 REST 无效、
     404=路径不对、返回登录页=会话过期、HTML 仅 3 个=该页是 SPA 空壳（应以 REST 为准）。
   - 日志会记录「HTML 解析 N 个、REST 全量 M 个、合并去重后 K 个」，便于核对是否拉全。
   - **REST 可用性缓存**：若某次发现确认所有 REST 端点均不可用（多为 401/404），会缓存该结论，
     **本次会话内后续「发现仓库」直接跳过 REST 探测**（只请求 HTML 兜底），
     避免每次都白打一堆请求冲击服务器；重新连接或重启后缓存作废。
   选中某仓库后点「查看文件」（或双击）即加载其文件树；也可手动填写仓库 ID 后点「加载文件树」。
3. 文件树：展开目录懒加载子项（分支为空时客户端会自动探测可用分支，如 master/main）；
   勾选文件「选择」列后可点「下载选中(Cookie)」。
4. 点击文件节点在右侧预览正文（文本文件；二进制文件会提示用「下载」保存到本地查看）。
5. 工具栏「下载整个仓库(Cookie)」会**递归遍历整棵文件树**并下载所有文件，保持目录结构；
   支持断点续传、进度条、取消、有界并发（默认 4 线程）。再次点击同一仓库会从断点继续。
6. PAT 模式点「克隆仓库(PAT)」全量 clone 到 `store/repos/<repoId>/`，随后以本地模式浏览。
   「测试连接」中的 PAT 校验已改为 `git ls-remote` 秒级验证（不再触发完整 clone）。
7. **查看提交记录**（右侧「提交记录」标签页）：提供两种模式（顶部「模式」下拉切换）——
   - **按 Issue 查询**：填入 Jira issue 单号（如 `TST-234`）后点「查询」，列出该 issue
     关联的全部提交（SHA / 作者 / 时间 / 提交说明）；选中某条会在下方显示改动文件清单
     （路径 / 变更类型 / 增删行数）。也可留空，尝试按当前仓库拉取（部分私有部署不开放
     按仓库列全量提交的接口，届时会有提示）。
   - **本地 Git 仓库**：对**已通过 PAT 模式克隆**到 `store/repos/<repoId>/` 的仓库，直接跑
     `git log` 拿到**完整提交历史**（不依赖 Jira REST）。未克隆时会有提示，请先点「克隆仓库(PAT)」。
   - **点文件看历史版本**：选中提交后，右侧「变更文件」列表中**单击任一文件**，会在「文件预览」
     标签页打开该文件在**此提交时的历史内容**（本地克隆走 `git show <sha>:<path>`；否则用
     commit SHA 作 ref 调 Cookie 文件接口）。
8. **下载并发数可调**（工具栏「并发」数字框，1–16，默认 4）：批量/整库下载会按此并发数
   用有界线程池并行抓取，兼顾速度与稳定性；断点续传 / 进度 / 取消不受影响。
   - **批量下载复用单个 HTTP 客户端**：整库下载（数百~数千文件）只在批量开始时创建
     一个 `httpx.Client`（带代理/重试）并在线程池内共享，避免每文件重复 TCP/TLS 握手，
     大批量下载耗时显著下降。
   - **HEAD commit 缓存**：`(repo_id, branch) -> HEAD` 按键值缓存，重复读取文件 /
     同一批量内不重复解析，减少「分支自动探测 + 取 HEAD」的冗余请求。
   - **状态栏实时同步分支**：文件树自动探测到分支、下载/预览解析到分支后，底部状态栏
     立即刷新，不再停留在「(默认)」。
   - **请求速率限流（保护服务器）**：工具栏新增「速率」数字框（1–50 请求/秒，默认 6）。
     所有经 `http_get` / `_request_with` 的对外请求都先经过模块级**令牌桶限流**
     （`core/throttle.py`），无论下载并发开多大，对 Jira 服务器的稳态请求速率都被钳住，
     避免整库递归抓取（数千文件）把对方打崩。遇到 `429`/`503` 还会读取 `Retry-After`
     头做长退避。限流速率可运行时热更新，并实时显示在状态栏。

### 界面布局

主窗口采用「左树右栏」结构：

- **左侧**：上方仓库面板（发现/指定仓库），下方文件树（懒加载、可勾选下载）。
- **右侧标签页**：`文件预览` / `提交记录` / `日志`。底部状态栏实时显示
  当前 模式 / 仓库 / 分支 / Cookie·PAT 配置状态。

## 已知约束

- 当前提供的 PAT 若与登录账号不匹配（base64 前缀解出的账号 ≠ 登录账号），克隆会被 Jira 拒绝，
  需在该账号名下重新生成 PAT。
- Cookie 模式下载的二进制文件按字节原样落盘；但预览仅支持文本，二进制需在本地用其他工具打开。
- HTTP 请求默认 `verify=False`（企业内网经代理时绕过证书校验）；若需严格校验，可在
  `core/client.py` 的 `http_get` 中启用 `verify=True`。
- 运行时凭据仅存于内存，不写入磁盘；`store/` 已 gitignore。

## 日志与崩溃追溯

程序内置完整日志体系，便于定位「点一下就闪退」之类问题：

- **日志文件**：`logs/jira_git_gui.log`（带时间戳 + 级别 + 轮转，单文件 5MB × 3 备份）。
  任何未捕获异常（主线程 / 子线程 / Qt 消息）都会写入**完整 Python traceback**。
- **UI 日志面板**：主窗口右下角实时显示同样的日志，无需打开文件即可看到进度与错误。
- **全局钩子**：`main.py` 启动时通过 `install_global_hooks()` 接管
  `sys.excepthook` / `threading.excepthook` / Qt 消息处理器；即使发生致命异常也会先落盘再尽力弹窗提示，
  不会「静默闪退无痕迹」。
- **槽函数保护**：所有信号槽用 `core/safe.py` 的 `@safe_slot` 包裹，槽内异常被捕获并记录，
  不会抛回 Qt 事件循环导致进程退出。
- **后台任务**：`Worker` 在异常时不仅 `error` 信号上抛，还会把完整堆栈写入日志文件。
  其中**用户可预期的提示**（缺配置、会话过期、未选仓库、功能不支持等）被归类为
  `core.errors.UserError`，仅以 **WARNING** 级别记录且不打印 traceback，UI 仅显示
  友好文案——避免「请先选择仓库」这类提示被误记成 ERROR + 完整堆栈的日志噪音；
  其余**真正的代码缺陷**仍按 ERROR + 完整 traceback 处理，便于追溯。
- **文件树异步安全**：目录子项懒加载的回调**只通过 path（稳定字符串）传递节点身份，
  不再跨线程持有 `QTreeWidgetItem`**。回调触发时按 path 重新查找「活的」节点；若期间
  切换了仓库 / 重新加载了根目录（`tree.clear()` 已销毁旧节点），查找失败则安全丢弃过期
  结果，避免 `wrapped C/C++ object of type QTreeWidgetItem has been deleted` 崩溃。
  另：`set_children` 设置 `loaded` 标记后会 `setData` 写回（PyQt6 的 `data()` 返回副本，
  不写回则标记永不生效，每次展开都会重复请求）。

**遇到崩溃时**：把 `logs/jira_git_gui.log` 末尾的 traceback 内容反馈即可定位。

## 测试

测试遵循「先单元测试、后集成测试」的顺序。`core` 层是纯逻辑、不依赖 GUI，单测完全离线；
集成测试需要真实凭据，缺失时自动跳过。

```bash
cd /Users/caozhaoqi/PycharmProjects/jira-git-gui
PYTHONPATH=. ./venv/bin/python -m unittest discover -s tests -t .
```

### 1) 单元测试（离线、必跑）
`tests/test_download_resume.py`：断点续传清单读写、整树枚举、批量下载落盘、续传跳过不请求网络、
进度计数、取消中途停止。

`tests/test_client_optimizations.py`：二进制文件按字节落盘、分支探测缓存、PAT 轻量连通测试
（`git ls-remote` 成功/被拒/缺用户名）、并行下载并发与取消、`max_workers=1` 退化为串行、
**整批下载只创建并复用单个 HTTP 客户端**、**HEAD commit 缓存命中**。

`tests/test_worker.py`：Worker 透传 `max_workers`/自定义 kwargs；**错误分级**——`UserError`
只上抛纯消息（不带 traceback），真实异常仍带完整 traceback。

`tests/test_throttle.py`：令牌桶限流节奏（`qps`/`burst` 钳制稳态速率）、`burst` 允许短促突发、
`set_qps` 热更新、全局单例；**429/503 退避**——识别需退避状态码、`Retry-After` 秒数优先、
无头时指数退避（封顶 30s）。

`tests/test_discover_repos.py`：REST 信封归一化（6.x 的 `{total,repositories:[...]}` 信封 / 旧版包装 / 裸数组 / 单对象）、
仓库项解析（含 **id=0 边界** 与从 `gkRepoUrl`/`glRepoUrl` 提取真实 clone 地址）；
**HTML 翻页遍历**——`pageSize`/`pageIndex` 逐页取尽、`out of N` 总数提前终止、空页/末页自动停止（SPA 兜底用）；
**REST 翻页遍历**——实测端点 `/rest/gitplugin/1.0/repository/all` 用 `offset/limit` 翻页（limit 上限 100）、
按 `total` 提前终止、服务端忽略分页参数时自动停止（不死循环）；
**合并去重**——REST 权威全量 + HTML 仅补全默认分支 + 发现数日志（INFO/WARNING）；
**REST 不可用缓存**——HTML 只给 3 个 / REST 全 401·404 的真实场景回归：首次返回 3 个并缓存「REST 不可用」，
后续发现跳过 REST 探测（退化到 HTML），`set_config` 换配置后缓存作废。

### 2) 集成测试（需凭据，自动跳过）
`tests/test_integration.py` 真正访问 Jira 验证「发现仓库 → 查看文件 → 下载」全链路。
设置以下环境变量后才会执行（建议在本机、网络/代理可达时运行）：

```bash
export JIRA_URL=https://jira.example.com
export JIRA_COOKIE="JSESSIONID=...; atlassian.xsrf.token=..."
# 可选：PAT 克隆链路
export JIRA_PAT="<Personal Access Token>"
export JIRA_USERNAME=""
export JIRA_REPO_ID=""
export JIRA_REPO_NAME=""
PYTHONPATH=. ./venv/bin/python -m unittest tests.test_integration -v
```

未设置凭据时这些用例会以 `skipped` 形式干净跳过，不影响整体结果。

