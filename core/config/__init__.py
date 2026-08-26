"""core.config 子包 —— 由拆分后的子模块聚合而成，对外符号保持兼容。"""

# ---- 以下来自原 core/config.py（聚合兼容层）----
# -*- coding: utf-8 -*-
"""配置加载（聚合兼容层）。

从项目根目录 .env 自动读取连接配置；兼容多种键名别名与拼写误差；
真实环境变量优先级高于 .env；会话信息持久化到数据根。

实现已拆分到子模块：config_connect / config_session / config_merge /
config_cf / config_hcm。本文件仅重导出，外部 `from core.config import X` 不变。
"""

from .connect import (
    build_config,
    load_config,
)

from .session import (
    save_session,
    load_session,
    clear_session,
    get_session_path,
)

from .merge import (
    load_merge_config,
)

from .cf import (
    load_cf_accounts,
    clear_cf_accounts_cache,
)

from .hcm import (
    load_hcm_whitelist,
)

__all__ = [
    "build_config",
    "load_config",
    "save_session",
    "load_session",
    "clear_session",
    "get_session_path",
    "load_merge_config",
    "load_cf_accounts",
    "clear_cf_accounts_cache",
    "load_hcm_whitelist",
]

