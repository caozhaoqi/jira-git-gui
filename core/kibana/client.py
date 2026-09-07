# -*- coding: utf-8 -*-
"""Kibana Console Proxy 客户端。

背景（实测，勿轻易改通道）
--------------------------
目标环境 ES 与 Kibana 分离部署：ES 在集群内网（10.233.66.39:9200）本机不可达，
``/elasticsearch/`` 又是 HCM 的 nginx SPA fallback（返回 HTML 首页而非 ES 反代）。
唯一可用通道是 Kibana 7.17 自带的 Console Proxy::

    POST <base_url>/api/console/proxy?path=<es 路径>&method=<GET|POST>
    Header: kbn-xsrf: true
    Auth  : Basic <username:password>
    Body  : ES 请求体（method=POST 时）

它把请求原样转发给 ES 并把响应体透传回来（含 ES 自己的 4xx JSON 错误体）。

已知性能特征
------------
该代理每次请求有 **~4s 的固定开销**（连 ``_cluster/health`` 也要 4s，ES 侧
本身只要个位数毫秒）。因此上层必须：

- 元数据（命名空间 / 容器 / Pod / 应用 / 节点枚举）走 TTL 缓存；
- 同一批次的多个独立查询用线程池并发，而不是串行叠加。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx

from core.errors import UserError

__all__ = ["KibanaError", "KibanaClient", "get_client"]


class KibanaError(UserError):
    """Kibana / ES 返回的错误，或网络层失败。

    归类为 ``UserError``：绝大多数情况是「站点评错、账号过期、索引模式不存在、
    时间范围无数据」这类用户可修正的问题，不该被全局处理器兜底成 500。
    """


class KibanaClient:
    """单个 Kibana 站点的会话客户端。

    线程安全：内部持有一个 ``httpx.Client``（连接池），多线程并发调用 OK。
    """

    def __init__(self, site: Dict[str, Any]):
        base = str(site.get("base_url") or "").strip().rstrip("/")
        if not base:
            raise KibanaError("Kibana 地址为空")
        self.base_url = base
        self.username = str(site.get("username") or "")
        self.password = str(site.get("password") or "")
        self.index_pattern = str(site.get("index_pattern") or "logstash-*")
        self.time_field = str(site.get("time_field") or "es_time")
        self.msg_field = str(site.get("msg_field") or "log")
        self.field_prefix = str(site.get("field_prefix") or "kubernetes").strip(".")
        self.timeout = float(site.get("timeout") or 30)
        self.verify_ssl = bool(site.get("verify_ssl"))
        self._client: Optional[httpx.Client] = None
        self._lock = __import__("threading").Lock()

    # ------------------------------------------------------------------ 生命周期
    @property
    def proxy_url(self) -> str:
        return f"{self.base_url}/api/console/proxy"

    def _http(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    auth = httpx.BasicAuth(self.username, self.password) \
                        if (self.username or self.password) else None
                    self._client = httpx.Client(
                        auth=auth,
                        verify=self.verify_ssl,
                        timeout=self.timeout,
                        follow_redirects=True,
                        headers={"kbn-xsrf": "true",
                                 "Content-Type": "application/json"},
                    )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # -------------------------------------------------------------------- 字段
    def f(self, name: str, keyword: bool = False) -> str:
        """按 ``field_prefix`` 拼出完整字段名，如 ``kubernetes.pod_name.keyword``。"""
        base = f"{self.field_prefix}.{name}" if self.field_prefix else name
        return f"{base}.keyword" if keyword else base

    # ------------------------------------------------------------------ 请求层
    @staticmethod
    def _raise_for(resp: httpx.Response, body: Any) -> None:
        """把非 2xx 响应转成 ``KibanaError``（兼容 Kibana 与 ES 两套错误体）。"""
        if resp.status_code < 400:
            return
        if isinstance(body, dict):
            # ES 错误体：{"error": {"type":..., "reason":...}} 或 {"error": "..."}
            err = body.get("error")
            if isinstance(err, dict):
                reason = err.get("reason") or err.get("type") or ""
                root = (err.get("root_cause") or [{}])[0]
                reason = reason or root.get("reason") or ""
                raise KibanaError(f"ES {resp.status_code}: {reason}")
            if isinstance(err, str) and err:
                raise KibanaError(f"ES {resp.status_code}: {err}")
            # Kibana 错误体：{"statusCode":..,"error":..,"message":..}
            if body.get("message"):
                raise KibanaError(f"Kibana {resp.status_code}: {body['message']}")
        raise KibanaError(f"HTTP {resp.status_code}: {(resp.text or '')[:200]}")

    def call_es(self, path: str, method: str = "GET",
                body: Optional[dict] = None) -> Any:
        """经 Console Proxy 调用一次 ES API，返回已解析的 JSON。

        ``path`` 是 ES 侧路径（可带查询串），如 ``logstash-*/_search``、
        ``_cluster/health``。注意 **不要** 以 ``/`` 开头。
        """
        params = {"path": path.lstrip("/"), "method": method.upper()}
        try:
            resp = self._http().post(self.proxy_url, params=params, json=body)
        except httpx.TimeoutException:
            raise KibanaError(
                f"请求 Kibana 超时（{self.timeout:g}s）：{path}。"
                "该代理单次固定开销约 4s，可适当调大站点超时。")
        except httpx.HTTPError as ex:
            raise KibanaError(f"无法连接 Kibana（{self.base_url}）：{ex}")

        text = resp.text or ""
        parsed: Any
        try:
            parsed = resp.json()
        except Exception:  # noqa: BLE001
            parsed = {"_raw": text}

        self._raise_for(resp, parsed)
        return parsed

    def es_search(self, dsl: dict, index: str = None) -> dict:
        """对站点默认索引模式执行一次 ``_search``。"""
        idx = index or self.index_pattern
        return self.call_es(f"{idx}/_search", "POST", dsl)

    # ------------------------------------------------------------------ 便捷方法
    def kibana_status(self) -> dict:
        """Kibana 自身状态（含版本号）。不需要 ES 权限即可读。"""
        try:
            resp = self._http().get(f"{self.base_url}/api/status")
        except httpx.HTTPError as ex:
            raise KibanaError(f"无法连接 Kibana（{self.base_url}）：{ex}")
        data = {}
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            pass
        return {
            "http_status": resp.status_code,
            "kibana_version": (data.get("version") or {}).get("number", ""),
            "overall": ((data.get("status") or {}).get("overall") or {}).get("state", ""),
            "message": (data.get("status") or {}).get("overall", {}).get("title", "")
            if resp.status_code < 400 else (data.get("message") or ""),
        }

    def cluster_health(self) -> dict:
        data = self.call_es("_cluster/health", "GET")
        return {
            "cluster_name": data.get("cluster_name", ""),
            "status": data.get("status", ""),
            "nodes": data.get("number_of_nodes", 0),
            "active_shards": data.get("active_shards", 0),
        }

    def index_patterns(self) -> list:
        """列出 Kibana 里保存的索引模式（title + 时间字段）。"""
        try:
            resp = self._http().get(
                f"{self.base_url}/api/saved_objects/_find",
                params={"type": "index-pattern", "per_page": 100},
            )
        except httpx.HTTPError as ex:
            raise KibanaError(f"无法读取索引模式：{ex}")
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return []
        out = []
        for so in data.get("saved_objects") or []:
            attrs = so.get("attributes") or {}
            out.append({
                "id": so.get("id", ""),
                "title": attrs.get("title", ""),
                "time_field": attrs.get("timeFieldName", ""),
            })
        return out


# --------------------------------------------------------------------------- #
#  客户端缓存：同一站点配置复用一个 httpx 连接池
# --------------------------------------------------------------------------- #
_clients: Dict[Tuple[str, str, str], KibanaClient] = {}
_clients_lock = __import__("threading").Lock()


def get_client(site_name: str = None, refresh: bool = False) -> KibanaClient:
    """按站点名取（或新建）客户端。账号/地址变了会自动重建。"""
    from .config import get_site  # 延迟导入，避免与 config 形成导入环

    name, site = get_site(site_name)
    key = (name, str(site.get("base_url") or ""), str(site.get("username") or ""))
    with _clients_lock:
        cli = _clients.get(key)
        if refresh and cli is not None:
            cli.close()
            _clients.pop(key, None)
            cli = None
        if cli is None:
            cli = KibanaClient(site)
            # 同站点其它 key（配置变了）留下的旧连接池一并关掉
            for k in [k for k in _clients if k[0] == name]:
                _clients.pop(k).close()
            _clients[key] = cli
        else:
            # 同步可能被就地修改过的字段（密码/超时等）
            cli.password = str(site.get("password") or "")
            cli.timeout = float(site.get("timeout") or 30)
            cli.verify_ssl = bool(site.get("verify_ssl"))
            cli.index_pattern = str(site.get("index_pattern") or cli.index_pattern)
            cli.time_field = str(site.get("time_field") or cli.time_field)
            cli.msg_field = str(site.get("msg_field") or cli.msg_field)
            cli.field_prefix = str(site.get("field_prefix") or cli.field_prefix)
        return cli


def close_all_clients() -> None:
    with _clients_lock:
        for cli in _clients.values():
            cli.close()
        _clients.clear()
