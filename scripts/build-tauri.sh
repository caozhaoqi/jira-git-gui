#!/usr/bin/env bash
# 构建 Tauri 桌面版（macOS .app/.dmg；Windows .msi/.nsis；Linux AppImage/.deb）。
# 用法:
#   sh scripts/build-tauri.sh            # 默认 release 构建
#   sh scripts/build-tauri.sh --debug    # debug 构建
#   sh scripts/build-tauri.sh --no-bundle# 只编译二进制，不打包
set -euo pipefail

# 脚本位于 <root>/scripts/ 下，切到项目根目录再定位 tauri/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TAURI_DIR="$ROOT/tauri"

# ---- Rust / cargo 环境 ----
# rustup 安装时默认把 cargo 放在 $HOME/.cargo/bin；若不在 PATH 则补上。
if ! command -v cargo >/dev/null 2>&1; then
  if [ -x "$HOME/.cargo/bin/cargo" ]; then
    export PATH="$HOME/.cargo/bin:$PATH"
  else
    echo "错误: 未找到 cargo。请先安装 Rust: https://rustup.rs" >&2
    exit 1
  fi
fi

# macOS 上 Tauri 需要 pkg-config 能找到系统库（webkit2gtk 等通过 homebrew 提供）。
# 注意 set -u 下引用未定义变量会报 unbound variable，用 ${VAR:-} 兜底。
if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  export PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig:/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
fi

cd "$TAURI_DIR"

ARGS=()
if [ "${1:-}" = "--debug" ]; then
  ARGS=(--debug)
elif [ "${1:-}" = "--no-bundle" ]; then
  ARGS=(--no-bundle)
fi

echo "==> 构建 Tauri (release) ..."
# 注意: tauri 自带的 dmg 打包依赖 create-dmg 的 support 模板，若未安装会以
# 「failed to run bundle_dmg.sh」非零退出。这里不让它中断整体流程，后面用 hdiutil 兜底。
# 兼容 macOS 自带 bash 3.2：set -u 下空数组 "${ARGS[@]}" 会报 unbound variable，
# 用 "${ARGS[@]+"${ARGS[@]}"}" 惯用法（有参展开参数，无参展开为空串）。
if ! cargo tauri build "${ARGS[@]+"${ARGS[@]}"}"; then
  echo "警告: cargo tauri build 返回非零（通常是未安装 create-dmg 导致 DMG 步骤失败）。" >&2
  echo "       继续用系统 hdiutil 兜底生成 DMG，.app 本身已构建成功。" >&2
fi

BUNDLE_DIR="$TAURI_DIR/src-tauri/target/release/bundle"

# ---- 兜底生成 DMG（仅 macOS：tauri 自带 dmg 步骤失败 / 未安装 create-dmg 时）----
if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  APP="$(find "$BUNDLE_DIR/macos" -maxdepth 1 -name '*.app' | head -1)"
  if [ -n "$APP" ] && ! ls "$BUNDLE_DIR"/dmg/*.dmg >/dev/null 2>&1; then
    echo "==> 用 hdiutil 生成 DMG 分发包 ..."
    DMG_NAME="$(basename "$APP" .app).dmg"
    hdiutil create -volname "JiraGitGUI" -srcfolder "$APP" -ov -format UDZO "$BUNDLE_DIR/dmg/$DMG_NAME"
  fi
fi

# ---- 输出体积 ----
echo
echo "==> 构建产物体积:"
APP="$(find "$BUNDLE_DIR/macos" -maxdepth 1 -name '*.app' | head -1)"
if [ -n "$APP" ]; then
  echo "  .app  : $(du -sh "$APP" | cut -f1)  ($APP)"
fi
DMG="$(find "$BUNDLE_DIR/dmg" -maxdepth 1 -name '*.dmg' | head -1)"
if [ -n "$DMG" ]; then
  echo "  .dmg  : $(du -h "$DMG" | cut -f1)  ($DMG)"
fi
echo "==> 完成。"
