# 项目架构与模块地图

> 本文聚焦「代码组织 / 各模块职责 / 依赖方向」，功能介绍见 [`README.md`](../README.md)。
> 目标：让任何新接手的人能在一页内看懂 *jira-git-gui* 是如何分层与按业务域拆分的。

## 一、总览

```
jira-git-gui/
├── main.py                          # 遗留 PyQt6 桌面入口（仅供参考，交付形态见 electron/tauri）
├── api/                             # ★ 后端（FastAPI）—— 对外 HTTP / SSE / WebSocket 契约，按业务域分子路由
├── core/                            # ★ 核心逻辑层（无 GUI 依赖，按业务子域分目录）
│   ├── client/                      #   JiraGitClient（连接/仓库/文件/下载/看门狗）
│   ├── config/                      #   连接 / CF / HCM / 会话配置
│   ├── diff/                        #   差异扫描 / 对比 / 合并（含断点续传 manifest）
│   ├── k8s/                         #   kubectl 封装 / 环境 / Pod / exec+PTY / SSH 远程 / 快照
│   ├── kibana/                      #   Kibana/ES 日志查询、服务器派生站点
│   ├── sync/                        #   同步历史存储
│   └── (顶层通用)                   #   app_paths / cache / constants / errors / log_retention /
│                                    #   logger / models / safe / throttle / watchdog
├── frontend/web-react/              # ★ 活跃前端源码（vite + React + TS），构建产物 dist/ 由 api 挂载 /web
├── web/                             # 旧版前端（原生 JS/CSS，归档；dist 存在时不经 /web 提供）
├── electron/                        # ★ Electron 桌面壳（main.js 拉起 api 后端）
├── tauri/                           # ★ Tauri 桌面壳（Rust 工程，拉起 api 后端）
├── gui/  workers/                   # 遗留 PyQt6 GUI 与后台 worker（保留参考，交付不再使用）
├── build/                           # 打包脚本（PyInstaller spec 等，产物已 gitignore）
├── scripts/                         # 启动器 / 构建脚本（run_web.sh 为 Web/Electron 启动入口；*.sh + *.ps1 跨平台）
├── config/                          # 本地配置 JSON（*.local.json 已被 gitignore）
├── store/  logs/  cache/  merge_state/  sync_history/   # 运行时数据目录
├── tests/                           # pytest 单元测试 + 前端纯逻辑 .mjs 测试（npm test）
└── docs/                            # 专题文档（本文件、打包等）
```

> 说明：`frontend/web-react/dist`（React 构建产物）优先挂载于 `/web`；仅当 dist
> 不存在时回退到 `web/` 下的旧版静态文件。

**分层依赖方向（单向，避免环）：**

```
frontend(web-react)  →  api/*（HTTP/SSE）  →  core/*   （上层依赖下层）
gui/*（遗留 PyQt6）  →  api/* / core/*
tests/*              →  core/* / api/*
```

`core/` 内部也遵循「业务子域 → 通用基础」：
`client/`、`diff/`、`k8s/`、`kibana/`、`config/` 等子域 → 通用基础 `app_paths`/`constants`/`models`/`errors`/`safe`/`throttle`/`logger`/`cache`/`watchdog`/`log_retention`。

---

## 二、后端 `api/`（FastAPI）

按「业务域 + 角色」命名，便于定位：

| 模块 | 角色 | 说明 |
|------|------|------|
| `server.py` | 应用入口 | 创建 `app`、挂载所有 router、`_PROJECT_ROOT`/`_env_search_roots`、CORS |
| `common.py` | 共享层 | 日志、配置加载、CF/HCM 白名单、下载回调、通用 re-export |
| `schemas.py` | 数据契约 | Pydantic 请求/响应模型 |
| `routes_k8s.py` | K8s 路由聚合 | 仅 `include_router` 合并下列子模块，对外路径不变 |
| `routes_k8s_snapshot.py` | K8s 子路由 | 快照 / 报告 / 日志（含流式跟随 `GET /api/k8s/log`） |
| `routes_k8s_env.py` | K8s 子路由 | 环境管理 + Pod / YAML / 网络探测 |
| `routes_k8s_observe.py` | K8s 子路由 | events / describe / top / 时间参数归一化 |
| `routes_k8s_exec.py` | K8s 子路由 | 命令执行（`POST /api/k8s/exec`）+ 交互式 WebSocket 终端（降级实现） |
| `routes_k8s_files.py` | K8s 子路由 | 容器内文件操作（ls/read/upload/delete） |
| `routes_clash.py` | Clash 路由聚合 | 仅 `include_router` 合并下列子模块，对外路径不变 |
| `clash_base.py` | Clash 基础模块 | 常量 / 日志 / 底层工具函数 + Pydantic 模型（被 probe/rules/config 子模块共享）|
| `routes_clash_probe.py` | Clash 子路由 | 只读探测：接口/路由状态/连通性/代理端口 |
| `routes_clash_rules.py` | Clash 子路由 | 规则生成 / 一键应用 / 撤销 / 服务顺序修复 |
| `routes_clash_config.py` | Clash 子路由 | 诊断 / 配置路径探测 / 默认值 / 批量写入 |
| `routes_cf.py` | CF 云函数日志路由 | 调 `cf_core` |
| `routes_hcm.py` `hcm_core.py` | HCM 对象浏览器 | HCM 平台对象查询 |
| `routes_repos.py` | 仓库浏览路由 | status/connect/tree/file/commits/search |
| `routes_diff.py` | 差异对比路由 | 计算/对比/下载对比/批量合并（断点续传） |
| `routes_jira_issue.py` `routes_services_config.py` | 业务路由 | Jira issue / 服务配置页 |
| `routes_events.py` `routes_sync_history.py` `routes_settings.py` `routes_download.py` `routes_cache.py` | 各业务路由 | 事件(SSE)/同步历史/设置/下载/缓存 |
| `eventbus.py` | SSE 事件总线 | 跨模块跨线程广播；队列满按优先级丢弃（告警不被进度事件挤掉） |
| `unified_diagnose.py` `full_diagnose.py` `diagnosis_capabilities.py` | 统一诊断 | CF + K8s + dynamic_log + 元数据一键诊断与编排 |
| `cf/` | CF 云函数子包 | routes_cf + cf_tokens/cf_login/cf_logs/cf_diagnose/cf_stream（实时日志流） |
| `cfdebug/` | 云函数调试子包 | 本地 runner / DAP 断点桥 / mock 数据 / 错误定位 |
| `clash/` | Clash 子包 | probe（只读探测）/ rules（规则生成应用）/ config（诊断与写入） |
| `hcm/` | HCM 对象浏览器 | `routes_hcm.py` + `hcm_core.py`（HCM 平台对象查询） |
| `k8s/` | K8s 子包 | snapshot / env / observe / exec(WS 终端) / files / state |
| `kibana/` | Kibana 子包 | sites / logs / meta（ES 系统日志汇聚查询） |
| `cf_core.py` | CF 兼容层 | re-export，真实实现在 `cf/` 子包 |

> **约定**：`routes_*.py` 是「薄路由层」（解析参数、调 `core`/`cf_*`），重逻辑下沉到 `core/`。
> 阻塞操作（httpx / kubectl 子进程 / 大文件读写 / paramiko SSH）在 async 路由里
> 必须 `asyncio.to_thread` 下放，否则会冻结事件循环（SSE 断流、WebSocket 卡死）。

**K8s / Clash 路由的进一步拆分（聚合 + 子模块）**：原先 1000+ 行的单体
`routes_k8s.py` / `routes_clash.py` 已按业务子域拆成 `api/k8s/routes_k8s_<子域>.py` /
`api/clash/routes_clash_<子域>.py`，原文件退化为仅 `include_router` 的聚合壳，对外路由路径与
挂载点不变。子模块在需要保持旧 `import` 路径（测试直接引用）时，由聚合壳 re-export
顶层符号（如 `routes_k8s._k8s_normalize_time_arg`、`routes_clash._load_clash_defaults`）。

> 交互式 WebSocket 终端已是**真 PTY**：`core/k8s/exec_pty.py` 提供本地常驻 PTY
> 会话（ready/data/exit 协议）；SSH 远程环境走 `core/k8s/ssh_exec.py`（paramiko
> invoke_shell，同一协议），按环境复用连接。

---

## 三、核心层 `core/`（无 GUI 依赖）

按业务子域分目录，目录名即业务域：

### 3.1 客户端子域（`client/`）
| 模块 | 职责 |
|------|------|
| `__init__.py` | `JiraGitClient` 聚合组装（Mixin 拆分） |
| `connection.py` | httpx 请求封装（PAT/Cookie、网络看门狗挂钩） |
| `repos.py` | 仓库发现 / 列表解析（discover dump 覆盖写 + 历史清理） |
| `files.py` | 文件读取 / 目录树 |
| `browse.py` | Jira 页面浏览（Cookie 模式远端目录树） |
| `clone.py` | PAT 模式克隆 |
| `download.py` | 下载（并发 + 断点） |

### 3.2 K8s 子域（`k8s/`）
| 模块 | 职责 |
|------|------|
| `kubectl.py` | kubectl 执行封装（输出按 `MAX_OUTPUT_BYTES` 头尾截断） |
| `env.py` | 多环境 kubeconfig 管理（含 SSH 远程环境标记 `ssh://<env>`） |
| `pods.py` | Pod 列表 / YAML / 事件 / describe / top |
| `events.py` | 事件查询 |
| `yaml.py` | 资源 YAML 获取 / 清理 / 应用 |
| `exec.py` `exec_cmd.py` `exec_fs.py` | 容器内一次性执行 / 命令 / 文件浏览（自动 SSH 化） |
| `exec_pty.py` | 本地常驻 PTY 会话（交互终端） |
| `ssh_exec.py` | SSH 远程执行 + PTY（paramiko，按环境复用连接） |
| `netdetect.py` | 网络连通性探测 |
| `snapshot_fetch.py` `snapshot_render.py` | 快照抓取（tail 钳制）/ HTML 渲染 |

### 3.3 差异对比子域（`diff/`）
| 模块 | 职责 |
|------|------|
| `models.py` | DiffEntry 等数据模型 |
| `scan_local.py` `scan_remote.py` | 本地/远端扫描（远端带 repo_id/branch 快照防跨仓库混读 + TTL 缓存） |
| `diff_core.py` `normalize.py` | 计算 diff / 规范化（JSONC/空白） |
| `merge_file.py` `merge_entries.py` | 合并到本地 |
| `merge_manifest.py` | 断点续传 manifest（读写/冲突检测） |

### 3.4 Kibana / 配置 / 同步子域
| 模块 | 职责 |
|------|------|
| `kibana/client.py` `queries.py` | ES 查询客户端 / DSL 构建（pod/namespace 聚合） |
| `kibana/config.py` `servers.py` | 站点配置 / 服务器派生站点（`srv::` 前缀） |
| `config/connect.py` `cf.py` `hcm.py` `merge.py` `session.py` | 连接 / CF / HCM / 合并 / 会话配置 |
| `sync/store.py` `view.py` | 同步历史存储 / 视图（敏感信息脱敏） |

### 3.5 通用基础（`core/` 顶层）
| 模块 | 职责 |
|------|------|
| `app_paths.py` | 运行时可写目录（freeze 时迁到应用数据目录） |
| `constants.py` | 目录 / 代理 / 超时等常量 |
| `models.py` | ConnectConfig / RepoInfo / TreeEntry / DiffResult |
| `errors.py` | `UserError` 等异常 |
| `safe.py` | 安全工具（脱敏等） |
| `throttle.py` | 全局令牌桶限流（扫描 QPS 抬升/恢复在此之上实现） |
| `cache.py` | 通用缓存（TTL + evict） |
| `log_retention.py` | 日志/导出目录保留策略（discover dump / cf_logs 按数量+龄清理） |
| `logger.py` | 日志桥接 / `get_logger` |
| `watchdog.py` | 网络看门狗（显式传参，不再写全局单例） |

---

## 四、GUI 层 `gui/`（PyQt6，遗留）

> ⚠️ 当前交付形态是 **Electron / Tauri + React 前端**；本目录仅作参考保留，
> 日常开发请改 `frontend/web-react/` + `api/`。入口 `main.py`（`scripts/run.sh`）。

| 模块 | 职责 |
|------|------|
| `app.py` | `GUIApp`：应用装配 + 启动 |
| `main_window.py` | `MainWindow` 主窗口 |
| `repo_panel.py` | 仓库面板 |
| `k8s_panel.py` | K8s 面板（`K8sPanel` / `EnvManageDialog` / 后台任务） |
| `connect_dialog.py` | 连接配置对话框 |
| `styles.py` | QSS 主题构建 / 应用 |
| `log_dock.py` `log_table.py` | 日志停靠窗 / 日志表格 |
| `state.py` `events.py` `worker_bridge.py` | GUI 状态 / 事件 / worker 桥接 |
| `icons_rc.py` | 图标资源 |

---

## 五、其它

- **`workers/`**：`download_worker.py`（下载任务）、`sync_worker.py`（同步任务）—— 耗时操作后台化，避免阻塞 UI。
- **`tests/`**：按被测试对象命名（`test_client_*`、`test_diff_*`、`test_discover_repos` 等），`pytest` 运行。
- **`build/`**：PyInstaller 的 `.spec` 与 `run_backend.py`/`run_gui.py`；`pyinstaller_*/` 构建产物已被 `.gitignore` 忽略，不入库。
- **`scripts/`**：启动器与构建脚本（Shell + PowerShell 跨平台配对）。

## 六、命名约定（便于维护）

1. **路由文件**：`api/routes_<业务>.py` 或业务子包 `api/<业务>/routes_<业务>.py`，薄层，只做参数解析 + 调 `core`。
2. **业务子域**：`core/` 下按目录分域（`client/`、`diff/`、`k8s/`、`kibana/`、`config/`、`sync/`），文件名不带前缀。
3. **聚合兼容层**：把大模块拆小后，原文件保留为 `re-export` 壳（如 `api/cf/cf_core.py`），保证旧 `import` 路径不变。
4. **无 GUI 依赖**：`core/` 不得 import `gui/`；`api/` 不得 import `gui/`。
5. **i18n**：前端文案统一走 `i18n/` 字典（zh/en/ja 三份同步），`api/client.ts` 等非组件模块用纯函数 `t()`；`npm test` 里有 i18n 键完整性校验。
