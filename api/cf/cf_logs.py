# -*- coding: utf-8 -*-
"""CF 云函数日志 —— 日志查询、导出、剪贴板、token 状态。

依赖 ``api.cf_tokens``（共享 token 缓存）与 ``api.cf_login``（按需刷新）。
"""
import json
from datetime import datetime

import httpx

from api.common import logger, _PROJECT_ROOT, _HCM_WL, _cf_is_session_err, _cf_token_stale
from api.cf.cf_tokens import (
    _CF_TOKEN_CACHE, _cf_tokens_save,
    _HCM_MODEL_LIST_API, _HCM_HCMINNER_HEADER, _HCM_HCMINNER_VALUE,
    _cf_ssl_context,
)
from api.cf.cf_login import cf_refresh_token
from api.cf.cf_diagnose import cf_parse_log_rows


_IS_MODEL_MISSING = lambda errcode, errmsg: (
    errcode == 80001 or (isinstance(errmsg, str) and "Unknown Model Name" in errmsg)
)


async def cf_query_logs(req) -> "dict":
    """代理查询 CF 平台 dynamic_log 日志，返回 {result, error, is_session}。

    认证方式依次尝试：Cookie → Bearer+hcminner → Header token。
    会话类失败自动触发重登刷新后重试一次。
    """
    req_token = (req.token or "").strip()
    token = ""
    cookie_value = ""
    cached = _CF_TOKEN_CACHE.get(req.server_url.rstrip("/"))
    if isinstance(cached, dict):
        cached_token = (cached.get("token") or "").strip()
        cached_cookie = (cached.get("cookie") or "").strip()
    else:
        cached_token, cached_cookie = "", ""

    if req_token:
        token = req_token
        cookie_value = f"token={req_token}"
    elif cached_token or cached_cookie:
        token = cached_token
        cookie_value = cached_cookie or (f"token={cached_token}" if cached_token else "")
        if _cf_token_stale(cached):
            fresh = await cf_refresh_token(req.server_url, req.proxy)
            if isinstance(fresh, dict) and (fresh.get("token") or fresh.get("cookie")):
                token = (fresh.get("token") or "").strip()
                cookie_value = (fresh.get("cookie") or "").strip() or (f"token={token}" if token else "")
                cached = fresh
            else:
                logger.warning(f"[CF] 查询前主动刷新失败，仍用旧凭证尝试: {req.server_url}")
    else:
        fresh = await cf_refresh_token(req.server_url, req.proxy)
        if isinstance(fresh, dict) and (fresh.get("token") or fresh.get("cookie")):
            token = (fresh.get("token") or "").strip()
            cookie_value = (fresh.get("cookie") or "").strip() or (f"token={token}" if token else "")
            cached = fresh
        else:
            raise PermissionError("请先配置 token（或先执行自动登录获取 token）")

    if req_token:
        suspicious = ("<html", "<!doctype", "__image_validate_index", "input ", "name=")
        low = req_token.lower()
        for s in suspicious:
            if s in low:
                raise ValueError("Token 格式异常，请重新获取 Token（当前值疑似 HTML/验证码片段，而非登录 Token）")

    base = (req.server_url or "").strip().rstrip("/")
    if not base or base.startswith("/"):
        raise ValueError("请配置有效的 server_url（自动登录失败的账号需手动补全或重新登录后再查询）")
    try:
        req_page_size = max(1, min(int(req.page_size), 1000))
    except (TypeError, ValueError):
        req_page_size = 200
    url = f"{base}{_HCM_MODEL_LIST_API}"
    base_headers_json = {"Content-Type": "application/json"}
    filter_dict = {}
    if req.log_type:
        filter_dict["log_type"] = req.log_type
    payload = {
        "model": "dynamic_log",
        "page_index": req.page_index,
        "page_size": req_page_size,
        "filter_dict": filter_dict,
    }

    def _client_kwargs():
        ctx = _cf_ssl_context()
        kw = dict(timeout=30, follow_redirects=True, verify=ctx)
        if req.proxy:
            kw["proxy"] = req.proxy
        else:
            # 显式 transport 会忽略客户端的 verify=，必须在这里也传入 ssl 上下文
            kw["transport"] = httpx.AsyncHTTPTransport(verify=ctx)
        return kw

    def _build_attempts():
        return [
            {"name": "cookie", "headers": {**base_headers_json, "Cookie": cookie_value}},
            {"name": "bearer_hcminner",
             "headers": {**base_headers_json, "Authorization": f"Bearer {token}", _HCM_HCMINNER_HEADER: _HCM_HCMINNER_VALUE}},
            {"name": "header_token",
             "headers": {**base_headers_json, "token": token}},
        ]

    async def _run_query_once():
        attempts = _build_attempts()
        results = []
        for i, att in enumerate(attempts):
            try:
                client_kw = _client_kwargs()
                if "cookies" in att:
                    client_kw["cookies"] = att["cookies"]
                async with httpx.AsyncClient(**client_kw) as client:
                    resp = await client.post(url, json=payload, headers=att["headers"])
                    if resp.status_code >= 400:
                        try:
                            eb = resp.json()
                        except ValueError:
                            eb = {}
                        eb = eb if isinstance(eb, dict) else {}
                        errcode = eb.get("errcode")
                        errmsg = eb.get("errmsg") or eb.get("description") or eb.get("message") or resp.text[:400]
                        if _IS_MODEL_MISSING(errcode, errmsg):
                            raise RuntimeError(f"[{att['name']}] {errmsg}（model「dynamic_log」在当前部署/租户不存在）")
                        if resp.status_code == 405:
                            raise RuntimeError(f"[{att['name']}] HTTP 405 Method Not Allowed: {resp.text[:300]}")
                        if resp.status_code >= 500:
                            raise RuntimeError(f"[{att['name']}] 平台暂时错误（HTTP {resp.status_code}），请稍后重试")
                        if resp.status_code in (401, 403) and _cf_is_session_err(resp.status_code, errcode, errmsg):
                            raise PermissionError(f"[{att['name']}] HTTP {resp.status_code}: {errmsg[:400]}（token 可能已失效，建议重新登录获取 Token）")
                        raise RuntimeError(f"[{att['name']}] HTTP {resp.status_code}: {errmsg[:400] or resp.text[:400]}")
                    try:
                        data = resp.json()
                    except ValueError:
                        raise RuntimeError(f"[{att['name']}] 返回非JSON: {resp.text[:600]}")
                    biz_fail = (isinstance(data, dict) and data.get("success") is False) or \
                               (isinstance(data, dict) and data.get("errcode") and data.get("errcode") != 0)
                    if biz_fail:
                        msg = (data.get("errmsg") or data.get("description") or data.get("message") or data.get("msg") or
                               (isinstance(data.get("result"), dict) and data["result"].get("message")) or str(data)[:500])
                        if _IS_MODEL_MISSING(data.get("errcode"), msg):
                            raise RuntimeError(f"[{att['name']}] {msg}（model「dynamic_log」在当前部署/租户不存在）")
                        ec = data.get("errcode")
                        ec = ec if isinstance(ec, int) else 0
                        if _cf_is_session_err(ec if ec in (401, 403) else 0, ec, msg):
                            raise PermissionError(f"[{att['name']}] 业务失败: {msg}（token 可能已失效，建议重新登录获取 Token）")
                        raise RuntimeError(f"[{att['name']}] 业务失败: {msg}")
                    if isinstance(data, dict) and "result" in data:
                        return {"result": {"method": att["name"], "raw": data, "data": data["result"]}, "error": None, "is_session": False}
                    return {"result": ({**data, "method": att["name"]} if isinstance(data, dict) else data), "error": None, "is_session": False}
            except httpx.ConnectError as e:
                results.append((att["name"], ConnectionError(f"[{att['name']}] 无法连接服务器 {base}: {e}")))
                break
            except httpx.TimeoutException as e:
                results.append((att["name"], TimeoutError(f"[{att['name']}] 请求超时: {e}")))
                continue
            except (PermissionError, RuntimeError) as e:
                results.append((att["name"], e))
                continue
            except Exception as e:
                logger.exception(f"[CF] 方式{i+1} 异常: {e}")
                results.append((att["name"], RuntimeError(f"[{att['name']}] {type(e).__name__}: {e}")))
                continue
        cookie_err = next((e for n, e in results if n == "cookie" and e is not None), None)
        primary = cookie_err if cookie_err is not None else (results[-1][1] if results else RuntimeError("查询失败，未知错误"))
        is_session = _cf_is_session_err(getattr(primary, "status_code", 0), None, str(getattr(primary, "args", ("",))[0]))
        return {"result": None, "error": primary, "is_session": is_session}

    first = await _run_query_once()
    if first["result"] is not None:
        return first["result"]
    last_error = first["error"]
    if first["is_session"] and not req_token:
        fresh = await cf_refresh_token(req.server_url, req.proxy)
        if isinstance(fresh, dict) and (fresh.get("token") or fresh.get("cookie")):
            token = (fresh.get("token") or "").strip()
            cookie_value = (fresh.get("cookie") or "").strip() or (f"token={token}" if token else "")
            retry = await _run_query_once()
            if retry["result"] is not None:
                return retry["result"]
            last_error = retry["error"]
    raise last_error


def cf_export_logs(req) -> "dict":
    """将查询到的 CF 云函数日志导出为本地 JSON 文件，返回文件路径与内容。

    dynamic_log 的 ``content`` 是**字符串**（Python repr 或 ``[TAG] 正文``），
    格式不统一。这里统一做一次结构化解析，为每行补上 ``parsed_content``
    （tags/level/stage/dept_id/object_id/errcode/is_error/locate），
    并在顶层给出 ``error_summary``，便于 AI/前端直接消费而不必逐条适应格式。
    """
    if not req.rows:
        raise ValueError("无可导出的日志数据")
    export_dir = _PROJECT_ROOT / "logs" / "cf_logs"
    export_dir.mkdir(parents=True, exist_ok=True)
    safe_log_type = "".join(c if c.isalnum() or c in "-_" else "_" for c in (req.log_type or "unknown"))[:60]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"cf_logs_{safe_log_type}_{ts}.json"
    fpath = export_dir / fname

    rows = cf_parse_log_rows(req.rows)  # 结构化解析，保留原字段
    out = {
        "export_info": {
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "CF 云函数日志 (dynamic_log)",
            "server_url": req.server_url,
            "log_type": req.log_type,
            "auth_method": req.auth_method,
            "page_index": req.page_index,
            "page_size": req.page_size,
            "total": req.total,
            "returned_count": len(req.rows),
            "keyword": req.keyword or "",
            "filtered_by_client": bool(req.filtered or req.keyword),
            "parsed": True,  # 标记本文件已做结构化解析
        },
        "error_summary": _summarize_parsed(rows),
        "logs": rows,
    }
    if req.raw is not None:
        out["raw_response"] = req.raw
    content = json.dumps(out, ensure_ascii=False, indent=2)
    try:
        fpath.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.exception(f"[CF] 导出日志写入失败: {e}")
        raise RuntimeError(f"写入文件失败: {e}")
    logger.info(f"[CF] 日志已导出: {fpath} ({len(req.rows)} 条)")
    return {"ok": True, "path": str(fpath), "filename": fname, "count": len(req.rows), "content": content}


def _summarize_parsed(rows: "list") -> "dict":
    """对结构化后的日志做汇总统计，让 AI 一眼看到错误分布。"""
    total = len(rows)
    errors, errcodes, stages, levels = [], {}, {}, {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pc = r.get("parsed_content") or {}
        lv = pc.get("level") or "OTHER"
        levels[lv] = levels.get(lv, 0) + 1
        if pc.get("stage"):
            stages[pc["stage"]] = stages.get(pc["stage"], 0) + 1
        ec = pc.get("errcode")
        if isinstance(ec, int) and ec != 0:
            errcodes[str(ec)] = errcodes.get(str(ec), 0) + 1
        if pc.get("is_error"):
            errors.append({
                "id": r.get("id"),
                "create_time": r.get("create_time"),
                "log_type": r.get("log_type"),
                "stage": pc.get("stage"),
                "dept_id": pc.get("dept_id"),
                "object_id": pc.get("object_id"),
                "errcode": ec,
                "message": (pc.get("message") or "")[:300],
                "locate": pc.get("locate"),
            })
    return {
        "total_rows": total,
        "error_count": len(errors),
        "levels": levels,
        "stages": stages,
        "errcodes": errcodes,
        # 只保留最近 50 条错误明细，避免导出文件过大
        "errors": errors[:50],
        "errors_truncated": len(errors) > 50,
    }


def cf_save_clipboard(req) -> "dict":
    """将剪贴板文本内容保存为本地文件，返回文件路径。"""
    if not req.text or not req.text.strip():
        raise ValueError("剪贴板内容为空")
    export_dir = _PROJECT_ROOT / "logs" / "cf_clipboard"
    export_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in (req.filename or "").strip())[:80]
    if not safe_name:
        safe_name = f"clipboard_{ts}.txt"
    elif not safe_name.endswith((".txt", ".json", ".log", ".md")):
        safe_name += ".txt"
    fpath = export_dir / safe_name
    try:
        fpath.write_text(req.text, encoding="utf-8")
    except Exception as e:
        logger.exception(f"[CF] 剪贴板文件写入失败: {e}")
        raise RuntimeError(f"写入文件失败: {e}")
    logger.info(f"[CF] 剪贴板已保存: {fpath} ({len(req.text)} chars)")
    return {"ok": True, "path": str(fpath), "filename": safe_name, "size": len(req.text)}


def cf_mask_tokens() -> "dict":
    """返回已缓存的 CF 账号 token 状态（token 仅掩码展示，绝不返回明文）。"""
    out = []
    for su, v in _CF_TOKEN_CACHE.items():
        if not isinstance(v, dict):
            continue
        tok = v.get("token", "") or ""
        cookie = v.get("cookie", "") or ""
        masked = ""
        if tok:
            masked = (tok[:8] + "..." + tok[-4:]) if len(tok) > 16 else (tok[:4] + "****")
        elif cookie:
            first = cookie.split(";", 1)[0]
            if "=" in first:
                ck, cv = first.split("=", 1)
                masked = f"{ck}={cv[:6]}...{cv[-4:]}" if len(cv) > 12 else f"{ck}={cv[:4]}****"
        out.append({
            "name": v.get("name", su),
            "server_url": su,
            "has_token": bool(tok) or bool(cookie),
            "token_masked": masked,
            "need_captcha": bool(v.get("need_captcha")),
            "last_error": v.get("last_error", "") or "",
            "ts": v.get("ts", "") or "",
            "stale": _cf_token_stale(v),
        })
    return {"tokens": out}
