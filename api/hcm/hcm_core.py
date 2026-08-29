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
    _HCM_PROXY_TARGET, _HCM_PRESET_TOKEN,
    get_cf_accounts,
)



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
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=_cf_ssl_context()) as client:
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


async def hcm_direct(req) -> "dict":
    """直连 HCM 网关并解密响应（绕过透传链，全程**不走任何代理**）。

    token 默认 2 小时过期，过期后查询会报 17003/51006。这里在首次响应为
    token 失效类错误时，用 cf_accounts 中该网关的账号密码**自动重登一次并重试**，
    避免每 2 小时就要手工重新登录。
    """
    _hd = _load_hcm_direct_core()

    # 允许请求级覆盖网关（如切换到可达的 HCM 部署），否则用配置文件中的 proxy_target。
    effective_target = (getattr(req, "target", "") or "").strip() or _HCM_PROXY_TARGET
    if not effective_target:
        raise HTTPException(500, "直连目标未配置（hcm_whitelist.local.json 的 proxy_target.base_url，或请求传入 target）")

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
        """直连 POST 到目标网关（**不配置 proxy，不使用代理**）。"""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, verify=ssl_ctx) as client:
            return await client.post(
                target, content=body,
                headers={"Content-Type": "application/json"},
                cookies={"token": tok} if tok else {},
            )

    async def _auto_relogin(target_url: str) -> str:
        """用 cf_accounts 中该网关的账号密码重登，返回新 token；失败返回空串。"""
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

    # token 失效（默认 2h 过期）→ 自动重登一次并重试
    if _resp_is_token_error(raw_text):
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
        raise HTTPException(resp.status_code, f"HCM 网关 {effective_target} 返回 HTTP {resp.status_code}: {raw_text[:1500]}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("[HCM直连] 网关返回非 JSON: %s", raw_text[:800])
        raise HTTPException(502, f"HCM 直连响应非 JSON（可能网关异常页）: {raw_text[:800]}")

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
        raise HTTPException(
            200 if errcode else resp.status_code,
            f"HCM 网关错误 {errcode or ''}: {errmsg}{hint}",
        )

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
            for k in ("srv_begin", "srv_end", "profile_index", "log_index")
            if k in data
        }
        if "srv_begin" in meta and "srv_end" in meta:
            meta["duration_ms"] = meta["srv_end"] - meta["srv_begin"]
    return {"ok": True, "data": result, "meta": meta, "gateway": effective_target}


def hcm_save_data(req) -> "dict":
    """将 HCM 对象数据 JSON 写入本地文件（logs/hcm_data/），返回路径与大小。

    提供给前端「保存 JSON」按钮：后端写盘，前端拿到绝对路径并复制到剪贴板。
    """
    content = getattr(req, "content", None)
    if not content or not str(content).strip():
        raise ValueError("无数据可保存")
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
