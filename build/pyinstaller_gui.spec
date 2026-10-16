# -*- mode: python ; coding: utf-8 -*-
"""冻结 PyQt6 桌面版（main.py）为原生应用。

产物（onedir）：
- macOS  : dist/JiraGitGUI.app
- Windows: dist/JiraGitGUI/JiraGitGUI.exe
- Linux  : dist/JiraGitGUI/JiraGitGUI（可再包 AppImage）

跨平台说明：PyInstaller 不支持交叉编译，必须在目标系统上构建，或用 CI
（见 .github/workflows/release.yml 的 build-gui-* 任务）按 OS 出包。

macOS 上会额外生成 .app（BUNDLE 仅在 darwin 生效；其他平台忽略）。
"""
import os
import sys
from pathlib import Path

SPEC_DIR = Path(os.path.dirname(os.path.abspath(SPEC)))
PROJ = SPEC_DIR.parent

a = Analysis(
    [str(PROJ / "main.py")],
    pathex=[str(PROJ)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "core", "core.client", "core.config", "core.constants", "core.models",
        "core.differ", "core.cache", "core.logger", "core.errors", "core.safe",
        "core.sync_history", "core.throttle", "core.worker", "core.app_paths",
        "gui", "gui.main_window", "gui.commit_panel", "gui.connect_dialog",
        "gui.log_panel", "gui.preview_panel", "gui.repo_panel", "gui.tree_panel",
        "gui.highlighter", "gui.styles",
    ],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="JiraGitGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,   # 与后端 spec 同理：strip/UPX 改写 PE 是「加载 DLL 失败」的已知诱因，
    upx=False,     # 且 PyQt6 的 Qt DLL 被 UPX 压缩后更易出加载问题，全部关闭。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,   # 同 EXE：关闭 UPX，避免 Qt/Python DLL 加载失败与构建不可复现
    upx_exclude=[],
    name="JiraGitGUI",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="JiraGitGUI.app",
        icon=None,
        bundle_identifier="com.jiragitgui.app",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleName": "JiraGitGUI",
            "CFBundleDisplayName": "Jira Git 通用拉取工具",
        },
    )
