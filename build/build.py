#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jira-git-gui 跨平台本地构建编排脚本。

PyInstaller / electron-builder 均不支持交叉编译，因此本脚本在「当前所在的操作系统」
上构建「该平台」的产物：在 macOS 上出 .app / dmg，Windows 上出 .exe / nsis，
Linux 上出可执行 / AppImage+deb。要三端产物请用 CI（.github/workflows/release.yml）。

用法：
    python build/build.py                 # 等价于 --flavor all（本机支持的全部形态）
    python build/build.py --flavor gui     # PyQt6 桌面版
    python build/build.py --flavor backend # 单文件后端（Web 版）
    python build/build.py --flavor electron# Electron 桌面版（会先内置冻结后端）
    python build/build.py --flavor all
    python build/build.py --list           # 列出本机可构建的形态
    python build/build.py --no-deps        # 跳过依赖自动安装（假定已装好）

退出码：0=成功，非 0=有形态构建失败。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
ELECTRON_DIR = ROOT / "electron"
TAURI_DIR = ROOT / "tauri"
VENV = ROOT / "venv"

# 各形态构建成功后产出的目录 / 文件（相对 ROOT）提示
ARTIFACT_HINTS = {
    "gui": "dist/JiraGitGUI*",
    "backend": "dist/jira-git-backend*",
    "electron": "electron/dist-electron/*",
    "tauri": "tauri/src-tauri/target/release/bundle/*",
}


def detect_os() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "windows":
        return "windows"
    if s.startswith("linux"):
        return "linux"
    return s


def venv_python() -> Path:
    """返回应使用的 python：优先项目 venv，否则当前解释器。"""
    if VENV.exists():
        if os.name == "nt":
            p = VENV / "Scripts" / "python.exe"
        else:
            p = VENV / "bin" / "python"
        if p.exists():
            return p
    return Path(sys.executable)


def run(cmd, cwd: Path = ROOT, check: bool = True, quiet: bool = False, env=None):
    printable = " ".join(str(c) for c in cmd)
    if not quiet:
        print(f"  $ {printable}")
    res = subprocess.run(cmd, cwd=str(cwd), check=False, env=env)
    if check and res.returncode != 0:
        raise RuntimeError(f"命令失败 (exit={res.returncode}): {printable}")
    return res


def py_has(venv_py: Path, module: str) -> bool:
    r = subprocess.run(
        [str(venv_py), "-c", f"import {module}"],
        cwd=str(ROOT), check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def ensure_module(venv_py: Path, import_name: str, pip_name: str, no_deps: bool) -> bool:
    """确保某 python 模块可用；缺失时尝试安装。返回该模块最终是否可用。"""
    if py_has(venv_py, import_name):
        return True
    if no_deps:
        print(f"  ⚠️ 缺少 {import_name}（--no-deps 已禁用自动安装）。请先："
              f"{venv_py} -m pip install {pip_name}")
        return False
    print(f"  ↳ 安装缺失依赖：{pip_name}")
    run([str(venv_py), "-m", "pip", "install", pip_name])
    return py_has(venv_py, import_name)


def backend_exe_name(os_name: str) -> str:
    return "jira-git-backend.exe" if os_name == "windows" else "jira-git-backend"


def build_gui(venv_py: Path, os_name: str, no_deps: bool) -> bool:
    print("\n=== 构建 PyQt6 桌面版 (gui) ===")
    if not ensure_module(venv_py, "PyQt6", "PyQt6>=6.6", no_deps):
        print("  ✗ 跳过 gui（PyQt6 不可用）")
        return False
    if not ensure_module(venv_py, "PyInstaller", "pyinstaller", no_deps):
        print("  ✗ 跳过 gui（PyInstaller 不可用）")
        return False
    run([str(venv_py), "-m", "PyInstaller",
         str(BUILD_DIR / "pyinstaller_gui.spec"), "--noconfirm"])
    return True


def build_backend(venv_py: Path, os_name: str, no_deps: bool) -> bool:
    print("\n=== 构建单文件后端 (backend) ===")
    for mod, pkg in [("fastapi", "fastapi>=0.110"),
                     ("uvicorn", "uvicorn>=0.29"),
                     ("httpx", "httpx>=0.27")]:
        if not ensure_module(venv_py, mod, pkg, no_deps):
            print(f"  ✗ 跳过 backend（缺少 {mod}）")
            return False
    if not ensure_module(venv_py, "PyInstaller", "pyinstaller", no_deps):
        print("  ✗ 跳过 backend（PyInstaller 不可用）")
        return False
    run([str(venv_py), "-m", "PyInstaller",
         str(BUILD_DIR / "pyinstaller_backend.spec"), "--noconfirm"])
    return True


def copy_backend_to_electron(os_name: str) -> Path | None:
    src = ROOT / "dist" / backend_exe_name(os_name)
    if not src.exists():
        return None
    dst_dir = ELECTRON_DIR / "resources" / "backend"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copyfile(src, dst)
    dst.chmod(0o755)
    print(f"  ✓ 已内置后端到 Electron 资源：{dst.relative_to(ROOT)}")
    return dst


def build_electron(venv_py: Path, os_name: str, no_deps: bool) -> bool:
    print("\n=== 构建 Electron 桌面版 (electron) ===")
    if shutil.which("npm") is None:
        print("  ✗ 未找到 npm，请先安装 Node.js (https://nodejs.org)")
        return False
    # electron 需要内置冻结后的后端
    if not (ROOT / "dist" / backend_exe_name(os_name)).exists():
        print("  ↳ 后端尚未构建，先构建 backend ...")
        if not build_backend(venv_py, os_name, no_deps):
            return False
    if copy_backend_to_electron(os_name) is None:
        print("  ✗ 后端可执行不存在，无法嵌入 Electron")
        return False
    if not no_deps:
        if not (ELECTRON_DIR / "node_modules").exists():
            print("  ↳ npm install（首次）")
            run(["npm", "install"], cwd=ELECTRON_DIR)
        else:
            print("  ↳ node_modules 已存在，跳过 npm install（用 --no-deps 也跳过）")
    dist_map = {"macos": "dist:mac", "windows": "dist:win", "linux": "dist:linux"}
    # 国内镜像加速 Electron 下载；本地构建跳过代码签名
    env = os.environ.copy()
    env.setdefault("ELECTRON_MIRROR", "https://npmmirror.com/mirrors/electron/")
    env.setdefault("CSC_IDENTITY_AUTO_DISCOVERY", "false")
    # macOS 用 --dir 跳过 DMG（sandbox 限制 hdiutil），仅打包 .app
    npm_cmd = ["npm", "run", dist_map[os_name], "--", "--dir"]
    run(npm_cmd, cwd=ELECTRON_DIR, env=env)
    return True


def copy_backend_to_tauri(os_name: str) -> Path | None:
    src = ROOT / "dist" / backend_exe_name(os_name)
    if not src.exists():
        return None
    dst_dir = TAURI_DIR / "src-tauri" / "resources" / "backend"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copyfile(src, dst)
    dst.chmod(0o755)
    print(f"  ✓ 已内置后端到 Tauri 资源：{dst.relative_to(ROOT)}")
    return dst


def build_tauri(venv_py: Path, os_name: str, no_deps: bool) -> bool:
    print("\n=== 构建 Tauri 桌面版 (tauri) ===")
    if shutil.which("cargo") is None:
        print("  ✗ 未找到 Rust，请先安装 rustup (https://rustup.rs)")
        return False
    # Tauri 需要内置冻结后的后端
    if not (ROOT / "dist" / backend_exe_name(os_name)).exists():
        print("  ↳ 后端尚未构建，先构建 backend ...")
        if not build_backend(venv_py, os_name, no_deps):
            return False
    if copy_backend_to_tauri(os_name) is None:
        print("  ✗ 后端可执行不存在，无法嵌入 Tauri")
        return False
    # 同步前端文件到 tauri/web/（Tauri 编译需要 frontendDist）
    web_src = ROOT / "web"
    web_dst = TAURI_DIR / "web"
    if web_dst.exists():
        shutil.rmtree(web_dst)
    shutil.copytree(web_src, web_dst)
    print(f"  ✓ 已同步前端文件到 {web_dst.relative_to(ROOT)}")
    # Tauri 构建（跳过 DMG，sandbox 环境限制 hdiutil）
    run(["cargo", "tauri", "build", "--bundles", "app"], cwd=TAURI_DIR)
    return True


FLAVORS = {
    "gui": build_gui,
    "backend": build_backend,
    "electron": build_electron,
    "tauri": build_tauri,
}


def list_flavors(os_name: str):
    print(f"当前平台：{os_name}")
    print("可构建形态：")
    print("  gui     - PyQt6 桌面版 (.app / .exe / 可执行)")
    print("  backend - 单文件后端 (Web 版)")
    print("  electron- Electron 桌面版 (dmg / nsis / AppImage+deb)，需先有后端")
    print("  tauri   - Tauri 桌面版 (dmg / msi / AppImage+deb)，需先有后端")
    print("\n示例：")
    print("  python build/build.py --flavor all")


def main():
    ap = argparse.ArgumentParser(description="jira-git-gui 跨平台本地构建")
    ap.add_argument("--flavor", choices=["gui", "backend", "electron", "tauri", "all"],
                    default="all", help="要构建的形态（默认 all）")
    ap.add_argument("--list", action="store_true", help="仅列出本机可构建形态")
    ap.add_argument("--no-deps", action="store_true",
                    help="跳过依赖自动安装（假定已装好 requirements.txt + pyinstaller）")
    args = ap.parse_args()

    os_name = detect_os()
    if args.list:
        list_flavors(os_name)
        return

    print(f"平台识别：{os_name}  |  venv python：{venv_python()}")

    if args.flavor == "all":
        targets = ["gui", "backend", "electron", "tauri"]
    else:
        targets = [args.flavor]

    ok = True
    built = []
    for f in targets:
        fn = FLAVORS[f]
        try:
            if fn(venv_python(), os_name, args.no_deps):
                built.append(f)
        except RuntimeError as e:
            print(f"  ✗ {f} 构建失败：{e}")
            ok = False

    print("\n========== 构建结果 ==========")
    for f in targets:
        mark = "✓" if f in built else "✗"
        print(f"  {mark} {f:8s} -> {ARTIFACT_HINTS[f]}")
    if built:
        print("\n产物目录：")
        for f in built:
            print(f"  {ARTIFACT_HINTS[f]}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
