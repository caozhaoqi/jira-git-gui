# -*- coding: utf-8 -*-
"""pytest 全局夹具。

## 为什么必须把数据目录重定向到临时目录

``core.app_paths.get_data_root()`` 决定应用写盘位置（``logs/``、``cache/``、
``merge_state/``、``.session.json`` …）。此前测试**没有**隔离它，于是：

  - ``tests/test_discover_repos.py`` 调用 ``discover_repos()`` 时，会真的往仓库根
    的 ``logs/`` 里写一份 ``discover_raw_<时间戳>.txt`` 接口原始 dump；
  - 每次跑测试都新增若干份，实测累积到 **537 个文件 / 113MB**；
  - 更糟的是任何「清理/保留策略」一旦生效，会在测试运行期间删除**用户真实数据**。

因此这里在**导入任何 core/api 模块之前**把 ``JIRA_GIT_DATA_DIR`` 指向临时目录，
让整个测试会话的读写都落在临时区，结束后自动删除。

注意：``core/app_paths`` 在 import 时会捕获数据根，所以本文件必须最先执行——
pytest 保证 conftest.py 早于测试模块导入。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_TMP_DATA_ROOT = tempfile.mkdtemp(prefix="jgg-tests-data-")

# 用 setdefault 以外的语义：测试必须隔离，不允许继承外部真实数据目录
os.environ["JIRA_GIT_DATA_DIR"] = _TMP_DATA_ROOT

# 顺带把可能污染真实目录的开关关掉
os.environ.setdefault("JGG_KEEP_DISCOVER_RAW", "3")

atexit.register(shutil.rmtree, _TMP_DATA_ROOT, ignore_errors=True)
