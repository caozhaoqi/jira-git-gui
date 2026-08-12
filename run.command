#!/usr/bin/env bash
# Jira Git 通用拉取工具 —— macOS 双击启动脚本
# 双击此文件即可在终端里启动 GUI（macOS 会把 .command 当作可双击的脚本）。
cd "$(dirname "$0")"
exec ./run.sh
