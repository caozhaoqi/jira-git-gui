# CHANGELOG


## v0.0.7

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

## v0.0.6

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