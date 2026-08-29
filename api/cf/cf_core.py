# -*- coding: utf-8 -*-
"""CF 云函数日志 —— 业务逻辑聚合层（向后兼容 re-export）。

为提升可读性，原模块已按职责拆分为：
- ``api.cf_tokens``：token 缓存、验证码、通用工具
- ``api.cf_login``：账号登录 / 自动登录 / token 刷新
- ``api.cf_logs``：日志查询、导出、剪贴板、token 状态

- ``api.cf_diagnose``：错误解析、词典查询、Wiki 路由、结构化日志解析、案例库

本文件仅做 re-export，保持 ``from api.cf.cf_core import <symbol>`` 的既有调用方式不变。
业务逻辑实现请前往上述子模块。
"""
from api.cf.cf_tokens import (
    sniff_image_type,
    new_cf_client,
    cf_captcha_cleanup_expired,
    cf_captcha_new,
    cf_captcha_fetch,
    _CF_TOKEN_CACHE,
    _cf_tokens_save,
    _CF_CAPTCHA_CACHE,
    _CF_CAPTCHA_TTL,
    _CF_CAPTCHA_MAX,
    _CF_TOKENS_FILE,
    _cf_tokens_load,
    _HCM_MODEL_LIST_API,
    _HCM_HCMINNER_HEADER,
    _HCM_HCMINNER_VALUE,
)
from api.cf.cf_login import (
    cf_login_account,
    cf_autologin_all,
    cf_account_by_server,
    cf_refresh_token,
)
from api.cf.cf_logs import (
    cf_query_logs,
    cf_export_logs,
    cf_save_clipboard,
    cf_mask_tokens,
    _IS_MODEL_MISSING,
)
from api.cf.cf_diagnose import (
    cf_parse_error,
    cf_token_health,
    cf_diagnose_context,
    cf_save_case,
    cf_save_feedback,
    cf_feedback_metrics,
    cf_list_cases,
    cf_rebuild_source_index,
    parse_cf_log_content,
    cf_parse_log_rows,
    cf_apply_feedback_learnings,
)

__all__ = [
    # tokens
    "sniff_image_type", "new_cf_client",
    "cf_captcha_cleanup_expired", "cf_captcha_new", "cf_captcha_fetch",
    "_CF_TOKEN_CACHE", "_cf_tokens_save", "_CF_CAPTCHA_CACHE", "_CF_CAPTCHA_TTL",
    "_CF_CAPTCHA_MAX", "_CF_TOKENS_FILE", "_cf_tokens_load",
    "_HCM_MODEL_LIST_API", "_HCM_HCMINNER_HEADER", "_HCM_HCMINNER_VALUE",
    # login
    "cf_login_account", "cf_autologin_all", "cf_account_by_server", "cf_refresh_token",
    # logs
    "cf_query_logs", "cf_export_logs", "cf_save_clipboard", "cf_mask_tokens",
    "_IS_MODEL_MISSING",
    # diagnose
    "cf_parse_error", "cf_token_health", "cf_diagnose_context",
    "cf_save_case", "cf_save_feedback", "cf_feedback_metrics", "cf_list_cases", "cf_rebuild_source_index",
    "parse_cf_log_content", "cf_parse_log_rows", "cf_apply_feedback_learnings",
]
