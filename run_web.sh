#!/usr/bin/env bash
# Electron 版启动脚本（macOS / Linux）
# 用法：
#   ./run_web.sh              # 启动 API + Web 前端（浏览器打开）
#   ./run_web.sh --electron   # 启动 Electron 桌面应用
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="./venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

if [ "${1:-}" = "--electron" ]; then
    # Electron 模式
    if [ ! -d "./electron/node_modules" ]; then
        echo "首次运行，安装 Electron 依赖…"
        cd electron && npm install && cd ..
    fi
    cd electron && npx electron . "${@:2}"
else
    # Web 模式：启动 API 服务器，自动打开浏览器
    echo "启动 API 服务器…"
    echo "浏览器访问 http://127.0.0.1:8787"
    # macOS 自动打开浏览器
    if command -v open >/dev/null 2>&1; then
        (sleep 2 && open "http://127.0.0.1:8787") &
    elif command -v xdg-open >/dev/null 2>&1; then
        (sleep 2 && xdg-open "http://127.0.0.1:8787") &
    fi
    exec "$PYTHON" -m api.server "$@"
fi
