# 项目架构与模块地图

> 本文聚焦「代码组织 / 各模块职责 / 依赖方向」，功能介绍见 [`README.md`](../README.md)。
> 目标：让任何新接手的人能在一页内看懂 *jira-git-gui* 是如何分层与按业务域拆分的。

## 一、总览

```
jira-git-gui/
├── backend/                         # ★ 后端核心（Python）
│   ├── main.py                      #   桌面 GUI 入口（PyQt6）—— 创建 MainWindow 并启动事件循环
│   ├── server.py                    #   ⚠️ 遗留单体后端（旧版，已由 api/ 取代，勿再修改）
│   ├── run_merge.py                 #   CLI：把远端仓库最新代码合并到本地（缓存优先 + 同步历史）
│   ├── api/                         #   后端（FastAPI）—— 对外 HTTP / WebSocket 契约，按业务域分子路由
│   ├── core/                        #   核心逻辑层（无 GUI 依赖，按业务域：k8s / diff / config / cf / hcm）
│   ├── workers/                     #   后台 worker（下载 / 同步等耗时任务）
│   └── tools/                       #   独立小工具（如 k8s YAML 清洗演示）
├── desktop/                         # ★ 桌面 GUI（PyQt6）—— 当前主交付形态
│   └── gui/                         #   界面层（main_window / k8s_panel / styles 等）
├── frontend/                        # ★ Web 前端（React，编译产物由 api 挂载 /web 提供）
│   ├── web-react/                   #   活跃前端源码（vite + React + TS）
│   └── web-legacy/                  #   旧版前端（原生 JS/CSS，归档）
├── electron/                        # ★ Electron 桌面壳（独立平台工程，main.js 拉起 api 后端）
├── tauri/                           # ★ Tauri 桌面壳（Rust 工程，src-tauri 拉起 api 后端）
├── build/                           # 打包脚本（PyInstaller spec 等，产物已 gitignore）
├── scripts/                         # 启动器 / 构建脚本（*.sh + *.ps1 跨平台）
├── config/                          # 本地配置 JSON（*.local.json 已被 gitignore）
├── tests/                           # pytest 单元测试
└── docs/                            # 专题文档（本文件、HCM、Tauri 迁移、打包等）
```

> 说明：`web/`（构建产物）保留在仓库根，由 `api/server.py` 经 `app.mount("/web", ...)`
> 直接托管，路径约定不可移动；`web-react/dist` 构建后挂载于 `frontend/web-react/dist`。

**分层依赖方向（单向，避免环）：**

```
gui/*  →  api/*  →  core/*   （上层依赖下层）
workers/*  →  core/* / api/*
tests/*  →  core/* / api/*
```

`core/` 内部也遵循「业务子域 → 通用基础」：
`k8s_*`、`diff_*`、`config_*` 等子域模块 → 通用基础 `app_paths`/`constants`/`models`/`errors`/`safe`/`throttle`/`logger`/`cache`。

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
| `routes_diff.py` | 差异对比路由 | 计算/对比/下载对比 |
| `routes_events.py` `routes_sync_history.py` `routes_settings.py` `routes_download.py` `routes_cache.py` | 各业务路由 | 事件/同步历史/设置/下载/缓存 |
| `cf_core.py` | CF 逻辑聚合层 | re-export，真实实现在 `cf_tokens`/`cf_login`/`cf_logs` |
| `cf_tokens.py` | CF：token 缓存 + 验证码 | 共享状态 `_CF_TOKEN_CACHE` |
| `cf_login.py` | CF：登录 / 自动登录 / 刷新 | |
| `cf_logs.py` | CF：日志查询 / 导出 / 剪贴板 / 掩码 | |

> **约定**：`routes_*.py` 是「薄路由层」（解析参数、调 `core`/`cf_*`），重逻辑下沉到 `core/`。
> `cf_core.py` 仅做向后兼容的 re-export，业务逻辑已拆到 `cf_tokens/cf_login/cf_logs`。

**K8s / Clash 路由的进一步拆分（聚合 + 子模块）**：原先 1000+ 行的单体
`routes_k8s.py` / `routes_clash.py` 已按业务子域拆成 `routes_k8s_<子域>.py` /
`routes_clash_<子域>.py`，原文件退化为仅 `include_router` 的聚合壳，对外路由路径与
挂载点不变。子模块在需要保持旧 `import` 路径（测试直接引用）时，由聚合壳 re-export
顶层符号（如 `routes_k8s._k8s_normalize_time_arg`、`routes_clash._load_clash_defaults`）。

> 注：`routes_k8s_exec.py` 的交互式 WebSocket 终端当前为**降级实现**——core 暂未提供
> 常驻 PTY 能力，因此对前端每次发来的命令帧做一次性的 `exec_command` 并返回输出帧，
> 复用 `ready`/`data`/`exit` 协议。真正的 PTY 行编辑 / resize 需在 `core.k8s_exec` 补充
> 常驻 PTY 能力后再启用。

---

## 三、核心层 `core/`（无 GUI 依赖）

按子域分组，文件前缀即业务域：

### 3.1 K8s 运维子域（`k8s_*`）
| 模块 | 职责 |
|------|------|
| `k8s_kubectl.py` | kubectl 二进制定位（Homebrew/Docker/PATH 回退） |
| `k8s_manager.py` | 环境管理入口 / 集群句柄 |
| `k8s_env.py` | 多环境（dev/test/prod）kubeconfig 管理 |
| `k8s_pods.py` | Pod 列表 / YAML / 事件 / describe / top / 网络检测 |
| `k8s_exec.py` | 容器内执行 / 文件浏览器 |
| `k8s_snapshot.py` | 快照编排（调度 Pod 抓取） |
| `k8s_snapshot_fetch.py` | 抓取 Pod 日志 / 运行快照 |
| `k8s_snapshot_render.py` | 快照 HTML 渲染 |

### 3.2 差异对比子域（`diff_*`）
| 模块 | 职责 |
|------|------|
| `diff_models.py` | Diff 数据模型（DiffEntry 等） |
| `diff_scan.py` | 本地/远程文件扫描 + 缓存 |
| `diff_diff.py` | 计算 diff / 规范化（JSONC/空白） |
| `diff_merge.py` | 合并到本地 / merge_entries |

### 3.3 配置子域（`config_*`）
| 模块 | 职责 |
|------|------|
| `config.py` | 默认连接配置（从 .env 加载） |
| `config_connect.py` | 连接配置 |
| `config_cf.py` `config_hcm.py` | CF / HCM 平台配置 |
| `config_merge.py` | 合并配置 |
| `config_session.py` | 会话级配置 |

### 3.4 通用基础（无业务前缀）
| 模块 | 职责 |
|------|------|
| `app_paths.py` | 运行时可写目录（freeze 时迁到 `~/.jira-git-gui`） |
| `constants.py` | 目录 / 代理 / 超时等常量 |
| `models.py` | ConnectConfig / RepoInfo / TreeEntry / DiffResult |
| `errors.py` | `UserError` 等异常 |
| `safe.py` | 安全工具（脱敏等） |
| `throttle.py` | 全局令牌桶限流 |
| `cache.py` | 通用缓存 |
| `logger.py` | 日志桥接 / `get_logger` |
| `watchdog.py` | 网络看门狗 |
| `client.py` | `JiraGitClient`：Jira/Git 核心客户端（http/repos/files/download） |
| `differ.py` | 差异引擎高层封装 |
| `sync_history.py` | 同步历史记录 |

---

## 四、GUI 层 `gui/`（PyQt6）

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

1. **路由文件**：`routes_<业务>.py`，薄层，只做参数解析 + 调 `core`/`cf_*`。
2. **业务子域**：`k8s_<职责>.py`、`diff_<职责>.py`、`config_<场景>.py`。
3. **聚合兼容层**：把大模块拆小后，原文件保留为 `re-export` 壳（如 `cf_core.py`），保证旧 `import` 路径不变。
4. **无 GUI 依赖**：`core/` 不得 import `gui/`；`api/` 不得 import `gui/`。
