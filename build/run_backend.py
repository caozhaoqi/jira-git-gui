"""PyInstaller 入口脚本：启动 FastAPI 后端（供 Web / Electron 两种形态使用）。

冻结成单文件可执行后：
- ``sys._MEIPASS`` 指向解压目录，``web/`` 与 ``.env`` 会被收集到该目录下；
- ``api.server`` 内的 ``_PROJECT_ROOT`` / ``_env_search_roots`` 已适配该布局。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.server import main

if __name__ == "__main__":
    main()
