# -*- coding: utf-8 -*-
"""HCM 对象浏览器 —— 业务逻辑层。

提供：环境列表收集、同源代理转发、直连网关（复用 scripts/hcm_direct.py 的
加解密实现，与前端 crypto.ts 字节级一致）。
"""
import importlib.util as _ilu
import json
from datetime import datetime

import httpx
from fastapi import HTTPException

from api.common import (
    app, logger, _PROJECT_ROOT,
    _HCM_PROXY_TARGET, _HCM_HTTP_PROXY, _HCM_PRESET_TOKEN,
    get_cf_accounts,
)
from core.errors import UserError
from core.network_proxy import resolve_http_proxy


def _request_proxy(target: str) -> str:
    """Use the HCM server-specific proxy only for its configured gateway."""
    if (target or "").rstrip("/") == (_HCM_PROXY_TARGET or "").rstrip("/"):
        return resolve_http_proxy(_HCM_HTTP_PROXY)
    return ""



async def hcm_envs() -> "dict":
    """返回可选服务器环境列表（含预设 token 标记），供前端「选择服务器」。"""
    envs: list[dict] = []
    seen = set()

    if _HCM_PROXY_TARGET:
        envs.append({
            "key": "hcm_proxy",
            "name": "代理（同源）",
            "server_url": _HCM_PROXY_TARGET,
            "source": "hcm_whitelist",
            "has_preset_token": bool(_HCM_PRESET_TOKEN),
        })
        seen.add(_HCM_PROXY_TARGET)

    for acc in get_cf_accounts():
        url = (acc.get("server_url") or "").strip()
        if not url or url in seen:
            continue
        envs.append({
            "key": f"cf:{url}",
            "name": acc.get("name", url),
            "server_url": url,
            "domain": (acc.get("domain") or "").strip(),
            "source": "cf_accounts",
            "has_preset_token": False,
        })
        seen.add(url)

    return {"envs": envs}


async def hcm_proxy(api_name: str, request) -> "Response":
    """同源代理：转发到 HCM OpenAPI 网关 /api/<api_name>。"""
    from fastapi.responses import Response

    if not _HCM_PROXY_TARGET:
        raise HTTPException(
            500,
            "HCM 代理目标未配置：请在 config/hcm_whitelist.local.json 设置 proxy_target.base_url",
        )

    token = (request.headers.get("X-HCM-Token", "") or "").strip() or _HCM_PRESET_TOKEN
    model = request.query_params.get("model", "")
    target = f"{_HCM_PROXY_TARGET.rstrip('/')}/api/{api_name}"
    if model:
        target += f"?model={model}"

    body = await request.body()
    headers = {"Content-Type": "application/json"}
    cookies = {"token": token} if token else {}

    logger.info(
        "[代理] %s → %s token=%s body=%dB",
        api_name, target, "header" if request.headers.get("X-HCM-Token") else "preset", len(body),
    )
    try:
        # 复用 CF 共享的 SSL 上下文（开启旧式重协商），对 HTTP 目标无害；
        # 若将来 _HCM_PROXY_TARGET 配成 HTTPS legacy 服务器也能兼容。
        from api.cf.cf_tokens import _cf_ssl_context
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=_cf_ssl_context(),
            proxy=_request_proxy(_HCM_PROXY_TARGET) or None,
        ) as client:
            resp = await client.post(target, content=body, headers=headers, cookies=cookies)
        logger.info(
            "[代理] 网关响应 %s len=%dB head=%s",
            resp.status_code, len(resp.content), resp.content[:600].decode("utf-8", "replace"),
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.ConnectError as e:
        raise HTTPException(502, f"无法连接 HCM 服务器 {_HCM_PROXY_TARGET}: {e}")
    except httpx.TimeoutException:
        raise HTTPException(504, "连接 HCM 服务器超时")
    except Exception as e:
        logger.exception(f"[HCM] 代理异常: {e}")
        raise HTTPException(500, f"HCM 代理失败: {type(e).__name__}: {e}")


def _load_hcm_direct_core():
    """加载 scripts/hcm_direct.py（含 KEY/IV/AES/SM3/encrypt/sign/decrypt）。"""
    spec = _ilu.spec_from_file_location(
        "hcm_direct_core", str(_PROJECT_ROOT / "scripts" / "hcm_direct.py"))
    hd = _ilu.module_from_spec(spec)
    spec.loader.exec_module(hd)
    return hd


_TOKEN_ERRCODES = (17003, 51006)


async def _auto_relogin_token(target_url: str) -> str:
    """用 cf_accounts 中该网关的账号密码重登，返回新 token；失败返回空串。

    hcm_direct / hcm_decrypt_param 共用。
    """
    try:
        from api.cf.cf_login import cf_login_account, cf_account_by_server
        acc = cf_account_by_server(target_url)
        if not acc:
            logger.info("[HCM直连] cf_accounts 无该网关账号，跳过自动重登: %s", target_url)
            return ""
        r = await cf_login_account(acc, proxy="")
        if not (r or {}).get("ok"):
            logger.warning("[HCM直连] 自动重登失败 %s: %s", target_url, (r or {}).get("message"))
            return ""
        new_tok = (r.get("token") or "").strip()
        if new_tok:
            logger.info("[HCM直连] 自动重登成功 %s（新 token %d 字符）", target_url, len(new_tok))
        return new_tok
    except Exception as e:
        logger.warning("[HCM直连] 自动重登异常 %s: %s", target_url, e)
        return ""


def _resp_is_token_error(raw_text: str) -> bool:
    """判定网关响应是否为「token 失效」类错误。

    HCM token 默认仅 2 小时有效（``hcm_cloud.context_expire_seconds``，见
    hcm-core ``apps/idm/auth_util.py: AuthUtil.get_expire``）。过期后
    ``hcm.paas.object.list`` 等接口会内部抛异常并被框架兜底成 **17003**，
    而不是标准的 51006——这也是它难以一眼定位的原因。
    """
    try:
        d = json.loads(raw_text or "")
    except Exception:
        return False
    return isinstance(d, dict) and d.get("errcode") in _TOKEN_ERRCODES


def _is_html_error_page(raw_text: str) -> bool:
    """判定网关响应是否为 HTML 错误兜底页（非 JSON 结构体）。

    实测 ``hr-test.bii.com.cn``：token 失效时**不返回** 17003/51006 JSON，
    而是框架兜底成 HTTP 500 HTML 错误页（``<!DOCTYPE html> ...
    STATUS:500 服务器内部错误``），errcode 判定完全落空；有效 token +
    空信封体则回 501 HTML。JSON 必以 ``{``/``[`` 开头、HTML 必以 ``<``
    开头，据此区分。凡 5xx + HTML 形态按疑似登录态失效重登重试一次，
    重试后仍失败则原样透传（真·服务器错误多重一次登录无害）。
    """
    return (raw_text or "").lstrip().startswith("<")


async def hcm_direct(req) -> "dict":
    """直连 HCM 网关并解密响应（绕过同源透传链）。

    token 默认 2 小时过期，过期后查询会报 17003/51006。这里在首次响应为
    token 失效类错误时，用 cf_accounts 中该网关的账号密码**自动重登一次并重试**，
    避免每 2 小时就要手工重新登录。
    """
    _hd = _load_hcm_direct_core()

    # 允许请求级覆盖网关（如切换到可达的 HCM 部署），否则用配置文件中的 proxy_target。
    effective_target = (getattr(req, "target", "") or "").strip() or _HCM_PROXY_TARGET
    if not effective_target:
        # 缺配置属用户可预期错误 → 400（与 UserError 约定一致，回归测试也依赖非 500）
        raise HTTPException(400, "直连目标未配置（hcm_whitelist.local.json 的 proxy_target.base_url，或请求传入 target）")

    token = req.token.strip() or _HCM_PRESET_TOKEN
    if not token:
        raise HTTPException(400, "未提供 token 且预设 token 为空，请先配置 token 或传入 token")

    api_name = req.api_name.strip() or "hcm.paas.object.list"
    query = ["debug=1"]
    if getattr(req, "sql_debug", False):
        query.append("sql_debug=1")
    if getattr(req, "profile_debug", False):
        query.append("profile_debug=1")
    target = f"{effective_target.rstrip('/')}/api/{api_name}?{'&'.join(query)}"
    if req.model:
        target += f"&model={req.model}"

    hp = _hd.encrypt_param(req.params)
    body = json.dumps({
        "hcm_transfer_strategy": "ha",
        "hcm_param": hp,
        "s3h": _hd.sign_param(hp),
    }).encode("utf-8")

    # 复用 CF 共享的 SSL 上下文（开启旧式重协商），对 HTTP 目标无害；
    # 若将来 _HCM_PROXY_TARGET 配成 HTTPS legacy 服务器也能兼容。
    from api.cf.cf_tokens import _cf_ssl_context
    ssl_ctx = _cf_ssl_context()

    async def _post_once(tok: str):
        """直连 POST 到目标网关，使用其专属代理（如有）或全局代理。"""
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=ssl_ctx,
            proxy=_request_proxy(effective_target) or None,
        ) as client:
            return await client.post(
                target, content=body,
                headers={"Content-Type": "application/json"},
                cookies={"token": tok} if tok else {},
            )

    async def _auto_relogin(target_url: str) -> str:
        """用 cf_accounts 中该网关的账号密码重登，返回新 token；失败返回空串。"""
        return await _auto_relogin_token(target_url)

    logger.info("[直连] %s → %s body=%dB token=%s", api_name, target, len(body),
                "preset" if not req.token.strip() else "provided")

    try:
        resp = await _post_once(token)
    except httpx.ConnectError as e:
        logger.error("[HCM直连] 无法连接 %s: %s", effective_target, e)
        raise HTTPException(502, f"无法连接 HCM 服务器 {effective_target}: {e}")
    except httpx.TimeoutException:
        logger.error("[HCM直连] 请求超时 %s", target)
        raise HTTPException(504, "连接 HCM 服务器超时")

    raw_text = resp.content.decode("utf-8", "replace")
    logger.info("[HCM直连] 网关响应 %s len=%dB head=%s",
                resp.status_code, len(resp.content), raw_text[:600])

    # token 失效（默认 2h 过期）→ 自动重登一次并重试。
    # 注意 1：部分网关 token 失效/无权限时不返回 errcode 结构，而是
    # 401/403（body 可能为空）——同样先按疑似登录态失效重登重试一次，
    # 重试后仍是 403 则按原样透传（真无权限）。
    # 注意 2：hr-test 部署对无效 token 直接兜底成 HTTP 500 HTML 错误页
    # （非 JSON，errcode 判定落空）——同样纳入疑似失效：重登一次后重试
    # 即可恢复（实测：无效 token → 500 HTML；有效 token → 200 数据）。
    if (_resp_is_token_error(raw_text)
            or resp.status_code in (401, 403)
            or not raw_text.strip()
            or (resp.status_code >= 500 and _is_html_error_page(raw_text))):
        new_tok = await _auto_relogin(effective_target)
        if new_tok and new_tok != token:
            try:
                resp = await _post_once(new_tok)
            except (httpx.ConnectError, httpx.TimeoutException):
                pass  # 重试仍连不上：沿用首次响应走原错误分支
            else:
                raw_text = resp.content.decode("utf-8", "replace")
                token = new_tok
                logger.info("[HCM直连] 自动重登后重试 → %s len=%dB head=%s",
                            resp.status_code, len(resp.content), raw_text[:300])

    if resp.status_code >= 400:
        # 对齐网关自带调试页（hcm-core templates/api/api_ui.html + api.js）：
        # 错误响应也要原样透传（含 traceback / log_index），供前端红色结果块 + 下载接口日志。
        # HTTPException 只留给「非 JSON 异常页」这类无结构信息的场景。
        try:
            err_payload = json.loads(raw_text)
        except json.JSONDecodeError:
            err_payload = None
        if isinstance(err_payload, dict):
            meta = {
                k: err_payload[k]
                for k in ("srv_begin", "srv_end", "profile_index", "log_index", "sql_trace")
                if k in err_payload
            }
            if "srv_begin" in meta and "srv_end" in meta:
                meta["duration_ms"] = meta["srv_end"] - meta["srv_begin"]
            return {
                "ok": False,
                "data": err_payload,
                "error": {
                    "errcode": err_payload.get("errcode"),
                    "errmsg": err_payload.get("errmsg") or err_payload.get("message") or "",
                    "error_code": err_payload.get("error_code"),
                    "traceback": err_payload.get("traceback") or "",
                    "http_status": resp.status_code,
                },
                "meta": meta,
                "gateway": effective_target,
            }
        # 非 JSON 错误页（网关 500 HTML / nginx 错误页 / SSO 跳转页等）：对齐调试页
        # ——responseText 原文进红色结果块，由前端 Text/Code 视图展示（树视图对
        # HTML 无意义，前端按内容自动切换）。HTTPException 只留「连不上」类场景。
        logger.warning("[HCM直连] 网关 HTTP %s 返回非 JSON 错误页（%dB）: %s",
                       resp.status_code, len(raw_text), raw_text[:200])
        return {
            "ok": False,
            "data": raw_text[:500_000],
            "error": {
                "errcode": None,
                "errmsg": f"HCM 网关返回 HTTP {resp.status_code}（非 JSON 响应，见结果原文）",
                "raw": raw_text[:500_000],
                "http_status": resp.status_code,
                "traceback": "",
            },
            "meta": {},
            "gateway": effective_target,
        }

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        # 200 但非 JSON（网关异常页）：同样原文进结果块，不抛 502
        logger.error("[HCM直连] 网关返回非 JSON: %s", raw_text[:800])
        return {
            "ok": False,
            "data": raw_text[:500_000],
            "error": {
                "errcode": None,
                "errmsg": "HCM 网关返回非 JSON 响应（可能网关异常页），见结果原文",
                "raw": raw_text[:500_000],
                "http_status": resp.status_code,
                "traceback": "",
            },
            "meta": {},
            "gateway": effective_target,
        }

    has_result = isinstance(data, dict) and "result" in data
    has_encrypted = isinstance(data, dict) and "hcm_param" in data
    if not has_result and not has_encrypted:
        errcode = data.get("errcode") if isinstance(data, dict) else None
        errmsg = (
            data.get("errmsg") or data.get("message")
            or data.get("description") or raw_text[:300]
        )
        if errcode == 51006:
            hint = "（请重新获取 HCM 登录 Token 后填入）"
        elif errcode == 17003:
            hint = ("（多为 token 失效或不匹配：HCM token 默认仅 2 小时有效，且不可跨服务器复用；"
                    "已尝试用 cf_accounts 中该网关账号自动重登并重试）")
        else:
            hint = ""
        logger.warning("[HCM直连] 网关返回业务错误 errcode=%s msg=%s", errcode, errmsg)
        # 业务错误不再抛 HTTPException：对齐网关调试页行为——错误响应原样返回
        # （红色结果块 + traceback + 「查看明细错误」+ 下载接口日志），由前端统一抛 HcmApiError。
        meta = {
            k: data[k]
            for k in ("srv_begin", "srv_end", "profile_index", "log_index", "sql_trace")
            if k in data
        }
        if "srv_begin" in meta and "srv_end" in meta:
            meta["duration_ms"] = meta["srv_end"] - meta["srv_begin"]
        return {
            "ok": False,
            "data": data,
            "error": {
                "errcode": errcode,
                "errmsg": errmsg + hint,
                "error_code": data.get("error_code"),
                "traceback": data.get("traceback") or "",
                "http_status": resp.status_code,
            },
            "meta": meta,
            "gateway": effective_target,
        }

    if has_result:
        result = data["result"]
        logger.info("[HCM直连] 明文响应，直接取 result=%s", type(result).__name__)
    else:
        inner = _hd.decrypt_param(data["hcm_param"], data.get("hcm_transfer_strategy", "hb5"))
        result = inner.get("result", inner) if isinstance(inner, dict) else inner
        logger.info("[HCM直连] 解密成功 result=%s", type(result).__name__)

    meta = {}
    if isinstance(data, dict):
        meta = {
            k: data[k]
            for k in ("srv_begin", "srv_end", "profile_index", "log_index", "sql_trace")
            if k in data
        }
        if "srv_begin" in meta and "srv_end" in meta:
            meta["duration_ms"] = meta["srv_end"] - meta["srv_begin"]
    return {"ok": True, "data": result, "meta": meta, "gateway": effective_target}


async def hcm_decrypt_param(req) -> "dict":
    """调用网关 /api/api_decrypt_param 解密抓包参数（hcm_param → 明文 JSON）。

    关键差异：该 handler **不能走 /api/hcm/direct 的信封通道**。网关的
    DecryptParamHandler 在 `_asynchronous_service` 里先对 body 做一次
    TransferParamDecryptUtil.decrypt_param（会 pop 掉 hcm_transfer_strategy /
    hcm_param），随后 execute 再取这两个键包成信封解第二层——即 handler 语义
    是「解两层」。若把用户参数再套一层传输信封（hcmDirect 的做法），handler
    解完后键已被 pop，execute 取不到 → KeyError 'hcm_transfer_strategy'。

    正确姿势：**明文直发** body {hcm_transfer_strategy, hcm_param, s3h?}，
    其中 hcm_param = encrypt_param(完整信封 {hcm_transfer_strategy,
    hcm_param:<抓包密文>, s3h:<抓包签名>})——即把抓包得到的信封整体再加密
    一层，handler 两层各解一次后还原出业务明文。
    """
    _hd = _load_hcm_direct_core()

    effective_target = (getattr(req, "target", "") or "").strip() or _HCM_PROXY_TARGET
    if not effective_target:
        # 缺配置属用户可预期错误 → 400（与 UserError 约定一致，回归测试也依赖非 500）
        raise HTTPException(400, "直连目标未配置（hcm_whitelist.local.json 的 proxy_target.base_url，或请求传入 target）")
    token = (getattr(req, "token", "") or "").strip() or _HCM_PRESET_TOKEN
    if not token:
        raise HTTPException(400, "未提供 token 且预设 token 为空，请先配置 token 或传入 token")

    captured = (getattr(req, "hcm_param", "") or "").strip()
    if not captured:
        raise HTTPException(400, "未提供要解密的 hcm_param")
    strategy = (getattr(req, "hcm_transfer_strategy", "") or "").strip() or "ha"
    sig = (getattr(req, "s3h", "") or "").strip() or _hd.sign_param(captured)

    # 内层信封：完整还原抓包请求体（解开后仍含 strategy/hcm_param，供 handler 第二层解密）
    inner_envelope = {
        "hcm_transfer_strategy": strategy,
        "hcm_param": captured,
        "s3h": sig,
    }
    outer_hp = _hd.encrypt_param(inner_envelope)
    body = json.dumps({
        "hcm_transfer_strategy": "ha",
        "hcm_param": outer_hp,
        "s3h": _hd.sign_param(outer_hp),
    }).encode()

    target = f"{effective_target.rstrip('/')}/api/api_decrypt_param?debug=1"

    from api.cf.cf_tokens import _cf_ssl_context
    ssl_ctx = _cf_ssl_context()

    async def _post_once(tok: str):
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, verify=ssl_ctx,
            proxy=_request_proxy(effective_target) or None,
        ) as client:
            return await client.post(
                target, content=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                cookies={"token": tok} if tok else {},
            )

    logger.info("[HCM解密] %s body=%dB token=%s", target, len(body),
                "preset" if not (getattr(req, "token", "") or "").strip() else "provided")

    try:
        resp = await _post_once(token)
    except httpx.ConnectError as e:
        logger.error("[HCM解密] 无法连接 %s: %s", effective_target, e)
        raise HTTPException(502, f"无法连接 HCM 服务器 {effective_target}: {e}")
    except httpx.TimeoutException:
        raise HTTPException(504, "连接 HCM 服务器超时")

    raw_text = resp.content.decode("utf-8", "replace")

    def _suspect_auth(raw: str) -> bool:
        """api_decrypt_param 特有：token 过期/缺失**不报** 17003/51006，而是
        handler 内部取登录上下文时炸出 {"code":500,"message":"'NoneType'
        object has no attribute 'keys'"}（实测）。凡非 result 结构的
        errcode/code>=4xx 响应，先按疑似登录态失效重登重试一次；重试后仍
        失败则按原样透传（如真正的 no permission）。"""
        try:
            d = json.loads(raw or "")
        except Exception:
            return False
        if not isinstance(d, dict) or "result" in d:
            return False
        if d.get("errcode") is not None:
            return True
        code = d.get("code")
        return isinstance(code, int) and code >= 400

    if (_resp_is_token_error(raw_text) or _suspect_auth(raw_text)
            or (resp.status_code >= 500 and _is_html_error_page(raw_text))):
        new_tok = await _auto_relogin_token(effective_target)
        if new_tok and new_tok != token:
            try:
                resp = await _post_once(new_tok)
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            else:
                raw_text = resp.content.decode("utf-8", "replace")
                logger.info("[HCM解密] 自动重登后重试 → %s", resp.status_code)

    if resp.status_code >= 400:
        try:
            err_payload = json.loads(raw_text)
        except json.JSONDecodeError:
            err_payload = None
        msg = (err_payload or {}).get("message") or (err_payload or {}).get("errmsg") or raw_text[:300]
        return {
            "ok": False,
            "error": {"errcode": (err_payload or {}).get("errcode"), "errmsg": msg,
                      "http_status": resp.status_code},
            "gateway": effective_target,
        }

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        raise HTTPException(502, f"HCM 解密响应非 JSON: {raw_text[:500]}")

    if isinstance(data, dict) and "result" in data:
        return {"ok": True, "data": data["result"], "gateway": effective_target}
    if isinstance(data, dict) and (data.get("errcode") or (isinstance(data.get("code"), int) and data["code"] >= 400)):
        # 网关业务错误：errcode 结构 或 {code:5xx, message}（api_decrypt_param 实测形态）
        return {"ok": False, "error": {"errcode": data.get("errcode", data.get("code")),
                                       "errmsg": data.get("errmsg") or data.get("message") or ""},
                "gateway": effective_target}
    # 兜底：handler 也可能直接回明文结构
    return {"ok": True, "data": data, "gateway": effective_target}


def hcm_save_data(req) -> "dict":
    """将 HCM 对象数据 JSON 写入本地文件（logs/hcm_data/），返回路径与大小。

    提供给前端「保存 JSON」按钮：后端写盘，前端拿到绝对路径并复制到剪贴板。
    """
    content = getattr(req, "content", None)
    if not content or not str(content).strip():
        raise UserError("无数据可保存")
    export_dir = _PROJECT_ROOT / "logs" / "hcm_data"
    export_dir.mkdir(parents=True, exist_ok=True)
    model = (getattr(req, "model", "") or "unknown").strip()
    safe_model = "".join(c if c.isalnum() or c in "-_." else "_" for c in model)[:80] or "unknown"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"hcm_data_{safe_model}_{ts}.json"
    fpath = export_dir / fname
    try:
        fpath.write_text(str(content), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.exception("[HCM] 数据文件写入失败: %s", e)
        raise RuntimeError(f"写入文件失败: {e}")
    size = fpath.stat().st_size
    logger.info("[HCM] 对象数据已保存: %s (%d bytes)", fpath, size)
    return {"ok": True, "path": str(fpath), "filename": fname, "size": size}


async def hcm_temp_download(req) -> "Response":
    """下载 HCM 网关的临时文件（接口日志 / SQL Profile）。

    为什么必须走后端代理：网关的 ``/document/temp/download?index=...``
    ① 挂在 **HCM 网关** 上，不是本机后端——前端 ``<a href="/document/temp/download...">``
    会打到本机 origin → 404；
    ② 网关靠 **cookie ``token``** 认登录态，跨 origin 直接跳转浏览器不会带上该 cookie，
    会返回「请登录后操作」。

    所以这里由后端带着 token 代取，把字节流回给前端保存（前端拿到 Blob 再落盘）。
    """
    from fastapi.responses import Response
    from urllib.parse import quote

    index = (getattr(req, "index", "") or "").strip()
    if not index:
        raise HTTPException(400, "缺少 index")
    # 与网关侧一致的防目录穿越校验（网关自己也会再校一次）
    if "/" in index or ".." in index:
        raise HTTPException(400, "非法的 index")

    target = (getattr(req, "target", "") or "").strip() or _HCM_PROXY_TARGET
    if not target:
        raise HTTPException(500, "下载目标未配置（请求需传 target，或在 hcm_whitelist 配置 proxy_target.base_url）")
    token = (getattr(req, "token", "") or "").strip() or _HCM_PRESET_TOKEN

    name = (getattr(req, "name", "") or "").strip() or index
    url = (
        f"{target.rstrip('/')}/document/temp/download"
        f"?index={quote(index)}&name={quote(name)}"
    )

    # 复用 CF 共享的 SSL 上下文（开启旧式重协商），HTTP 目标同样适用。
    from api.cf.cf_tokens import _cf_ssl_context

    logger.info("[HCM] 临时文件下载 → %s token=%s", url, "preset" if not getattr(req, "token", "").strip() else "provided")

    async def _get(tok: str):
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True, verify=_cf_ssl_context(),
            proxy=_request_proxy(target) or None,
        ) as client:
            return await client.get(url, cookies={"token": tok} if tok else {})

    def _is_failed(r) -> bool:
        """非文件响应（错误体）判定：4xx/5xx、JSON 错误体、HTML 错误页。

        实测登录失效时网关回 **HTTP 200 + text/html** 错误页
        （"出错啦！error system_errors_msg_00000062"），只判 json 会把它
        当文件下发（前端存出 46 字节"日志"）；正常日志/profile 均为
        application/octet-stream，判 html 为失败安全。
        """
        c = (r.headers.get("content-type") or "").lower()
        return r.status_code >= 400 or "json" in c or "html" in c

    def _is_auth_failure(r) -> bool:
        """登录态失效判定：401/403、JSON errcode 17003/51006、HTML 错误页暗号。"""
        if r.status_code in (401, 403):
            return True
        text = r.content.decode("utf-8", "replace")
        if "json" in (r.headers.get("content-type") or "").lower():
            try:
                d = json.loads(text)
                if isinstance(d, dict) and d.get("errcode") in _TOKEN_ERRCODES:
                    return True
            except Exception:
                pass
        # 实测：token 跨网关不匹配/过期 → 200 + text/html + 该暗号
        return "system_errors_msg_00000062" in text or "请登录" in text

    def _err_msg(r) -> str:
        text = r.content.decode("utf-8", "replace")
        msg = text[:500]
        try:
            j = json.loads(text)
            if isinstance(j, dict):
                msg = j.get("errmsg") or j.get("description") or j.get("message") or msg
        except Exception:
            pass
        return msg

    try:
        resp = await _get(token)
    except httpx.ConnectError as e:
        raise HTTPException(502, f"无法连接 HCM 服务器 {target}: {e}")
    except httpx.TimeoutException:
        raise HTTPException(504, "下载 HCM 临时文件超时")

    # token 失效/跨网关不匹配 → 自动重登一次重试（与 hcm_direct 同策略）。
    # 典型场景：面板未填 token、后端用 hcm_whitelist 的 proxy_target 预设
    # token（只对默认网关有效）下载其它部署的日志 → 网关回 HTML 错误页。
    if _is_failed(resp) and _is_auth_failure(resp):
        new_tok = await _auto_relogin_token(target)
        if new_tok and new_tok != token:
            try:
                resp = await _get(new_tok)
            except (httpx.ConnectError, httpx.TimeoutException):
                pass  # 重试仍连不上：沿用首次响应走错误分支
            else:
                logger.info("[HCM] 临时文件下载自动重登后重试 → %s", resp.status_code)

    if _is_failed(resp):
        msg = _err_msg(resp)
        logger.warning("[HCM] 临时文件下载失败 index=%s http=%s msg=%s", index, resp.status_code, msg)
        raise HTTPException(
            resp.status_code if resp.status_code >= 400 else 400,
            f"HCM 下载失败: {msg}",
        )

    logger.info("[HCM] 临时文件下载成功 index=%s len=%dB", index, len(resp.content))
    # 文件名可能是中文：按 RFC 5987 同时给出 filename*（前端自行命名时该头仅作兜底）。
    disposition = f"attachment; filename*=UTF-8''{quote(name)}"
    return Response(
        content=resp.content,
        media_type=(resp.headers.get("content-type") or "").lower()
        or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )
