#!/usr/bin/env bash
# 跨平台构建脚本薄包装（macOS / Linux / Windows-Git-Bash）：
# 切到项目根目录后调用 Python 编排器 build/build.py。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
exec "$PY" build/build.py "$@"
