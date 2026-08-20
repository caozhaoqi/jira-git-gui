#!/usr/bin/env bash
# Jira Git 通用拉取工具 —— 一键启动（跨平台：macOS / Linux / Windows-Git-Bash）
# 优先使用项目自带 venv；若缺失则按 python3 / py -3 / python 顺序回退。
# main.py 自身也会做 venv 自愈。
set -e

# 脚本位于 <root>/scripts/ 下，切到项目根目录再干活
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if [ -x "$ROOT/venv/bin/python" ]; then
    PYTHON="$ROOT/venv/bin/python"
elif [ -x "$ROOT/venv/Scripts/python.exe" ]; then
    PYTHON="$ROOT/venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v py >/dev/null 2>&1; then
    PYTHON="py -3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
else
    echo "未找到可用的 Python 解释器，请先安装 Python 3.10+ 或创建 venv。" >&2
    exit 1
fi

exec "$PYTHON" main.py "$@"
