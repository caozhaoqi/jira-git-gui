#!/usr/bin/env bash
# Electron / Web 版启动脚本（跨平台：macOS / Linux / Windows-Git-Bash）
# 用法：
#   ./scripts/run_web.sh              # 启动 API + Web 前端（自动打开浏览器）
#   ./scripts/run_web.sh --electron   # 启动 Electron 桌面应用
set -euo pipefail

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
else
    PYTHON="python"
fi

open_browser() {
    local url="$1"
    case "$(uname -s 2>/dev/null)" in
        Darwin*)  open "$url" 2>/dev/null & ;;
        Linux*)   xdg-open "$url" 2>/dev/null & ;;
        MINGW*|MSYS*|CYGWIN*) cmd //c start "$url" 2>/dev/null & ;;
        *)        echo "请手动打开: $url" ;;
    esac
}

if [ "${1:-}" = "--electron" ]; then
    # Electron 模式
    if [ ! -d "$ROOT/electron/node_modules" ]; then
        echo "首次运行，安装 Electron 依赖…"
        (cd "$ROOT/electron" && npm install)
    fi
    (cd "$ROOT/electron" && npx electron . "${@:2}")
else
    # Web 模式：启动 API 服务器，自动打开浏览器
    echo "启动 API 服务器…"
    echo "浏览器访问 http://127.0.0.1:8787"
    open_browser "http://127.0.0.1:8787"
    exec "$PYTHON" -m api.server "$@"
fi
