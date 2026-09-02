# -*- coding: utf-8 -*-
"""
云函数本地调试：真实 HCM 网关客户端（api/cfdebug/real_client.py）
================================================================
env=test / env=custom 时使用。复刻云函数沙箱里 CustomerUtil 全局对象的语义：
  - call_open_api(api_name, param) 向 {base}/api/{api_name} 发 POST；
  - 返回体取 result 层（与云端一致）；
  - 写接口（edit/create/delete）在 dry_run=True 时只登记意图、不落库，
    用于在不污染测试环境的前提下验证写入逻辑（仅当真实环境且 CF_DB_SAVE 开启才真写）。
"""
import json
import logging
import urllib.request
import urllib.error
import urllib.parse


WRITE_APIS = ("hcm.model.edit", "hcm.model.create", "hcm.model.delete",
              "hcm.model.create.batch", "hcm.model.edit.batch",
              "hcm.model.remove", "hcm.model.remove.batch")


class RealCustomerUtil(object):
    """对接真实 HCM 网关的 CustomerUtil 实现（std 库实现，无第三方依赖）。"""

    def __init__(self, base, token=None, dry_run=True, verbose=False):
        self.base = (base or "").rstrip("/")
        self.token = token
        self.dry_run = dry_run
        self.verbose = verbose
        self.writes = []   # [(api_name, param)] 写意图登记（dry_run 下）

    def _build_url(self, api_name, param):
        url = f"{self.base}/api/{api_name}"
        if isinstance(param, dict) and param.get("model"):
            url += f"?model={urllib.parse.quote(str(param['model']))}"
        return url

    def call_open_api(self, api_name, param=None):
        param = dict(param or {})

        # 写接口：dry_run 默认只登记，不真正请求（保护测试/生产环境）
        if self.dry_run and api_name in WRITE_APIS:
            self.writes.append((api_name, param))
            if self.verbose:
                logging.info("[dry] %s %s", api_name, param)
            return {"success": True, "id": param.get("id_") or (param.get("info") or {}).get("id")}

        url = self._build_url(api_name, param)
        data = json.dumps(param, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Cookie", f"token={self.token}")

        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"{api_name} 失败(HTTP {e.code}): {text[:300]}")
        except Exception as e:  # 网络层错误
            raise RuntimeError(f"{api_name} 请求异常: {e}")

        try:
            d = json.loads(text)
        except Exception:
            raise RuntimeError(f"{api_name} 返回非 JSON: {text[:200]}")

        # 对齐云端语义：返回 result 层
        if isinstance(d, dict) and isinstance(d.get("result"), (dict, list)):
            return d["result"]
        return d
