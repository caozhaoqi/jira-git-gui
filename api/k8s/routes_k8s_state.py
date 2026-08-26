# -*- coding: utf-8 -*-
"""K8s 路由共享状态与辅助函数（供 routes_k8s_*.py 子模块复用）。

把原本集中在 ``routes_k8s.py`` 的模块级全局状态（快照任务的取消信号 / 运行状态 /
最近输出目录 / 快照元数据）与纯函数（since/until 时间参数归一化）抽到此处，
由各业务子路由模块共享，避免跨模块 ``global`` 可变标量的坑。
"""
import logging
import re
import threading
from typing import Optional, Tuple

logger = logging.getLogger("api.routes_k8s")


class _K8sState:
    """K8s 快照任务的进程内共享状态（mutable 容器，跨子模块共享同一实例）。"""

    def __init__(self) -> None:
        self.cancel = threading.Event()          # 取消信号
        self.running = False                      # 是否有快照任务在跑
        self.out_dir = {"dir": None}             # 最近一次输出目录（日志/报告下载）
        # 最近一次快照使用的 kubeconfig / namespace，供「查看日志」实时回退到集群拉取
        self.snap_meta = {"kubeconfig": None, "namespace": None}


# 全局唯一状态实例（各子模块 from api.routes_k8s_state import state 共享）
state = _K8sState()

# ------------------------------------------------------------------- 时间参数校验
# kubectl --since / --until 接受的格式：
#   --since  仅相对时长（如 30m / 1h / 2d），不接受绝对时间；
#   --until  相对时长或 RFC3339 绝对时间（如 2026-08-25T10:00:00Z）。
# 任意无法解析的字符串（例如误填的 "error"）若直接透传给 kubectl，会让 `kubectl logs`
# 以 `invalid argument "error" for "--since" flag` 失败，进而被后端包成 404 抛给用户。
# 这里做容错归一化：非法值忽略该筛选并告警，而不是让整次查询崩溃。
_K8S_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?(ns|us|µs|ms|s|m|h|d)$")
_K8S_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


def normalize_time_arg(name: str, value, allow_abs: bool) -> Tuple[Optional[str], Optional[str]]:
    """归一化 since/until 时间参数。

    返回 ``(normalized_or_None, warning_or_None)``：
    - 空值        → (None, None)，表示不使用该筛选；
    - 合法值      → (去空格后的原值, None)；
    - 非法值      → (None, 警告文本)，容错忽略，避免 kubectl 崩溃。
    """
    if value is None or not str(value).strip():
        return None, None
    v = str(value).strip()
    if _K8S_DURATION_RE.match(v):
        return v, None
    if allow_abs and _K8S_RFC3339_RE.match(v):
        return v, None
    hint = (
        "应为相对时长如 30m/1h/2d" + ("，或 RFC3339 时间如 2026-08-25T10:00:00Z" if allow_abs else "")
    )
    return None, f"参数 {name}={v!r} 不是合法的 kubectl 时间格式（{hint}），已忽略该筛选"
