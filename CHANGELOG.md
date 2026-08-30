# CHANGELOG


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

- ⚠️ 已知遗留（未改）：`.env` `MERGE_REPO_*` 映射按仓库**名**建立，`/api/diff/repo-mappings` 与 `selectCompareRepo` 自动填本地目录都按 `display_name||name` 查表——同名仓库会撞 key，只能匹配到其中一个。若要让同名仓库各自自动定位本地目录，需把映射改为按 `repo_id` 索引（后端 `load_merge_config` + `/api/diff/repo-mappings` 都带 repo_id）。待用户决定。


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