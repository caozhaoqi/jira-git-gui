"""运行时常量：目录、代理、超时。"""
import os
from pathlib import Path

from .app_paths import get_data_root

# 运行时数据根：开发态为项目根；冻结打包态为 ~/.jira-git-gui（见 core/app_paths）。
BASE_DIR = get_data_root()
STORE = BASE_DIR / "store"
REPOS_DIR = STORE / "repos"        # git clone 存放地： repos/<repoId>/
DOWNLOAD_DIR = STORE / "downloads"  # Cookie 模式批量下载存放地

for _d in (STORE, REPOS_DIR, DOWNLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)

HTTP_TIMEOUT = 40  # 单次 HTTP 请求超时（秒）
DEFAULT_REQUEST_QPS = 6  # 默认对外请求速率上限（每秒），保护 Jira 服务器不被打崩


def detect_proxy() -> str:
    """从环境变量探测代理地址（本地代理偶发断连，需要重试策略）。"""
    for _k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        _v = os.environ.get(_k)
        if _v:
            return _v
    return ""


PROXY_URL = detect_proxy()
