#!/usr/bin/env bash
# 跨平台构建脚本的 macOS / Linux 薄包装：切到项目根目录后调用 Python 编排器。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY="${PYTHON:-python3}"
exec "$PY" build/build.py "$@"
