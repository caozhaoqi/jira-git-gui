# CHANGELOG


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