"""Application-wide HTTP/HTTPS proxy configuration."""
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from core.app_paths import get_data_root
from core.errors import UserError

_SETTINGS_FILE = get_data_root() / "config" / "network.local.json"
_LOCK = threading.Lock()
_INITIAL_PROXY_ENV = {
    key: os.environ.get(key)
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")
}


def normalize_http_proxy(value: str) -> str:
    """Validate an optional HTTP(S) proxy URL without embedded credentials."""
    proxy = (value or "").strip()
    if not proxy:
        return ""
    try:
        parsed = urlsplit(proxy)
        _ = parsed.port
    except ValueError as ex:
        raise UserError(f"代理地址无效：{ex}") from ex
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port is None
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise UserError(
            "代理地址请填写 http://host:port 或 https://host:port，"
            "暂不支持代理认证或带路径的地址。")
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_http_proxy(service_proxy: str = "", request_proxy: str = "") -> str:
    """Return the explicit request proxy first, then the server-specific proxy."""
    return normalize_http_proxy(
        request_proxy if (request_proxy or "").strip() else service_proxy
    )


def get_global_http_proxy() -> str:
    """Return the persisted global proxy URL, or empty when not configured."""
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as ex:
        raise UserError(f"读取全局网络配置失败：{ex}") from ex
    if not isinstance(data, dict):
        raise UserError("全局网络配置格式无效")
    return normalize_http_proxy(str(data.get("http_proxy") or ""))


def apply_global_http_proxy() -> str:
    """Apply the saved proxy to standard-library clients and return its URL."""
    proxy = get_global_http_proxy()
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        if proxy:
            os.environ[key] = proxy
        else:
            original = _INITIAL_PROXY_ENV[key]
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
    return proxy


def set_global_http_proxy(value: str) -> str:
    """Persist and immediately apply the global proxy setting."""
    proxy = normalize_http_proxy(value)
    data = {"http_proxy": proxy}
    with _LOCK:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_FILE.with_suffix(_SETTINGS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _SETTINGS_FILE)
        try:
            os.chmod(_SETTINGS_FILE, 0o600)
        except OSError:
            pass
    apply_global_http_proxy()
    return proxy
