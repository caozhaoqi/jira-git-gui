"""可写数据目录解析（开发模式 / 冻结打包 双模式）。

为什么需要：
- 开发模式下，日志 / 缓存 / 下载等运行时目录放在项目根下即可。
- 用 PyInstaller / electron-builder 冻结成可执行文件后，程序文件位于只读的
  应用包内（macOS 的 .app/Contents、Windows 的 _MEIPASS、Linux 的 AppImage
  挂载点），任何写操作都会失败。此时必须把运行时数据重定向到用户可写目录。

规则：
- ``sys.frozen`` 为真（冻结态）→ 使用 ``~/.jira-git-gui``（跨平台一致，用户主目录下）。
- 否则（开发态）→ 使用项目根目录，保持原有行为不变。
"""
import os
import sys
from pathlib import Path


def get_data_root() -> Path:
    """返回运行时可写数据根目录。

    优先级：
    1. 环境变量 ``JIRA_GIT_DATA_DIR``（Electron 壳可据此与后端统一目录）
    2. 冻结态（sys.frozen）→ ``~/.jira-git-gui``
    3. 开发态 → 项目根目录
    """
    env = os.environ.get("JIRA_GIT_DATA_DIR")
    if env:
        root = Path(env)
    elif getattr(sys, "frozen", False):
        root = Path.home() / ".jira-git-gui"
    else:
        root = Path(__file__).resolve().parent.parent
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 极端情况下（如 home 不可写）退而求其次用当前工作目录
        root = Path.cwd()
    return root
