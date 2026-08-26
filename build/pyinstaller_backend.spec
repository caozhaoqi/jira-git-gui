# -*- mode: python ; coding: utf-8 -*-
"""冻结后端（api.server）为单文件可执行：供 Web 模式与 Electron 模式复用。

产物：dist/jira-git-backend[.exe]
- Web 模式：用户直接运行该可执行，浏览器打开 http://127.0.0.1:8787
- Electron 模式：electron-builder 将其作为 extraResource 打进安装包，
  electron/main.js 优先 spawn 该可执行（见 electron/package.json 的 build 配置）

注意：后端已与 PyQt6 解耦（core/logger 懒加载），故 excludes 中剔除 GUI 框架，
使无头后端体积更小、跨平台编译更稳。

console=True：保留标准输出，使 Electron 主进程能通过 pipe 捕获后端日志，
同时 Web 模式直接在终端可见启动信息。
"""
import os
from pathlib import Path
import glob

SPEC_DIR = Path(os.path.dirname(os.path.abspath(SPEC)))
PROJ = SPEC_DIR.parent

a = Analysis(
    [str(PROJ / "build" / "run_backend.py")],
    pathex=[str(PROJ)],
    binaries=[],
    datas=[
        (str(PROJ / "web"), "web"),
    ],
    hiddenimports=[
        # 后端 / 核心包
        "core", "core.client", "core.config", "core.constants", "core.models",
        "core.differ", "core.cache", "core.logger", "core.errors", "core.safe",
        "core.sync_history", "core.throttle", "core.worker", "core.app_paths",
        "api", "api.server",
        # Web 框架（uvicorn 会按运行配置动态 import 这些子模块，需显式声明）
        "fastapi", "fastapi.applications", "fastapi.routing", "fastapi.responses",
        "starlette", "starlette.applications", "starlette.routing",
        "starlette.responses", "starlette.staticfiles", "starlette.exceptions",
        "starlette.datastructures",
        "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        "httpx", "pydantic", "pydantic_core", "email_validator",
    ],
    excludes=[
        "PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui", "PyQt6.QtNetwork",
        "PyQt5", "PySide2", "PySide6", "tkinter", "gi", "PyQt6_sip",
    ],
    noarchive=False,
)

# .env 通常被 gitignore，CI checkout 后不存在；仅当本机确有 .env 时才打进包，
# 避免 PyInstaller 因找不到源文件而报错退出（缺失 .env 时运行时回退到连接设置 UI）。
_env_file = PROJ / ".env"
if _env_file.exists():
    a.datas.append((str(_env_file), "."))

# 打包 config/ 下的示例模板（*.example.json）；含真实 IP 的 .local.json 绝不进包
_cfg_datas = [
    (f, "config")
    for f in glob.glob(str(PROJ / "config" / "*.json"))
    if not f.endswith(".local.json")
]
a.datas.extend(_cfg_datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jira-git-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
