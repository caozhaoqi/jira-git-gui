# -*- coding: utf-8 -*-
"""api 包共享状态与工具。

把原先挤在 server.py 顶部的全局单例、配置加载、事件总线接入，
以及跨业务域复用的小工具（token 掩码、会话失效判定等）集中到此处，
供各 routes_*.py 模块导入，避免循环依赖与重复代码。
"""
import sys
import threading
import time
import uuid
import datetime as dt
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from core.client import JiraGitClient, NetworkWatchdog, DEFAULT_DOWNLOAD_WORKERS
from core.config import (
    load_config, load_session, save_session, get_session_path,
    load_cf_accounts, load_hcm_whitelist,
)
from core.constants import DEFAULT_REQUEST_QPS
from core.models import Commit
from core.logger import get_logger
from api.eventbus import (
    broadcast as _broadcast,
    capture_loop as _capture_loop,
    subscribe,
    unsubscribe,
)

# 确保项目根目录在 sys.path 中（api 包内模块也可能被直接 import）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# --------------------------------------------------------------------------- #
#  应用实例与全局单例
# --------------------------------------------------------------------------- #
app = FastAPI(title="Jira Git GUI API", version="2.0")
logger = get_logger()

# 客户端单例（单用户本地工具）
client = JiraGitClient()

# 从 .env 加载默认配置
_cfg, _env_loaded, _env_path = load_config()
if _env_loaded:
    client.set_config(_cfg)

# 从 session.json 加载上次保存的 Cookie（优先级高于 .env）
_session = load_session()
if _session.get("cookie"):
    client.config.cookie = _session["cookie"]
    if _session.get("jira_url") and not client.config.jira_url:
        client.config.jira_url = _session["jira_url"]
    if _session.get("username") and not client.config.username:
        client.config.username = _session["username"]

# 事件总线别名：保持既有调用点不变
broadcast = _broadcast
capture_loop = _capture_loop

# 当前下载任务取消标志与状态
# ⚠️ 旧实现是模块级单例 threading.Event()，被 /api/download 与 /api/download/repo 共用：
# 一次 cancel 会误杀另一个并发任务，且后启动的任务 .clear() 会清掉先任务的标志。
# 现改为「每次请求注册一个专属 Event」，取消端点一次性 set 全部活动任务。
_DOWNLOAD_CANCELS: "dict[str, threading.Event]" = {}
_DOWNLOAD_CANCELS_LOCK = threading.Lock()


def register_download_cancel() -> str:
    """为一次新下载任务生成隔离的取消 Event，返回任务 id。"""
    task_id = uuid.uuid4().hex
    with _DOWNLOAD_CANCELS_LOCK:
        _DOWNLOAD_CANCELS[task_id] = threading.Event()
    return task_id


def get_download_cancel(task_id: str) -> "threading.Event":
    """取回某任务的取消 Event（注册后立即调用，必存在）。"""
    with _DOWNLOAD_CANCELS_LOCK:
        return _DOWNLOAD_CANCELS[task_id]


def unregister_download_cancel(task_id: str) -> None:
    """任务结束后从活动表移除（已 set 的 Event 由调用方持有的引用继续生效）。"""
    with _DOWNLOAD_CANCELS_LOCK:
        _DOWNLOAD_CANCELS.pop(task_id, None)


def cancel_all_downloads() -> int:
    """取消所有正在进行的下载任务，返回被取消的任务数。"""
    with _DOWNLOAD_CANCELS_LOCK:
        events = list(_DOWNLOAD_CANCELS.values())
        _DOWNLOAD_CANCELS.clear()
    for ev in events:
        ev.set()
    return len(events)


task_status: dict[str, Any] = {"running": False, "type": None}

# HCM 代理配置
_HCM_WL = load_hcm_whitelist() or {}
HCM_PROXY_TARGET = (_HCM_WL.get("proxy_target", {}) or {}).get("base_url", "") or ""
HCM_PRESET_TOKEN = (_HCM_WL.get("token", "") or "").strip()

# 兼容别名（保持与旧 server.py 调用点一致的命名）
_HCM_PROXY_TARGET = HCM_PROXY_TARGET
_HCM_PRESET_TOKEN = HCM_PRESET_TOKEN


# --------------------------------------------------------------------------- #
#  共享工具函数
# --------------------------------------------------------------------------- #
def mask_token(token: str) -> str:
    """将 token 掩码展示，避免在前端/日志泄露明文。"""
    if not token:
        return ""
    if len(token) > 16:
        return token[:8] + "..." + token[-4:]
    return token[:4] + "****"


def mask_cookie(cookie: str) -> str:
    """将 cookie 形式凭证掩码展示。"""
    first = cookie.split(";", 1)[0]
    if "=" in first:
        ck, cv = first.split("=", 1)
        return f"{ck}={cv[:6]}...{cv[-4:]}" if len(cv) > 12 else f"{ck}={cv[:4]}****"
    return cookie[:4] + "****"


def cf_is_session_err(status: Optional[int], errcode: Any, errmsg: str) -> bool:
    """判定一次认证失败是否属于「会话类」（token 失效 / 未登录 / 登录过期）。

    会话类失败才触发重登刷新凭证；平台 5xx / 连接 / 超时 / 普通 4xx 不属于会话类，
    不应误判为 token 失效（否则会把平台抖动误触发无效重登）。
    """
    return status in (401, 403) or errcode == 17003 or any(
        k in str(errmsg) for k in ("未登录", "登录过期", "未授权", "unauthorized", "请先登录")
    )


def cf_token_stale(v: Any) -> bool:
    """判断缓存凭证是否可能已过期（供前端提示刷新）。"""
    if not isinstance(v, dict):
        return True
    if v.get("last_error") and (v.get("token") or v.get("cookie")):
        return True
    ts = v.get("ts") or ""
    if not ts:
        return False
    try:
        st = time.strptime(ts, "%Y-%m-%d %H:%M:%S")
        age_h = (time.time() - time.mktime(st)) / 3600.0
    except Exception:
        return False
    return age_h >= 24


# 兼容别名（保持与旧 server.py 调用点一致的命名）
_cf_is_session_err = cf_is_session_err
_cf_token_stale = cf_token_stale


def get_cf_accounts() -> list:
    """安全加载云函数账号配置（出错返回空列表）。"""
    try:
        return load_cf_accounts() or []
    except Exception:
        return []


# --------------------------------------------------------------------------- #
#  client 回调（SSE 推送 / 取消判定）—— 下载、克隆等后台任务共用
# --------------------------------------------------------------------------- #
def log_callback(msg: str) -> None:
    """client 回调：把日志推送到 SSE。"""
    broadcast("log", {"msg": msg, "ts": time.strftime("%H:%M:%S")})


def progress_callback(done: int, total: int, path: str) -> None:
    """client 回调：把进度推送到 SSE。"""
    pct = (done * 100 // total) if total > 0 else 0
    broadcast("progress", {"done": done, "total": total, "pct": pct, "path": path})


def make_should_cancel(user_cancel: threading.Event,
                       watchdog: "NetworkWatchdog",
                       task_label: str = "任务"):
    """合成 should_cancel 回调：同时响应用户取消和网络看门狗触发。

    当看门狗首次触发时，额外广播 ``network_warning`` SSE 事件以通知前端。
    """
    _warned = threading.Event()

    def should_cancel() -> bool:
        if user_cancel.is_set():
            return True
        if watchdog.should_abort():
            if not _warned.is_set():
                _warned.set()
                broadcast("network_warning", {
                    "level": "error",
                    "message": f"网络中断：{task_label} 因连续网络失败自动停止（{watchdog.reason}）",
                    "failure_count": watchdog.failure_count,
                })
                logger.warning("网络看门狗触发：%s", watchdog.reason)
            return True
        return False

    return should_cancel


def commit_to_dict(c: "Commit") -> dict:
    """将 core.models.Commit 转为前端友好的 dict。"""
    return {
        "commit_id": c.commit_id,
        "display_id": c.display_id,
        "author": c.author,
        "date": c.date,
        "message": c.message,
        "branch": c.branch,
        "repository_name": c.repository_name,
        "files": [
            {
                "path": f.path,
                "change_type": f.change_type,
                "lines_added": f.lines_added,
                "lines_removed": f.lines_removed,
            }
            for f in c.files
        ],
    }
