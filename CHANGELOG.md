# CHANGELOG


## v0.0.10

- **HCM 接口调试面板补齐 WS 通道与响应树**（对齐网关自带调试页）：
  - 新增 `api/hcm/ws_bridge.py`：逆向网关 `static/api/api.js` 的 `callHcmOpenWebSocket` 协议（`ws(s)://<host><前缀>/ws/<api>`，握手 Cookie token，首帧**明文业务参数**（无 REST 加密信封），回包 `{status: notify|success|error, data}`；notify 可多条、`data=="pong"` 为心跳，success/error 为终态）。两个入口：`ws_call`（一次性调用，`POST /api/hcm/ws-call`）与 `ws_proxy`（流式双向桥接，`WS /api/hcm/ws`）；两个 connect 均显式 `proxy=None`（禁环境代理，与 hcm_direct「不走代理」约定一致）。
  - 登录态失效自动重登：`请先登录` / errcode 17003、51006 / bhcd 网关兜底码 `system_errors_msg_00000055` 均触发（ws_call 重试一次；ws_proxy 重建连接并重发首帧，本地无感知）——预设 token 属另一网关时用户选网关点 WS 即可用，与 REST 通道体验对齐。
  - 前端：WS 按钮 + 连接/运行/终态状态点 + notify 进度折叠面板；结果区重构为 REST/WS 共用，新增 Tree/Text 视图切换（`JsonTree` 组件：可折叠树 + `{n}`/`[n]` 徽标 + 搜索高亮/命中计数/↑↓ 跳转/复制值与路径）；修复「WS 终态后 REST 结果被掩盖」（结果区以最新一次请求为准）。
  - 依赖：`websockets>=12.0` 补进 requirements.txt 与 PyInstaller spec hiddenimports（v0.0.9 踩过漏声明坑）。测试 `tests/test_hcm_ws_bridge.py`（12 例：mock 网关协议回放 + 路由 + 自动重登/重建）；现网 E2E：bhcd 网关 ws-call 与流式代理均 success（count 3288）， playwright 全流程 21 项 PASS。
- **HCM 接口调试面板 View 三态 + 非 JSON 错误页处理**（续对齐网关调试页）：
  - 结果区视图切换由 Tree/Text 两态按钮组改为调试页同款 **View 下拉（视图 / 文本 / 代码）**；新增 `CodeView` 组件：行号 + 自研轻量 tokenizer 语法高亮（JSON 键/串/数字/关键字，HTML doctype/标签/属性/属性值/注释；截断的 JSON 也按 JSON 高亮），搜索命中 `<mark>` 高亮 + 命中行数统计。tokenizer 自研而非引 highlight.js：token 边界明确、搜索替换不会误伤 class 名、无 `dangerouslySetInnerHTML`。
  - 非 JSON 响应（网关 500 HTML 错误页 / nginx 页 / SSO 跳转页）：后端 `hcm_direct` 不再抛 HTTPException 把整页 HTML 塞进 message，改为结构化 `{ok:false, error.raw=原文}`（HTTP 200），前端红色结果块展示 + **按内容自动选视图**（结构化 JSON → 树，HTML/纯文本 → 代码视图，树对非 JSON 无意义）；错误块新增 errmsg 摘要行（红条），raw 原文优先展示时可读性不丢。
  - 顺带修复 3 处既有 CSS 级联问题（`.hcm-api-chk` / `.hcm-ops-keyinput` / `.hcm-ops-decinput` 单类变体被 panels.css 基类压掉）——改复合选择器提特异性，`audit_css_cascade` 由 4 项告警清零。
  - 测试：新增 `tests/test_hcm_direct.py`（6 例：成功/业务错误/HTTP 500 HTML/200 非 JSON/502 上游页/路由层结构化；⚠️ fake client 必须按 URL 过滤响应，否则全量跑时 startup 的 cf auto-login 会消耗脚本响应）；playwright E2E 27 项 PASS（真实网关 JSON 流程 + page.route mock HTML 500 全流程：红块/自动代码视图/行号/高亮/搜索/树兜底）。
- **WS 调试会话状态机修复**（终态与连接关闭竞争）：
  - 修复「WS 已拿到终态错误响应（结果区红块 + HTML 代码视图）却仍弹 `WS connection closed` 错误条」——onerror/onclose 在**已拿到终态（final）**时不再改会话状态（终态后网关/后端关连接是协议既定行为，一次性会话）；终态帧到达时清掉此前的连接层 error 字段（覆盖 onclose 先跑的时序）。
  - 非 JSON 帧（网关异常页/裸文本）不再被静默丢弃：对齐后端 `ws_call` 语义，按 error 终态处理并保留原文（HTML 进代码视图，用户看得见内容）。
  - 真连接中断（无终态）仍保留 `WS 连接已关闭` 错误反馈；notify 进度后中断 → 进度面板 + 错误条并存（进度可见 + 失败原因明确）。
  - 验证：playwright E2E 17 项 PASS（mock 网关五场景回放：error-HTML 终态/裸 HTML 帧/success/静默关闭/notify 后中断），真实网关回归 27 项 PASS。
- **服务账号支持配置域名别名（domain）**：cf_accounts 条目新增可选 `domain` 字段（仅下拉展示识别，连接/登录仍走 server_url）；全链路贯通（load_cf_accounts → services-config GET/POST/PUT → /api/cf/accounts → /api/cf-debug/accounts → hcm_envs → kibana list_servers）；HCM 接口/HCM 运维/HCM 对象/云函数/云函数调试下拉均展示「name · url · domain」；系统设置服务账号表格新增域名副行。测试 `tests/test_cf_account_domain.py`（6 例）。
- **AI 数据桥**（前后端全链路）：把每个功能面板的实时输出暴露为本地 HTTP 接口，外部 AI 助手可直接拉取应用正在显示的数据。
  - 后端 `api/ai_bridge.py`：内存环形缓冲（每 tab 20 条、单条 256KB 截断），`POST /api/ai/output/{tab}` 上报 + `GET /api/ai/output` 概览 + `GET /api/ai/output/{tab}?format=text&at=-n` 拉取 + `/history` 历史；TAB_DESCRIPTIONS 白名单限定 tab。测试 `tests/test_ai_bridge.py`（9 例）。
  - 前端：`api/aiBridge.ts` 上报（fire-and-forget + 800ms 节流 + localStorage 开关 `jgg-ai-bridge`）；13 个数据源面板全量接入（云函数日志/差异对比/一致性/调试/Clash/HCM 对象/接口/运维/统一诊断/K8s 快照/Kibana/应用日志/仓库）；新增 `AiBridgePanel` 状态总览页与 `AiApiFab` 悬浮按钮（任意面板右下角一键复制 curl）。
- **HCM 运维**（新 tab）：
  - Redis：Key 浏览/搜索/批量删除（二次确认）+ 常用 Key 预设。
  - 线上错误：`monitor.error.get_error_list` 报错监控，按时间/关键字过滤。
  - 参数解密：`api_decrypt_param` 抓包参数还原明文（嵌套信封：外层明文 body 内 `hcm_param` 为加密载荷；token 过期表现为 `code:500 NoneType.keys`，新增 `_suspect_auth` 判定触发自动重登重试）。
  - `hcm_direct` 401/403/空 body 也触发自动重登（部分网关 token 失效不回 errcode 结构）。
- **侧栏两轮重构**：
  - 10 个页签按域收进「云函数 / HCM / 系统」三个可折叠分组（手动折叠优先，组内激活自动展开）；高频页签平铺，`Cmd/Ctrl+1..6` 直达（Electron 原生菜单 Cmd/Ctrl+Alt+1..4）。
  - `Tabs.tsx` 重写：TabGroup 结构、section 组头、hint 角标。
- **全局图标统一**：新增 `components/icons.tsx` 33 个 16×16 单色 SVG 图标（stroke=currentColor，随主题/激活态着色），全站 55 个文件 323 处 emoji 全面替换为统一图标。
- 文档：README / README.zh-CN 补充 HCM 运维、AI 数据桥、新侧栏结构与快捷键说明；ARCHITECTURE.md 补 `api/ai_bridge.py` 与 HCM 运维模块说明。
- Electron：`ALL_TABS` 补齐 cfsync / hcmapi / hcmops / aibridge。


## v0.0.9

- 修复 GitHub CI/CD 打出来的安装包「后端起不来」：现场 `Failed to load Python DLL
  '...\python311.dll'` → `LoadLibrary: ?????` → 退出码 -1，四次重启后 Electron 放弃。
  三层原因、都已修：
  - **主因一（CI 三端全部中招）**：`requirements.txt` 漏声明运行时依赖——`core/k8s/env.py`
    顶层 `import yaml`（PyYAML），冻结出的后端一启动就 `ModuleNotFoundError: No module
    named 'yaml'`。本机 venv 碰巧装了这些包所以本地构建不报错，CI 只装 requirements.txt，
    产物三端全坏。补齐 `PyYAML` / `paramiko` / `debugpy` / `SQLAlchemy`（后三者为懒加载依赖，
    同时补进 spec 的 hiddenimports），并把直接 import 的 `pydantic` / `anyio` / `starlette`
    显式声明，不再依赖 fastapi/uvicorn 的传递依赖。
  - **主因二（Windows 独有）**：`core/k8s/exec_pty.py` 顶层裸 `import fcntl`（`pty` /
    `termios` 同理），这三个模块 Windows 上不存在。改为 `try/except ImportError` 降级 +
    `PTY_SUPPORTED` 标志：`PtySession.start()` 在不支持的平台抛带提示的 RuntimeError
    （引导改用一次性命令通道），`_set_winsize` 直接返回 False；Unix 行为不变。
    新增回归测试 `tests/test_backend_import_windows_safe.py`：AST 扫描 api/+core/ 顶层
    裸 import 的 Unix 专属模块 + 子进程挡掉这些模块后 `import api.server` 必须成功。
  - **隐患（Windows 特有、未纳管）**：spec 的 `upx=True` + `strip=True`。UPX 压缩后的
    Python DLL 在 Windows 上 `LoadLibrary` 失败是 PyInstaller 官方点名的已知问题
    （错误 193/126），与现场 `LoadLibrary` 失败症状吻合；两开关还使产物随 runner 工具
    环境漂移、不可复现。前后端 spec 均已关闭。
  - **新增验证**：`release.yml` 三个 job 在「Build backend」后强制拉起冻结后端并探测
    `/api/status`，探不到直接让工作流失败；并固定 `pyinstaller==6.22.0`。两轮 CI 冒烟
    分别抓到了上面两个主因（第二轮：macOS/Linux 全绿，Windows 浮出 fcntl）。
- 启动失败弹窗新增「打开日志目录」按钮：后端启动失败 / 端口不可用 / 后端持续崩溃时，
  原来只有「确定」，日志路径看得见点不着；现在一颗按钮直接在资源管理器/访达定位并选中
  日志文件（`shell.showItemInFolder`），Windows 现场不用再手抄路径。

- UI 更新
- kibana、hcm object 功能更新


## v0.0.8

- 支持云函数日志多开
- 体验优化
- UI交互更新



## v0.0.7

- 文件树：远端目录层级缓存 + 「粘贴完整路径直达」
  - **层级缓存（后端）**：`core/client/browse.py` 给 `list_level` / `list_level_ex` / `_list_level_cookie_ex` 链路加短 TTL 缓存（复用 `core/cache.py`，命名空间 `remote-tree-<repo_id>`，key `branch|path`，TTL 300s）。来回切目录、刷新页面、重复跑差异扫描都直接命中，少打 Jira。
    - 补上原先**只加读没加写**的缺口：`_tree_entries_for_cache()` 定义了却从未调用，缓存永远不可能命中；现在在「页面 200 且树解析成功」的唯一出口写盘。
    - 命中判定必须用 `is not None` 而非真值：空目录解析出 `[]` 也是有效结果，真值判断会把空目录当成未命中而反复回源。
    - 失败响应（登录页 / 非 200 / 解析不出树）一律提前 return，**绝不落盘**，避免 Cookie 一失效就把空结果缓存成「目录是空的」。
    - 新增 `refresh` 参数（`/api/tree?refresh=true`、`list_level(..., refresh=True)`）强制回源并回写；新增 `invalidate_remote_tree_cache(repo_id="")` 与 `client.invalidate_tree_cache()` 按仓库 / 全局失效。
  - **路径直达（前后端）**：新增 `GET /api/tree/resolve?path=...&local_dir=...`，把「从根到目标」的 N 层**并发**拉取后一次性返回，墙钟时间从「点 N 层 × 单次延迟」压到「~1 × 单次延迟」（外加令牌桶排队，默认 6 QPS）。
    - 返回 `levels`（各层条目，前端据此一次性铺开整条祖先链）、`target`（dir / file / missing）、`broken_at`、`elapsed_ms`。
    - **路径存在性校验**：远端对不存在的路径只回空树、不给 404，光看响应分不清「空目录」和「粘错路径」；后端沿链逐级核对父层条目，把断点精确报给前端。
    - 拒绝 `..`（本地目录模式下会拼到 `local_dir` 后面，放行会越权读到仓库外）；深度上限 32 层。
    - 前端 `FileTree.tsx` 新增「直达路径」输入条（回车或按钮触发）：灌满 `dirCache`、展开整条祖先链、目标为文件时直接预览，并高亮 + 滚动到目标节点。滚动只触发一次（用独立的 `pendingScroll`，避免之后每次展开无关目录都被拽回直达目标）。
  - 顺带修复：`core/client/connection.py` 的 `_get_http_client()` 惰性创建**无锁**——并发首启（直达 / 差异扫描一瞬间起多个线程）会各建一个 `httpx.Client`，只留最后一个挂在实例上，其余连接池泄漏。改为双重判空 + 加锁。
  - 测试
    - 新增 `tests/test_tree_level_cache.py`（8 例）：锁住缓存三不变量——① 同层第二次请求零远端请求；② 空目录也算命中；③ 失败响应绝不落盘（且恢复后能立刻拿到数据）。
    - 新增 `tests/test_tree_resolve.py`（7 例）：锁住直达三不变量——① N 层并发而非串行（6 层 × 0.3s 串行需 1.8s，断言远低于此且 `max_concurrent >= 2`）；② 坏路径精确报出断点；③ 拒绝 `..`。
  - 验证：前端 `npm run build`（含 `tsc --noEmit`）通过，dist 已刷新；全量 pytest **230 passed / 0 failed**；实起 `api.server` 冒烟 `/api/tree` 与 `/api/tree/resolve` 均正确应答。

- 修复：进度「预计剩余」倒计时不准确（差异扫描为主，克隆 / 下载 / 合并一并受益）
  - **根因一（扫描特有，结构性）**：旧实现 `DiffPanel.tsx` 用 `const frac = (pct - 10) / 70` 反推后端 `pct` 的魔数重映射，再拿「已扫**文件**数」除以「**目录**进度比例」反推总量（`totalEst = done / frac`）。两个单位混着除，只有「每个目录文件数均匀」时才成立。实测算例：根下 30 个目录、最后一个独占全仓 99% 文件时，目录进度 97% 处旧模型估总量 = 150（真实 24145，差两个数量级，界面谎报「几乎扫完」），扫完那个大目录后又一步暴涨 161 倍——这就是「剩余时间越走越长 / 来回跳」的来源。
    - 修法：改用后端 `scan_stage`（stage=remote）里已有的 **`local_count`** 作为远端文件总量估计。本地与远端扫的是同一仓库的同一子目录（`compare_dir` 同时收窄两侧），文件数高度接近，且**与进度里的 `done`（已扫文件数）同单位**。拿不到该值、或已扫数反超估计值时返回 null——宁可不显示，也不给离谱数字。`pct` 只保留给进度条用。
  - **根因二（三处共有）**：`rate = (done - d0) / elapsed` 是**任务开始至今的累计平均速率**，速率一变就严重滞后。实测「前 10s 扫 900 个、后 10s 只扫 50 个」时真值 10s，旧值给 1.05s（低估近 10 倍）。
    - 修法：`utils/eta.ts` 改为 EMA 跟踪**近期**速率（默认 alpha 0.3，扫描用 0.2 更保守）。
  - **平滑约束施加在速率上，不在 ETA 上**（关键设计决定，实现中踩过坑）：最初直接对 ETA 做百分比限幅，结果恒速前进时本该线性下降的 ETA 被误判成「跳变」拦住，越到后面越滞后（真值 41s 却显示 71s）。改为对**速率**限幅（`maxRateJump`，默认 1.6 倍/采样）后：恒速时 ETA 自然线性倒数（实测 231/141/41s 与真值完全一致），突变时又跳不动（停滞实测单步最大 1.42x）。
  - 置信度门槛：样本数不足 / 预热期内 / 速率未建立时返回 null（界面不显示），避免开局给个离谱数字；时间倒流、进度回退的脏样本直接丢弃，不污染速率。另移除已无人调用的 `etaFromFraction`（它正是单位混用那个脚枪）。
  - 测试：新增 `tests/test_eta.mjs`（25 项断言）。Node 22.18+ 默认支持 type stripping，可直接 `import '../frontend/web-react/src/utils/eta.ts'`，无需额外编译步骤。运行：`node tests/test_eta.mjs`。


## v0.0.6


- 差异对比「git 风格管理仓库」工作流（git-style management repo workflow）
  - 仓库对比选择器：下拉项同时显示「仓库名 · ID」，同名仓库靠 `repo_id` 区分（本环境存在同名仓库，仅靠名称无法分辨）；`value` 仍用 `repo_id`，选中逻辑不变。
  - 对比目录（compare-dir）选择器：手动输入路径 + 文件树浏览弹层（走 `/api/tree`，限 `type==='dir'`），只扫该子目录而非整仓。
  - 快速扫描（fast scan，默认开）：只取远端文件 `size`、不下载内容；`compute_diff` 在两端 hash 都为空时退化为按 size 比较，零内容下载完成差异判定。
  - 最近更新记录（git log 风格）：「最近更新」按钮走 `GET /api/diff/commits?path=compare_dir`，列出该目录近期提交。
  - 已合并标记：每个差异条目带 ✓ 徽标（来自 `GET /api/diff/merge-manifest`），面板顶部显示已合并计数；判定标准是「本地文件 md5 == 记录 remote_hash」。
  - 合并断点续传：manifest 落盘到应用数据目录 sidecar `get_data_root()/merge_state/<safe_local_dir>/manifest.json`（与仓库解耦，不进 `local_dir`，不会污染 git status）；二次合并自动跳过已合并项。

- 修复：合并续传「本地被改后不重新覆盖」
  - 根因：跳过判断误用远端内容 hash（`content_hash(remote_content) == rec["remote_hash"]`），于是本地被用户改过、远端却没变时也会命中跳过，永不把本地拉回远端 → 静默丢掉「重新同步」语义。
  - 修法：`api_diff_merge` 与 `api_diff_merge_batch::_writer` 统一改用 `is_already_merged(local_dir, path, manifest)`（本地文件当前 md5 == 记录 remote_hash 才跳过）；本地若被改（md5 不符）则一定重新抓取并覆盖。

- 修复：批量合并大文件 / 二进制整批失败
  - 根因：① web viewer 不内嵌大文件内容（页面提示 too large / binary）；② `get_file` 设计给「预览」用，遇到 bytes 直接拒绝「二进制文件…」。合并需要把远端字节写回本地，被这一步拒了 → 批量合并整批失败。
  - 修法：`core/client/files.py`：`get_file(path, allow_binary=False)` 合并场景传 `True` 时返回 bytes（不再以「无法预览」为由拒绝）；`_cookie_file_content` 在 viewer 内嵌失败时新增 `_fetch_raw_file(repo_id, ref, path)` 兜底——打插件 REST 原始文件端点（`/rest/git/1.0/repositories/{repoId}/files/{ref}?path=` 与 `/rest/gitplugin/1.0/repository/{repoId}/files/{ref}?path=`）绕过 viewer 大小限制直接取字节。
    **严格守门**：只接受原始字节（text/plain / octet-stream / 未知）或 JSON 包内 `content`/`rawFile`（base64 或文本）；HTML / JSON 错误包一律拒绝，绝不把错误页当文件内容写坏本地。取不到则给诚实报错。
  - `core/diff/scan_remote.py::get_file_cached(..., allow_binary=False)` 透传 `allow_binary` 给 `client.get_file`；合并（单 / 批量）传 `True`，预览 `api_diff_file` 保持 `False`。

- 测试
  - 新增 `tests/test_scan_remote_fast.py`（4 例）：锁住快扫两不变量——① `fast_hash=True` 时 `scan_remote` **绝不调用 `get_file`**（零内容下载，扫描加速核心）；② 空 hash 退化为 `compute_diff` 的 size 比较，仍能正确给出 modified / same / remote_only / local_only。
  - `tests/test_diff_merge_routes.py`：新增 `resume_refetches_when_local_changed`（本地被改则重抓、未改跳过）；`_install_spy` 与两处内联 spy 签名加 `allow_binary=False`（否则 `unexpected keyword argument 'allow_binary'`）。
  - `tests/test_differ_perf.py`：`FakeClient.get_file` 与 `SlowClient.get_file` 加 `allow_binary=False`。

- 验证：前端 `npm run typecheck`(tsc --noEmit) 全过；`npm run build`(`vite build --base /web/`) 成功，dist 已刷新。全量 pytest（`--basetemp=.pytest_tmp`）207 passed / 0 failed（仅 fastapi on_event 弃用 warning）。



- K8s 文件下载：修复「10MB 只下载了几百 KB」，改为分片下载 + 断点续传 + 进度条
  - **根因**：前端「下载」按钮直接 `new Blob([editContent])`，而 `editContent` 来自 `/api/k8s/file/read`，后端默认按 `max_bytes=200000` 截断；二进制文件还会被 `is_binary` 直接拦下，压根进不了编辑器。
  - 新增后端接口 `POST /api/k8s/file/stat`（文件大小 + mtime）与 `POST /api/k8s/file/download`（按 `offset`/`length` 取 `[offset, offset+length)` 的原始字节，**二进制安全**）。
  - 容器内取片用 `tail -c +N | head -c L | base64`；精简镜像（distroless 等）没有 `base64` 时自动回退 `od -An -v -tx1` 十六进制，出参一律统一为 base64，前端只需处理一种编码。
  - 新增前端下载器 `src/utils/k8sFileDownload.ts`：**offset 只在成功接到数据后才前进**，单片失败重试不会重来已下载的部分（断点续传），支持暂停 / 继续 / 取消，失败按 500ms→8s 指数退避重试。
  - 新增进度条组件 `K8sDownloadBar`：百分比 / 已下载 / 总量 / 分片序号 / 速度 / 剩余时间 / 重试次数；`stat` 失败时退化为「未知大小」的不确定态动画条，不会卡在 0% 让人以为没动静。
  - 文件列表每行新增 `⬇` 按钮，二进制文件不进编辑器也能直接下载（提示文案同步改为引导走该按钮）。
  - 内存保护：超过 1.5GB 提前失败并提示改用 `kubectl cp`，避免把标签页撑爆。
  - 切换 Pod / 环境或卸载面板时自动中断在途下载，避免旧 Pod 的分片把进度写回新界面。
  - `apiPost` 支持传入 `RequestInit`（如 `signal`），用于取消在途请求。

- K8s Shell 真 PTY 终端：修复 `vim` / `top` / `less` 等全屏程序「输入无反馈」与显示错位
  - 修复连上后敲命令毫无反应：等待 READY 标记时残留的首屏输出是**已解码的 str**，却被当成 bytes 二次解码，抛 `TypeError` 使会话泵起不来（READY 与首屏输出落在同一批读取时必现），且异常发生在 `try` 之前导致会话未清理。
  - 修复终端尺寸永远锁死 80x24：远端启动脚本的 `stty rows/cols` 在 READY 之后执行，会覆盖客户端刚上报的真实尺寸；改为建连即上报 `?cols=&rows=`，并在 resize 后延迟重放一次（幂等）。
  - 修复全屏光标错位：TTY 模式下关闭 xterm `convertEol`（远端 pty 已输出 CRLF，再转换会变 `\r\r\n`），本地提示文案统一 `\r\n`；ready 时不再插入本地 banner，让本地屏幕与远端 pty 行号对齐。
  - 窗口尺寸同步改用 `ResizeObserver`，面板折叠/分栏拖动（不触发 `window.resize`）也能同步。
  - 行缓冲降级模式执行 `vim` / `top` 等全屏命令时给出明确提示，不再静默无反馈。
  - `kubectl` 子进程改用解析出的绝对路径，避免装在非标准位置时 exec 失败导致黑屏。

- 测试：新增 `tests/test_k8s_shell_pty_e2e.py`（mock kubectl + 真 PTY，无需 k8s 集群即可端到端验证 vim/top/less/resize/首屏尺寸/降级提示）；修复 `test_k8s_exec_mock.py` 在 PTY 分支引入后因缺 `query_params` 而失败。
- 测试：新增 `tests/test_k8s_file_download.py`（15 例，把「容器内执行」落到本机 `sh -c`，覆盖 10MB 分片重组 md5 一致、6MB 处续传、全 256 字节值二进制安全、hex 回退归一化、eof 语义、特殊字符路径、长度 clamp、HTTP 全栈往返）；新增 `tests/test_k8s_file_download_dl.js`（31 断言，esbuild 打 TS + stub fetch，覆盖重试续传、暂停/继续、取消、内存上限、工具函数）。


- feat：cloud function error snippet locate
- fix: some bug

## v0.0.5

- React 前端迁移完成：`web/` 已全面切换为 `web-react/` 的构建产物，原生 vanilla-JS 版本归档至 `web-legacy/`。
- 修复 K8s 快照卡死：事件总线抽到独立 `api/eventbus.py`（解决 `python -m` 启动导致的双模块实例分叉，SSE 订阅者收不到 `k8s_done`/`k8s_finished`）。
- 修复 K8s Shell 失效：后端 `_ws/k8s/exec` 重复拼接 `kubectl` 导致 `unknown command "kubectl"`；改为替换 `argv[0]`。
- Shell 输出规整：xterm 改为只读输出区、命令本地回显、`cls` 映射 `clear`、后端实时过滤 `__PWD__` 跟踪标记、子进程设 `COLUMNS=240`。
- Pod 列表不溢出：`.k8s-table` 改 `table-layout: fixed` + 横向滚动；快照页移除运行日志区，改「已选 Pod」操作栏。
- 文档同步：README / README.zh-CN 更新 `web/` 为 React 产物、日志查看器路径改为 `/web/?view=log`、补充本轮修复说明。
- update：ui design

## v0.0.4

- add： clash rule： local ip

## v0.0.3

- react + type scripts update ui

## v0.0.2

- tauri build update
- update feat

## v0.0.1

- electron add
- k8s feat add
- cloud function add