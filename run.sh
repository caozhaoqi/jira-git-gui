#!/usr/bin/env bash
# Jira Git 通用拉取工具 —— 一键启动（macOS / Linux）
# 优先使用项目自带 venv；若缺失则尝试 python3。main.py 自身也会做 venv 自愈。
set -e
cd "$(dirname "$0")"

if [ -x "./venv/bin/python" ]; then
    exec ./venv/bin/python main.py "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 main.py "$@"
else
    echo "未找到可用的 Python 解释器，请先安装 Python 3.10+ 或创建 venv。" >&2
    exit 1
fi
