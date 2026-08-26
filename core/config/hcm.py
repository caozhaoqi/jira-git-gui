# -*- coding: utf-8 -*-
"""配置加载子模块（由 core/config.py 拆分，保持 import 兼容）。"""
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from core.app_paths import get_data_root
from core.models import ConnectConfig

_BASE = get_data_root()
_SESSION_FILE = _BASE / ".session.json"

from .connect import _env_search_roots

def load_hcm_whitelist(project_root: "Optional[Path]" = None) -> "dict":
    """读取 平台连接业务白名单。

    白名单项（改了会连不上平台）：
      - hcminner:           内部 OpenAPI 鉴权头 {header, value}
      - model_list_api:     真实日志查询接口路径（POST，拼在 server_url 之后）
      - reference_projects: 参考项目名（cloud-vue / core），合并比对识别用
      - platform_hosts:     真实平台域名白名单（占位，见 .local 覆盖）
      - proxy_target:       同源代理目标网关基址（占位，见 .local 覆盖）

    加载顺序（后者覆盖前者，敏感值优先来自 .local）：
      1) 内置 defaults（占位，无真实 IP/域名，可安全提交）
      2) config/hcm_whitelist.json（跟踪模板，敏感字段为占位符）
      3) config/hcm_whitelist.local.json（本机真实值，**已 gitignore，不入库**）

    注意：含真实服务器 IP / 域名的连接信息只允许存在于 *.local.json，
    该文件已被 .gitignore 忽略，请勿将真实值写回跟踪的 hcm_whitelist.json。
    找不到文件或解析失败时回退到内置默认值，保证服务不因配置缺失中断。
    """
    defaults = {
        "hcminner": {"header": "hcminner", "value": "1"},
        "model_list_api": {"path": "/api/hcm.model.list"},
        "reference_projects": {"names": ["cloud-vue", "core"]},
        "platform_hosts": {
            "hosts": []
        },
        "proxy_target": {"base_url": ""},
    }
    roots = _env_search_roots(project_root)
    merged = {k: dict(v) for k, v in defaults.items()}
    for root in roots:
        candidates = [
            root / "hcm_whitelist.json",
            root / "config" / "hcm_whitelist.json",
            root / "hcm_whitelist.local.json",
            root / "config" / "hcm_whitelist.local.json",
        ]
        for p in candidates:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                for k, v in data.items():
                    if k in merged and isinstance(v, dict) and isinstance(merged[k], dict):
                        merged[k].update(v)
                    else:
                        merged[k] = v
    # 环境变量最终覆盖（便于容器/CI 注入，不落盘）
    env_target = os.environ.get("HCM_PROXY_TARGET", "").strip()
    if env_target:
        merged.setdefault("proxy_target", {})["base_url"] = env_target
    env_hosts = os.environ.get("HCM_PLATFORM_HOSTS", "").strip()
    if env_hosts:
        merged.setdefault("platform_hosts", {})["hosts"] = [
            h.strip() for h in env_hosts.split(",") if h.strip()
        ]
    return merged

