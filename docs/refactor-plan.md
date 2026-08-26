# 重构方案：拆分超过 200 行的代码文件

> 目标：在不改变任何对外行为（HTTP 路由路径、CLI 入口、前端调用契约）的前提下，
> 将项目内所有超过 200 行的 Python 文件拆分为职责单一、更易维护的小模块。
> 重构完成后运行 `pytest` 验证导入与功能不受影响。

## 一、现状概览

项目已做过第一轮按业务拆分（`server.py` → `api/routes_*.py` + `core/*` 子模块）。
本轮针对剩余 **25 个 >200 行文件** 做二次细分。文件清单（行数）：

### api（路由 / 逻辑层）
| 文件 | 行数 | 主要区块 |
|------|------|----------|
| `api/routes_k8s.py` | 1059 | 时间校验 / 快照 / 日志 / 容器 / 环境 / YAML / Pods / 网络 / 事件 / describe |
| `api/routes_clash.py` | 977 | 操作日志 / 系统检测 / 路由查询 / 配置生成 / 应用 / 撤销 / 诊断 |
| `api/cf_core.py` | 589 | token 缓存 / 登录 / 验证码 / 日志查询 / 导出 / 剪贴板 |
| `api/routes_repos.py` | 341 | status / connect / repos / tree / file / search / commits |
| `api/routes_diff.py` | 322 | diff 计算 / 对比 / 下载对比 |
| `api/schemas.py` | 211 | Pydantic 请求/响应模型 |
| `api/common.py` | 201 | 共享单例 / 工具函数 |

### core（核心层）
| 文件 | 行数 | 主要区块 |
|------|------|----------|
| `core/client.py` | 1760 | JiraGitClient：http / connect / discover / tree / commits / file / clone / download |
| `core/k8s_pods.py` | 480 | kubectl 封装 / YAML / 事件 / describe / top / 网络检测 / 执行 |
| `core/diff_scan.py` | 390 | 本地扫描 / 远程扫描 / 缓存 |
| `core/k8s_exec.py` | 312 | kubectl 执行 / 容器内文件浏览 |
| `core/sync_history.py` | 288 | 同步历史记录 / 查询 / 统计 |
| `core/k8s_snapshot_fetch.py` | 264 | parse_pod / compute_age / classify / fetch_logs / run_snapshot |
| `core/diff_diff.py` | 246 | compute_diff / file_diff / 规范化 |
| `core/logger.py` | 210 | 日志桥接 / get_logger / 全局钩子 |
| `core/cache.py` | 208 | 缓存读写 / get_or_fetch / 失效 |
| `core/k8s_snapshot_render.py` | 205 | **重复** parse_pod / compute_age / classify / render_html |
| `core/diff_merge.py` | 205 | merge_to_local / merge_entries |

### gui（桌面层）
| 文件 | 行数 | 主要区块 |
|------|------|----------|
| `gui/main_window.py` | 679 | MainWindow 大窗口类 |
| `gui/k8s_panel.py` | 633 | 后台任务包装 / EnvManageDialog / K8sPanel |
| `gui/styles.py` | 395 | QSS 构建 / 主题应用 |
| `gui/connect_dialog.py` | 260 | ConnectDialog |
| `gui/repo_panel.py` | 207 | RepoPanel |

### tests / scripts / build
| 文件 | 行数 |
|------|------|
| `tests/test_discover_repos.py` | 378 |
| `tests/test_download_resume.py` | 321 |
| `tests/test_differ_perf.py` | 274 |
| `tests/test_client_optimizations.py` | 268 |
| `scripts/hcm_direct.py` | 451 |
| `build/build.py` | 277 |

---

## 二、拆分策略（通用原则）

1. **只拆不分家**：把大文件内的「区块」抽成同目录子模块，原文件保留为「聚合/转发」入口。
2. **对外契约不变**：HTTP 路由路径、函数签名、CLI 入口、导出的公共符号名全部保持不变。
3. **消除重复**：`k8s_snapshot_render.py` 与 `k8s_snapshot_fetch.py` 重复了 `parse_pod/compute_age/classify`，合并到 `core/k8s_models.py`。
4. **依赖方向单一**：子模块只依赖更底层模块 / `api.common`，不反向依赖路由层，避免循环导入。
5. **每个新文件 < 200 行**（个别含数据/常量文件可放宽）。

---

## 三、逐文件拆分方案

### A. api 层

#### `api/routes_k8s.py` (1059) → 拆为
- `api/k8s_routes_snapshot.py`：snapshot / cancel / report（含时间归一化 `_k8s_normalize_time_arg`）
- `api/k8s_routes_logs.py`：log / log/stream / pod-containers
- `api/k8s_routes_env.py`：env 列表/保存/切换/删除/导入/导出
- `api/k8s_routes_yaml.py`：yaml / pods / network / events / describe
- `api/routes_k8s.py` 改为：**仅保留 router 聚合 + 模块级状态（`_k8s_cancel` 等）**，各子模块 `include` 同一 `router`
  - 实现方式：子模块各自 `from api.routes_k8s import router`，在 `routes_k8s.py` 末尾 `import` 子模块触发路由注册。
  - 模块级可变状态（`_k8s_cancel`/`_k8s_running`/`_k8s_out_dir`/`_k8s_snap_meta`）保留在 `routes_k8s.py`，子模块通过 `from api.routes_k8s import ...` 读写。

#### `api/routes_clash.py` (977) → 拆为
- `api/clash_detect.py`：系统检测（`_parse_hardware_ports`/`_ifconfig`/`_default_gateway`/`_list_services`/`_host_routes`/`_classify` 等只读命令封装 + `_run`）
- `api/clash_route.py`：路由查询 / 路由命令构造（`_route_cmd_for`/`_route_get` 等）
- `api/clash_config.py`：配置生成 / 应用 / 撤销（YAML 改写、备份、回滚）
- `api/clash_diag.py`：诊断
- `api/routes_clash.py`：保留 `router` + 路由函数（从子模块 import 工具函数调用），或改为子模块各自挂 `@router`。
  - 推荐：子模块各自 `@router`，`routes_clash.py` 末尾 import 触发注册；操作日志 `_log`/`_LOG_PATH`/`_log_route_table` 抽到 `clash_detect.py` 或保留在入口。

#### `api/cf_core.py` (589) → 拆为
- `api/cf_tokens.py`：token 缓存（load/save/cache 字典 + 持久化）
- `api/cf_login.py`：账号登录 / autologin / refresh / captcha
- `api/cf_logs.py`：日志查询 / 导出 / 剪贴板 / mask
- `api/cf_core.py`：保留 `sniff_image_type`/`new_cf_client` 等通用工具 + 从子模块 re-export 公共函数（保持 `from api.cf_core import cf_query_logs` 等旧调用点可用）。

#### `api/routes_repos.py` (341) → 拆为
- `api/repos_routes_browse.py`：status / connect / repos / tree / file / search
- `api/repos_routes_commits.py`：commits
- 或仅把 `api_search` 内的大函数（HTML/REST 分页解析）抽到 `api/repos_search.py`。
- `routes_repos.py` 保持 router 聚合。

#### `api/routes_diff.py` (322) → 拆为
- `api/diff_routes_compare.py`：对比计算路由
- `api/diff_routes_download.py`：下载对比路由
- 若内部依赖 `core.diff_*` 较重，优先下沉到 core，路由层只做适配（当前 core 已有 diff_diff/diff_merge/diff_scan，路由层应薄）。

#### `api/schemas.py` (211) → 拆为
- `api/schemas_k8s.py`：K8s* 请求模型
- `api/schemas_cf.py`：CF 请求模型
- `api/schemas.py`：保留通用模型 + re-export。
  - 注意：`api/routes_k8s.py` 用 `from api.schemas import K8sSnapshotReq, ...` 批量导入，拆后需更新导入或在 `schemas.py` 内 re-export。

#### `api/common.py` (201) → 基本已合理，仅微调
- 把 `client` 回调（`log_callback`/`progress_callback`/`make_should_cancel`）与 `commit_to_dict` 等「与下载任务相关」的工具抽到 `api/download_tasks.py`，`common.py` re-export，降低入口耦合。

### B. core 层

#### `core/client.py` (1760) → 拆为（核心重点）
`JiraGitClient` 类职责极重，按方法族拆到 mixin/子模块：
- `core/client_http.py`：http_get / cookie_headers / encode_pat / b64_prefix_account / host_of（HTTP 基元 + 鉴权）
- `core/client_repos.py`：connect / discover_repos / list_level / get_commits / get_local_commits
- `core/client_files.py`：get_file / get_file_at_commit / clone_repo
- `core/client_download.py`：download / download_repo
- `core/client.py`：`class JiraGitClient` 改为**组合式**——保留 `__init__`/`set_config` 等，把方法通过 `from core.client_*` import` 绑定，或直接让 `JiraGitClient` 继承多个 Mixin 基类（`ClientHTTPMixin`/`ClientReposMixin`/...）。
  - 推荐 Mixin 方案：`class JiraGitClient(ClientHTTPMixin, ClientReposMixin, ClientFilesMixin, ClientDownloadMixin)`，每个 Mixin 在 `core/client_*.py` 定义，构造函数统一。
  - `NetworkWatchdog`/`DEFAULT_DOWNLOAD_WORKERS` 保留在 `client.py` 或 `client_http.py`。

#### `core/k8s_pods.py` (480) → 拆为
- `core/k8s_yaml.py`：list_pods / clean_manifest_obj / get_resource_yaml / apply_yaml_content
- `core/k8s_events.py`：list_events / describe_resource / get_top
- `core/k8s_netdetect.py`：detect_network / _api_server_host / _tcp_probe 等
- `core/k8s_pods.py`：保留 kubectl 基础封装（`_env_kubectl_prefix`/`run_kubectl_env`）+ 执行部分，或仅作聚合。
  - 子模块共享 `run_kubectl_env`，放在 `k8s_pods.py` 由子模块 import。

#### `core/diff_scan.py` (390) → 拆为
- `core/diff_scan_local.py`：scan_local / scan_local_cached / 哈希工具
- `core/diff_scan_remote.py`：scan_remote / scan_remote_parallel / get_file_cached
- `core/diff_scan.py`：re-export。

#### `core/k8s_exec.py` (312) → 拆为
- `core/k8s_exec_cmd.py`：exec_command / _build_exec_script / _split_pwd
- `core/k8s_exec_fs.py`：list_dir / read_file / write_file / delete_path / mkdir_path / _parse_ls / _file_size_bytes
- `core/k8s_exec.py`：kubectl 基础 + re-export。

#### `core/sync_history.py` (288) → 拆为
- `core/sync_history_store.py`：_ensure_dir / _history_file / _desensitize / record / list_history / show / clear
- `core/sync_history_view.py`：stats / format_log
- 体量不大，可仅拆 `stats`/`format_log` 到 view，或整体保留（288 行，接近阈值，建议轻拆）。

#### `core/k8s_snapshot_fetch.py` (264) + `core/k8s_snapshot_render.py` (205) → **合并去重**
- 新建 `core/k8s_models.py`：提取共用的 `parse_pod`/`compute_age`/`classify`（两文件完全相同，消除重复）。
- `core/k8s_snapshot_fetch.py`：保留 fetch_logs / run_snapshot，`from core.k8s_models import parse_pod, compute_age, classify`。
- `core/k8s_snapshot_render.py`：保留 render_html，同样 import 共用函数。
- 调用方（`api/routes_k8s.py` 等）导入路径不变（仍从原文件 import 顶层符号）。

#### `core/diff_diff.py` (246) → 拆为
- `core/diff_diff_core.py`：compute_diff / file_diff / _is_same_normalized / is_whitespace_only_diff
- `core/diff_normalize.py`：_strip_jsonc_comments / canonical_text

#### `core/logger.py` (210) → 拆为
- `core/logger_bridge.py`：LogBridge / QtLogHandler / set_get_log_bridge
- `core/logger_core.py`：get_logger / install_global_hooks / _show_fatal
- 或保留整体（结构清晰，仅超 10 行，可选轻拆）。

#### `core/cache.py` (208) → 拆为
- `core/cache_store.py`：_get_lock / _cache_path / get / set / invalidate / clear_all
- `core/cache_fetch.py`：get_or_fetch / cache_info
- 或保留整体。

#### `core/diff_merge.py` (205) → 拆为
- `core/diff_merge_file.py`：_force_writable / merge_to_local / _write_file / merge_to_local_bytes
- `core/diff_merge_entries.py`：merge_entries

### C. gui 层

#### `gui/main_window.py` (679) → 拆为
- `gui/main_window_menus.py`：菜单栏 / 工具栏构建
- `gui/main_window_panels.py`：各子面板装配（repo / k8s / diff 等）
- `gui/main_window.py`：`class MainWindow` 保留骨架（__init__ 装配子组件），方法下沉到上述模块或 Mixin。
  - 注意 MainWindow 是 QMainWindow 子类，方法需绑定 self，建议用 Mixin：`class MainWindow(MenuMixin, PanelsMixin, QMainWindow)`。

#### `gui/k8s_panel.py` (633) → 拆为
- `gui/k8s_env_dialog.py`：`EnvManageDialog`
- `gui/k8s_panel_main.py`：`K8sPanel`（主面板）
- `gui/k8s_tasks.py`：yaml_get_task / yaml_apply_task / net_task 等后台任务包装
- `gui/k8s_panel.py`：re-export。

#### `gui/styles.py` (395) → 拆为
- `gui/styles_qss.py`：`_build_qss`（主题常量 + QSS 拼接）
- `gui/styles_apply.py`：`apply_global_style`
- `gui/styles.py`：re-export。

#### `gui/connect_dialog.py` (260) / `gui/repo_panel.py` (207) →
- 体量适中，仅略超 200 行。若内部方法可分组（如 connect_dialog 的校验/UI 构建），可轻拆；否则保持原样（优先处理 >400 行文件）。

### D. tests / scripts / build

> 测试文件按 TestCase 类拆分，属于「整理」范畴，风险低。

- `tests/test_discover_repos.py` (378) → 拆为 `test_discover_repos_parse.py`（NormalizeAndParse/HtmlPagination/RestPagination）、`test_discover_repos_merge.py`（DiscoverMerge/RestUnavailableCache）
- `tests/test_download_resume.py` (321) → 拆为 `test_download_manifest.py`、`test_download_files.py`、`test_download_binary.py`
- `tests/test_differ_perf.py` (274) → 拆为 `test_differ_perf_scan.py`、`test_differ_perf_diff.py`、`test_differ_perf_merge.py`
- `tests/test_client_optimizations.py` (268) → 拆为 `test_client_binary.py`、`test_client_cache.py`、`test_client_parallel.py`
- `scripts/hcm_direct.py` (451) → 拆为 `scripts/hcm_crypto.py`（AES/SM3/加解密）、`scripts/hcm_api.py`（hcm_call/object_list/model_meta）、`scripts/hcm_cli.py`（load_config/main）
- `build/build.py` (277) → 已按函数分组清晰，仅略超。可拆 `build_gui`/`build_backend`/`build_electron`/`build_tauri` 到 `build/build_targets.py`，`build.py` 保留 `main` 编排；或保持原样。

---

## 四、执行顺序（建议）

1. **低风险先行**：core 纯函数模块（k8s_models 合并去重、diff/cache/logger 轻拆）。
2. **核心大文件**：`core/client.py` 用 Mixin 拆；`core/k8s_pods.py`/`k8s_exec.py` 拆。
3. **api 路由**：`routes_k8s.py` → `routes_clash.py` → `cf_core.py` → `routes_repos/diff`。
4. **gui**：`main_window.py` → `k8s_panel.py` → `styles.py`。
5. **tests/scripts/build**：最后整理。
6. **每步结束跑 `pytest`**（或至少 `python -c "import api.server"` 验证可导入、路由可注册）。

## 五、风险与验证

- **循环导入**：子模块不得反向 import 路由层；共享状态（如 k8s 任务标志）保留在入口模块，子模块 `from api.routes_k8s import router, _k8s_cancel`。
- **re-export 兼容**：所有被外部 import 的顶层符号（函数/类/常量）在原文件保留 `from .sub import xxx` 转发，保证 `api/server.py`、`gui/*` 等调用点零改动。
- **验证命令**：
  ```bash
  python -m pytest -q            # 功能回归
  python -c "import api.server"  # 路由注册/导入冒烟
  ```
