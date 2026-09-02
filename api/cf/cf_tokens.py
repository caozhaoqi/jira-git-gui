# -*- coding: utf-8 -*-
"""CF 云函数日志 —— Token 缓存与验证码（底层共享模块）。

承载：
- 常量（来自 hcm_whitelist 配置）
- token 缓存：内存字典 ``_CF_TOKEN_CACHE`` + 落盘 ``config/cf_tokens.local.json``
- 验证码缓存 / 获取（``cf_captcha_*``）
- 通用工具：``sniff_image_type`` / ``new_cf_client``

业务逻辑层其它模块（cf_login / cf_logs）均依赖本模块的共享状态。
"""
import json
import os
import ssl
import time
import secrets
import base64
import threading
from pathlib import Path

import httpx

from api.common import (
    logger,
    _PROJECT_ROOT, _HCM_WL,
    _cf_is_session_err, _cf_token_stale,
    get_cf_accounts,
)

# --------------------------------------------------------------------------- #
#  常量（来自 hcm_whitelist 配置）
# --------------------------------------------------------------------------- #
_HCM_MODEL_LIST_API = (_HCM_WL.get("model_list_api", {}) or {}).get(
    "path", "/baseservices/openapi/v1/model/list")
_HCM_HCMINNER_HEADER = (_HCM_WL.get("hcminner", {}) or {}).get("header", "hcminner")
_HCM_HCMINNER_VALUE = (_HCM_WL.get("hcminner", {}) or {}).get("value", "1")

# --------------------------------------------------------------------------- #
#  全局缓存状态
# --------------------------------------------------------------------------- #
_CF_CAPTCHA_CACHE: dict = {}
_CF_CAPTCHA_TTL: dict = {}
_CF_CAPTCHA_MAX = 200

# 并发锁：cf_login / cf_refresh_token / cf_autologin_all 是 async 协程，会在 await 处
# 交错；token 缓存在保存（json.dumps 迭代）与并发读写时可能触发
# “dictionary changed size during iteration”。统一用 RLock 串行化——
# RLock 可重入，避免“保存函数内部再次加锁”导致的同协程死锁；临界区均不含 await，
# 不会阻塞事件循环。验证码缓存同理（清理/写入并行）。
TOKEN_CACHE_LOCK = threading.RLock()
CAPTCHA_CACHE_LOCK = threading.RLock()

_CF_TOKENS_FILE = _PROJECT_ROOT / "config" / "cf_tokens.local.json"


def _cf_tokens_load() -> "dict":
    try:
        if _CF_TOKENS_FILE.exists():
            d = json.loads(_CF_TOKENS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _cf_tokens_save() -> None:
    """原子写：先写临时文件再用 os.replace 覆盖，避免进程崩溃/并发时留下半截文件。"""
    try:
        _CF_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TOKEN_CACHE_LOCK:
            content = json.dumps(_CF_TOKEN_CACHE, ensure_ascii=False, indent=2)
        tmp = _CF_TOKENS_FILE.with_suffix(".local.json.tmp")
        tmp.write_text(content, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _CF_TOKENS_FILE)
        try:
            os.chmod(_CF_TOKENS_FILE, 0o600)
        except OSError:
            pass
    except Exception as e:
        logger.warning(f"[CF] 持久化 token 缓存失败: {e}")


# 启动时从磁盘恢复已缓存的 token（非首次启动直接复用，不重复登录）
_CF_TOKEN_CACHE: "dict[str, dict]" = _cf_tokens_load()


# --------------------------------------------------------------------------- #
#  工具函数
# --------------------------------------------------------------------------- #
def sniff_image_type(data: bytes) -> "str | None":
    """按 magic bytes 判定图片真实类型（CF 验证码端点常错标 content-type）。"""
    if not data or len(data) < 4:
        return None
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _cf_ssl_context() -> "ssl.SSLContext":
    """CF 云函数服务器（如 rcbhlj 旧版 HCM）TLS 栈要求「旧式重协商」，

    而 OpenSSL 3.x 默认拒绝，握手会报
    ``[SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED] unsafe legacy renegotiation disabled``。

    OpenSSL 3.x 已移除 ``OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION``，改用
    ``OP_LEGACY_SERVER_CONNECT``（允许连接/重协商到遗留服务器）。旧版 Python/OpenSSL
    仍暴露前者，这里两个都尝试叠加（取非零值），**仍执行证书校验**（不关闭 verify）。
    """
    ctx = ssl.create_default_context()
    for name in ("OP_LEGACY_SERVER_CONNECT", "OP_ALLOW_UNSAFE_LEGACY_RENEGOTIATION"):
        opt = getattr(ssl, name, 0)
        if opt:
            ctx.options |= opt
    return ctx


def new_cf_client(req_proxy: str, existing_cookies=None):
    """创建 httpx 客户端；existing_cookies 可选 CookieJar（验证码→登录同会话）。"""
    ctx = _cf_ssl_context()
    kwargs = dict(timeout=15, follow_redirects=True, verify=ctx)
    if req_proxy:
        kwargs["proxy"] = req_proxy
    else:
        # 显式 transport 会忽略客户端的 verify=，必须在这里也传入 ssl 上下文
        kwargs["transport"] = httpx.AsyncHTTPTransport(verify=ctx)
    if existing_cookies is not None:
        kwargs["cookies"] = existing_cookies
    return httpx.AsyncClient(**kwargs)


# --------------------------------------------------------------------------- #
#  验证码
# --------------------------------------------------------------------------- #
def cf_captcha_cleanup_expired() -> None:
    """清理过期的 captcha 缓存（3 分钟 TTL）。"""
    with CAPTCHA_CACHE_LOCK:
        now = time.time()
        for cid in list(_CF_CAPTCHA_TTL.keys()):
            if now - _CF_CAPTCHA_TTL[cid] > 180:
                _CF_CAPTCHA_CACHE.pop(cid, None)
                _CF_CAPTCHA_TTL.pop(cid, None)
        if len(_CF_CAPTCHA_CACHE) > _CF_CAPTCHA_MAX:
            oldest = sorted(_CF_CAPTCHA_TTL, key=_CF_CAPTCHA_TTL.get)
            for cid in oldest[:100]:
                _CF_CAPTCHA_CACHE.pop(cid, None)
                _CF_CAPTCHA_TTL.pop(cid, None)


def cf_captcha_new(proxy: str = "") -> "dict":
    """获取 CF 登录图片验证码，返回 {captcha_id, image_code_index, image}。"""
    base = ""  # 由 routes 层传入，这里仅做占位声明（见 cf_captcha_fetch）
    # 实际实现见 cf_captcha_fetch（需要 req.server_url）
    raise NotImplementedError


async def cf_captcha_fetch(server_url: str, proxy: str = "") -> "dict":
    """获取 CF 登录图片验证码（异步网络请求）。"""
    if not server_url:
        raise ValueError("请先配置服务器地址")
    base = server_url.rstrip("/")
    url = f"{base}/img/imagevalidatecode"

    with CAPTCHA_CACHE_LOCK:
        cf_captcha_cleanup_expired()
        if len(_CF_CAPTCHA_CACHE) > _CF_CAPTCHA_MAX:
            oldest = sorted(_CF_CAPTCHA_TTL, key=_CF_CAPTCHA_TTL.get)
            for cid in oldest[:100]:
                _CF_CAPTCHA_CACHE.pop(cid, None)
                _CF_CAPTCHA_TTL.pop(cid, None)

    captcha_id = secrets.token_urlsafe(12)
    image_code_index = secrets.token_hex(4)
    try:
        jar = httpx.Cookies()
        async with new_cf_client(proxy, existing_cookies=jar) as client:
            try:
                await client.get(f"{base}/login")
            except Exception:
                pass
            resp = await client.get(url, params={"index": image_code_index, "v": secrets.token_hex(4)})
            resp.raise_for_status()
            if not resp.content or len(resp.content) < 10:
                raise ValueError("服务器未返回验证码图片")
            ctype = sniff_image_type(resp.content)
            if ctype is None:
                snippet = resp.content[:200].decode("utf-8", "ignore")
                raise ValueError(f"服务器未返回有效图片验证码，响应前200字符: {snippet}")
            b64 = base64.b64encode(resp.content).decode("ascii")
            with CAPTCHA_CACHE_LOCK:
                _CF_CAPTCHA_CACHE[captcha_id] = {"jar": jar.jar, "index": image_code_index}
                _CF_CAPTCHA_TTL[captcha_id] = time.time()
            return {
                "captcha_id": captcha_id,
                "image_code_index": image_code_index,
                "image": f"data:{ctype};base64,{b64}",
            }
    except httpx.ConnectError as e:
        raise ConnectionError(f"无法连接服务器 {base}: {e}")
    except httpx.TimeoutException:
        raise TimeoutError("获取验证码超时")
    except ValueError:
        raise
    except Exception as e:
        logger.exception(f"[CF] 获取验证码异常: {e}")
        raise RuntimeError(f"获取验证码失败: {type(e).__name__}: {e}")
