# -*- coding: utf-8 -*-
"""CF 云函数日志 —— 路由层（薄 HTTP 适配）。

业务逻辑全部在 api.cf_core，这里只做：参数接收、异常→HTTP 状态码映射、
SSE 广播副作用（如登录成功后广播 token 更新）。
"""
from fastapi import HTTPException

from fastapi import APIRouter
from pydantic import BaseModel
from api.common import app, logger, broadcast, get_cf_accounts
from api.cf.cf_core import (
    cf_captcha_fetch, cf_login_account, cf_autologin_all,
    cf_refresh_token, cf_query_logs, cf_export_logs, cf_save_clipboard,
    cf_mask_tokens, cf_diagnose_context, cf_save_case, cf_save_feedback,
    cf_feedback_metrics, cf_list_cases, cf_rebuild_source_index, cf_parse_log_rows,
    cf_apply_feedback_learnings,
)
from api.schemas import (
    CfLogReq, CfLogExportReq, CfLoginReq, CfCaptchaReq, CfAutoLoginReq, ClipboardSaveReq,
    CfDiagnoseReq, CfCaseSaveReq, CfCaseFeedbackReq, CfFeedbackLearnReq,
)

router = APIRouter()


def _http_error(e: Exception) -> HTTPException:
    """把业务异常映射为合适的 HTTP 状态码。"""
    msg = str(e)
    if isinstance(e, (PermissionError,)):
        return HTTPException(401, msg)
    if isinstance(e, (ConnectionError,)):
        return HTTPException(502, msg)
    if isinstance(e, (TimeoutError,)):
        return HTTPException(504, msg)
    if isinstance(e, (ValueError,)):
        return HTTPException(400, msg)
    return HTTPException(500, msg)


@router.get("/api/cf/accounts")
async def api_cf_accounts():
    """返回配置的 CF 账号列表（不含凭证明文）。"""
    accounts = get_cf_accounts()
    return {
        "accounts": [
            {
                "name": a.get("name", ""),
                "server_url": a.get("server_url", ""),
                "username": a.get("username", a.get("mobile", "")),
                "has_password": bool(a.get("password")),
                "has_cookie": bool(a.get("cookie")),
                "source": "config",
            }
            for a in accounts
        ]
    }


@router.get("/api/cf/captcha")
async def api_cf_captcha(server_url: str = "", proxy: str = ""):
    """获取 CF 登录验证码图片（返回 base64 data URL）。"""
    try:
        return await cf_captcha_fetch(server_url, proxy=proxy)
    except (ValueError, ConnectionError, TimeoutError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/login")
async def api_cf_login(req: CfLoginReq):
    """手动登录单个 CF 账号（支持验证码），缓存并返回 token。"""
    try:
        r = await cf_login_account(
            {"server_url": req.server_url, "username": req.mobile, "password": req.password},
            proxy=req.proxy,
        )
    except Exception as e:
        raise _http_error(e)
    from api.cf.cf_tokens import _CF_TOKEN_CACHE, _cf_tokens_save, TOKEN_CACHE_LOCK
    su = req.server_url.rstrip("/")
    if r.get("ok"):
        with TOKEN_CACHE_LOCK:
            _CF_TOKEN_CACHE[su] = {
                "token": r["token"], "cookie": r.get("cookie", ""),
                "name": req.mobile, "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                "need_captcha": False, "last_error": "",
            }
            _cf_tokens_save()
        broadcast("cf_token_update", {"server_url": su, "name": req.mobile, "ok": True})
        return {"ok": True, "token": r["token"], "cookie": r.get("cookie", ""), "message": "登录成功"}
    with TOKEN_CACHE_LOCK:
        _CF_TOKEN_CACHE.setdefault(su, {"token": "", "name": req.mobile})
        _CF_TOKEN_CACHE[su].update({"last_error": r.get("message", ""), "need_captcha": r.get("need_captcha", False)})
    return {"ok": False, "need_captcha": r.get("need_captcha", False), "message": r.get("message", "登录失败")}


@router.post("/api/cf/auto-login")
async def api_cf_auto_login(req: CfAutoLoginReq):
    """对配置的账号做自动登录（账号密码），返回结果列表。"""
    try:
        results = await cf_autologin_all(proxy=req.proxy)
    except Exception as e:
        raise _http_error(e)
    broadcast("cf_token_update", {"auto_login": True, "count": len(results)})
    return {"results": results, "count": len(results)}


@router.post("/api/cf/refresh-token")
async def api_cf_refresh_token(req: CfLoginReq):
    """对**单个**网关重新登录，取回新 token。

    HCM token 默认仅 2 小时有效（hcm_cloud.context_expire_seconds），过期后查询会报
    17003/51006。这里用 cf_accounts 中该网关已存的账号密码重登，比 auto-login（全量）快得多。
    """
    server_url = (req.server_url or "").strip()
    if not server_url:
        raise HTTPException(400, "缺少 server_url")
    try:
        entry = await cf_refresh_token(server_url, req.proxy)
    except Exception as e:
        raise _http_error(e)
    if not entry or not (entry.get("token") or entry.get("cookie")):
        raise HTTPException(401, f"重新登录失败：{server_url}（请确认 cf_accounts 已配置该网关账号密码）")
    broadcast("cf_token_update", {"server_url": server_url.rstrip("/"), "ok": True})
    return {
        "ok": True,
        "server_url": server_url,
        "token": entry.get("token", ""),
        "cookie": entry.get("cookie", ""),
        "ts": entry.get("ts", ""),
    }


@router.get("/api/cf/tokens")
async def api_cf_tokens():
    """返回已缓存 token 状态（掩码，不含明文）。"""
    return cf_mask_tokens()


@router.post("/api/cf/logs")
async def api_cf_logs(req: CfLogReq):
    """查询 CF 云函数日志。"""
    try:
        return await cf_query_logs(req)
    except (ValueError, PermissionError, ConnectionError, TimeoutError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/logs/export")
async def api_cf_export(req: CfLogExportReq):
    """导出 CF 日志到本地 JSON 文件。"""
    try:
        return cf_export_logs(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/clipboard-save")
async def api_cf_clipboard_save(req: ClipboardSaveReq):
    """保存剪贴板内容到本地文件。"""
    try:
        return cf_save_clipboard(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/diagnose-context")
async def api_cf_diagnose_context(req: CfDiagnoseReq):
    """聚合诊断上下文：解析错误 + 查词典 + 路由 Wiki + Token 健康度 + 历史案例。

    一次调用拿到全部素材，AI（或前端面板）无需再逐个文件读。
    路由规则运行时从 ERROR_ROUTE_INDEX.md 解析，改 md 即改路由。
    """
    try:
        return cf_diagnose_context(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/cases/save")
async def api_cf_case_save(req: CfCaseSaveReq):
    """保存诊断案例到 logs/cf_cases/。"""
    try:
        return cf_save_case(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.get("/api/cf/cases")
async def api_cf_cases(keyword: str = "", limit: int = 50):
    """列出诊断案例库条目（可按关键词过滤）。"""
    return cf_list_cases(keyword=keyword, limit=limit)


@router.post("/api/cf/cases/feedback")
async def api_cf_case_feedback(req: CfCaseFeedbackReq):
    """记录 AI 诊断的人工确认结果，供准确率统计和规则迭代。"""
    try:
        return cf_save_feedback(req)
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.get("/api/cf/cases/metrics")
async def api_cf_case_metrics():
    """返回诊断反馈准确率与结果分布。"""
    return cf_feedback_metrics()


@router.post("/api/cf/cases/feedback-learn")
async def api_cf_case_feedback_learn(req: CfFeedbackLearnReq):
    """诊断→规范闭环：根据人工反馈反哺 errdict.json 与 ERROR_ROUTE_INDEX.md。

    ``apply=false`` 仅产出提案预览（默认，安全）；``apply=true`` 先备份再回写。
    """
    try:
        return cf_apply_feedback_learnings(apply=bool(req.apply), max_proposals=int(req.max_proposals or 100))
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/diagnose-index/rebuild")
async def api_cf_diagnose_index_rebuild():
    """重建参考云函数源码索引；源码更新后调用一次即可。"""
    try:
        return cf_rebuild_source_index()
    except (ValueError, RuntimeError) as e:
        raise _http_error(e)


@router.post("/api/cf/logs/parse")
async def api_cf_logs_parse(req: CfLogExportReq):
    """把日志行的 content 字段解析成结构化字段（不改文件，仅返回解析结果）。

    用于前端「结构化预览」与 AI 消费：标签、级别、stage、dept_id、errcode、是否疑似错误。
    """
    try:
        return {"rows": cf_parse_log_rows(req.rows), "count": len(req.rows or [])}
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


# --------------------------------------------------------------------------- #
# 云函数改造工具（前端化）：上传 .py → 审计/预览 diff/应用下载，全部在内存完成，
# 不写真实文件、不触碰 git。直接复用 tools/cf_locate_retrofit.py 的核心函数。
# --------------------------------------------------------------------------- #
class CfRetrofitReq(BaseModel):
    content: str
    mode: str = "audit"          # audit | diff | apply
    redact_sensitive: bool = False


def _load_retrofit():
    """懒加载改造工具模块（tools/cf_locate_retrofit.py），避免常驻导入开销。"""
    import importlib.util
    tools_dir = Path(__file__).resolve().parents[2] / "tools"
    mod_path = tools_dir / "cf_locate_retrofit.py"
    if not mod_path.exists():
        raise RuntimeError("retrofit tool not found at tools/cf_locate_retrofit.py")
    spec = importlib.util.spec_from_file_location("cf_locate_retrofit", str(mod_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tools_dir


@router.post("/api/cf/retrofit")
async def api_cf_retrofit(req: CfRetrofitReq):
    """上传云函数源码，返回审计/改造结果（不写盘）。

    - audit: 返回风险清单（含字段访问面），供前端表格展示；
    - diff : 返回 unified diff（未写盘），供预览；
    - apply: 返回改造后完整源码，供前端下载（原文件由用户本地保留）。
    """
    import difflib
    import tempfile
    from pathlib import Path

    if not req.content.strip():
        raise HTTPException(400, "empty content")
    mode = req.mode if req.mode in ("audit", "diff", "apply") else "audit"
    try:
        rt, tools_dir = _load_retrofit()
        snippet_path = tools_dir / "cf_locate_kit" / "locate_snippet.py"

        # audit 需要真实文件路径；写到临时文件，用完即删。
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
            tf.write(req.content)
            tmp_path = Path(tf.name)
        try:
            if mode == "audit":
                info = rt.audit_file(tmp_path)
                info_public = {k: v for k, v in info.items() if k != "_accesses"}
                info_public["accesses"] = info.get("_accesses", [])
                return {"ok": True, "mode": "audit", "report": info_public}

            # diff / apply 需要注入的 snippet
            snippet = rt._snippet_body(snippet_path)
            new = rt._transform(req.content, snippet, redact_sensitive=bool(req.redact_sensitive))
            changed = new != req.content
            if mode == "diff":
                diff = "".join(difflib.unified_diff(
                    req.content.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile="a/uploaded.py", tofile="b/uploaded.py", n=3,
                ))
                return {"ok": True, "mode": "diff", "changed": changed, "diff": diff}
            # apply
            return {"ok": True, "mode": "apply", "changed": changed, "new_content": new}
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)
