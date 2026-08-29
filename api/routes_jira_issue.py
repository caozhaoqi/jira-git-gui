# -*- coding: utf-8 -*-
"""
Jira 建单接口 —— 供「HCM 云函数错误定位」面板把定位结论一键转成 Jira issue。

端点
----
    POST /api/jira/issue
    body: { project_key, summary, description, issuetype? }
    resp: { ok, key, id, url }

认证沿用连接设置（core/config/connect.py 的 ConnectConfig）：
  - cookie 模式：Cookie 请求头
  - pat   模式：Basic base64(username:secret)；secret 优先取 PAT 内嵌密钥
                （ConnectionClient._pat_secret），取不到则用 PAT 原文

注意
----
- 复用 api.common 里的全局 client（其 config 由连接设置/会话实时维护），不重复读配置。
- 仅做建单，不读取/改写任何既有 Jira 数据；失败时原样透出网关响应片段便于排查。
"""

import base64

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.common import client
# 认证辅助（_pat_secret / b64_prefix_account）是 ConnectionMixin 的静态方法，
# JiraGitClient 通过多继承复用它，这里直接引用该 mixin。
from core.client.connection import ConnectionMixin

router = APIRouter()


class IssueReq(BaseModel):
    project_key: str
    summary: str
    description: str = ""
    issuetype: str = "任务"


@router.post("/api/jira/issue")
async def api_jira_issue(req: IssueReq):
    cfg = client.config
    if not getattr(cfg, "jira_url", ""):
        raise HTTPException(400, "未配置 Jira URL，请先在「连接设置」里配置并连接")

    project_key = (req.project_key or "").strip()
    summary = (req.summary or "").strip()
    if not project_key or not summary:
        raise HTTPException(400, "project_key 与 summary 不能为空")

    url = f"{cfg.jira_url.rstrip('/')}/rest/api/2/issue"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    mode = (getattr(cfg, "mode", "") or "pat").lower()
    if mode == "cookie" and getattr(cfg, "cookie", ""):
        headers["Cookie"] = cfg.cookie
    elif getattr(cfg, "pat", ""):
        secret = ConnectionMixin._pat_secret(cfg.pat) or cfg.pat
        user = getattr(cfg, "username", "") or ConnectionMixin.b64_prefix_account(cfg.pat) or ""
        token = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"
    else:
        raise HTTPException(400, "未配置 PAT 或 Cookie，请先在「连接设置」里完成连接")

    body = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "description": req.description or "",
            "issuetype": {"name": req.issuetype or "任务"},
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(504, "调用 Jira 建单超时")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"调用 Jira 失败：{e}")

    if r.status_code >= 400:
        # 透出响应片段（含 Jira 的 errorMessages），便于定位是字段/权限还是项目键写错
        raise HTTPException(r.status_code, f"Jira 建单失败 HTTP {r.status_code}: {r.text[:800]}")

    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, f"Jira 返回非 JSON：{r.text[:300]}")

    key = data.get("key")
    return {
        "ok": True,
        "key": key,
        "id": data.get("id"),
        "url": f"{cfg.jira_url.rstrip('/')}/browse/{key}" if key else None,
    }
