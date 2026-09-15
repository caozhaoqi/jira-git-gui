#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取 HCM 系统运行日志（SysLogRecord）的命令行工具。

复用项目已有的 CF token 缓存 / 刷新 / SSL 配置（api.cf.cf_tokens / api.cf.cf_login），
直接打 HCM OpenAPI ``hcm.model.list?model=SysLogRecord``。

与「云函数日志」(dynamic_log) 走的是同一个接口，只是 model 换成 SysLogRecord。
注意 SysLogRecord **没有** log_type 字段，过滤请改用 dimension / category / status / date_。

用法示例：
  # 查某天系统日志（token 自动从缓存/刷新取）
  python tools/hcm_sys_log.py -s https://e1jw8.hcmcloud.cn -d 2026-09-15

  # 用 cf_accounts 里的服务器名（自动解析 URL），只看 open_api 维度且 ERROR
  python tools/hcm_sys_log.py -s 京投 -d 2026-09-15 --dimension open_api --status ERROR

  # 导出原始 JSON
  python tools/hcm_sys_log.py -s https://e1jw8.hcmcloud.cn -d 2026-09-15 --json syslog.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from urllib.parse import urlparse

# 让脚本可直接 `python tools/hcm_sys_log.py` 运行：把项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from api.cf.cf_tokens import (
    TOKEN_CACHE_LOCK,
    _CF_TOKEN_CACHE,
    _cf_ssl_context,
    _HCM_MODEL_LIST_API,
    _HCM_HCMINNER_HEADER,
    _HCM_HCMINNER_VALUE,
)
from api.cf.cf_login import cf_refresh_token


RECORD_MODEL = "SysLogRecord"


def _resolve_server(server: str) -> str:
    """server 可以是完整 URL，也可以是 cf_accounts 里的 name / key（自动解析出 URL）。"""
    if urlparse(server).scheme:
        return server.strip().rstrip("/")
    try:
        from core.config.cf import load_cf_accounts
        for acc in (load_cf_accounts() or []):
            name = (acc.get("name") or "")
            key = "cf:" + (acc.get("server_url") or "").strip().rstrip("/")
            if server in (name, key, acc.get("server_url")):
                return (acc.get("server_url") or "").strip().rstrip("/")
    except Exception:
        pass
    raise SystemExit(f"无法解析服务器 '{server}'，请直接传完整 URL（https://...）")


def _load_token(server_url: str, proxy: str):
    """从缓存取 token/cookie；没有或为空则现刷一次。"""
    with TOKEN_CACHE_LOCK:
        cached = _CF_TOKEN_CACHE.get(server_url.rstrip("/"))
    cached = cached if isinstance(cached, dict) else {}
    token = (cached.get("token") or "").strip()
    cookie = (cached.get("cookie") or "").strip()
    if not (token or cookie):
        fresh = asyncio.run(cf_refresh_token(server_url, proxy))
        if isinstance(fresh, dict):
            token = (fresh.get("token") or "").strip()
            cookie = (fresh.get("cookie") or "").strip()
    cookie = cookie or (f"token={token}" if token else "")
    return token, cookie


def _build_attempts(token: str, cookie: str):
    base = {"Content-Type": "application/json"}
    return [
        {"name": "cookie", "headers": {**base, "Cookie": cookie}},
        {"name": "bearer_hcminner",
         "headers": {**base, "Authorization": f"Bearer {token}",
                     _HCM_HCMINNER_HEADER: _HCM_HCMINNER_VALUE}},
        {"name": "header_token", "headers": {**base, "token": token}},
    ]


def _is_session_err(status: int, errcode, errmsg: str) -> bool:
    msg = (errmsg or "").lower()
    return status in (401, 403) or "17003" in msg or ("token" in msg and ("expire" in msg or "invalid" in msg))


async def _fetch_once(server_url, proxy, token, cookie, payload):
    """三种鉴权依次尝试；返回 (result, need_refresh, err)。"""
    attempts = _build_attempts(token, cookie)
    last_err = "未知错误"
    for att in attempts:
        try:
            kw = dict(timeout=30, follow_redirects=True, verify=_cf_ssl_context())
            if proxy:
                kw["proxy"] = proxy
            else:
                # 显式 transport 会忽略顶层 verify，需在 transport 也传 ssl 上下文
                kw["transport"] = httpx.AsyncHTTPTransport(verify=_cf_ssl_context())
            async with httpx.AsyncClient(**kw) as client:
                resp = await client.post(
                    server_url + _HCM_MODEL_LIST_API, json=payload, headers=att["headers"]
                )
                if resp.status_code >= 400:
                    body = resp.text[:300]
                    last_err = f"[{att['name']}] HTTP {resp.status_code}: {body}"
                    if _is_session_err(resp.status_code, None, body):
                        return None, True, last_err
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    last_err = f"[{att['name']}] 返回非JSON: {resp.text[:300]}"
                    continue
                if isinstance(data, dict) and data.get("errcode") not in (None, 0):
                    msg = data.get("errmsg") or data.get("description") or str(data)[:300]
                    last_err = f"[{att['name']}] 业务失败: {msg}"
                    if _is_session_err(0, data.get("errcode"), msg):
                        return None, True, last_err
                    continue
                result = data.get("result", data) if isinstance(data, dict) else data
                return result, False, ""
        except Exception as e:  # noqa: BLE001
            last_err = f"[{att['name']}] {type(e).__name__}: {e}"
            continue
    return None, False, last_err


async def query(server_url, proxy, token, cookie, payload, max_pages):
    all_rows = []
    page = payload["page_index"]
    refreshed = False
    while True:
        p = {**payload, "page_index": page}
        result, need_refresh, err = await _fetch_once(server_url, proxy, token, cookie, p)
        if result is not None:
            rows = (result.get("list") or []) if isinstance(result, dict) else []
            all_rows.extend(rows)
            if not rows or len(rows) < payload["page_size"]:
                break
            page += 1
            if max_pages and page > max_pages:
                break
            continue
        # result 为 None
        if need_refresh and not refreshed:
            fresh = await cf_refresh_token(server_url, proxy)
            if isinstance(fresh, dict) and (fresh.get("token") or fresh.get("cookie")):
                token = (fresh.get("token") or "").strip()
                cookie = (fresh.get("cookie") or "").strip() or f"token={token}"
                refreshed = True
                continue
        raise RuntimeError(f"查询失败: {err}")
    return all_rows


def main():
    ap = argparse.ArgumentParser(description="拉取 HCM 系统运行日志 (SysLogRecord)")
    ap.add_argument("-s", "--server", required=True,
                    help="HCM 服务器 URL，或 cf_accounts 里的 name/key（自动解析 URL）")
    ap.add_argument("-d", "--date", default=date.today().isoformat(),
                    help="分片日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--dimension",
                    help="记录维度: open_api/hcm_outer/login/message/hcm_robot/third_err/mail_setting/periodic_task")
    ap.add_argument("--category", help="所属模块")
    ap.add_argument("--status", help="状态: SUCCESS/ERROR")
    ap.add_argument("--keyword", help="对 description+object 做客户端关键字过滤")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--max-pages", type=int, default=0, help="0=不限（一直取到完）")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--token", default="", help="手动传 token（留空则用缓存/自动刷新）")
    ap.add_argument("--json", metavar="PATH", help="把原始结果导出为 JSON 文件")
    args = ap.parse_args()

    server_url = _resolve_server(args.server)
    if args.token.strip():
        token, cookie = args.token.strip(), f"token={args.token.strip()}"
    else:
        token, cookie = _load_token(server_url, args.proxy)

    filter_dict: dict = {"date_": args.date}
    if args.dimension:
        filter_dict["dimension"] = args.dimension
    if args.category:
        filter_dict["category"] = args.category
    if args.status:
        filter_dict["status"] = args.status
    payload = {
        "model": RECORD_MODEL,
        "page_index": 1,
        "page_size": args.page_size,
        "filter_dict": filter_dict,
    }

    rows = asyncio.run(query(server_url, args.proxy, token, cookie, payload, args.max_pages))
    if args.keyword:
        kw = args.keyword.lower()
        rows = [r for r in rows
                if kw in ((r.get("description") or "") + (r.get("object") or "")).lower()]

    print(f"# 服务器: {server_url}")
    print(f"# 日期: {args.date}  过滤: {filter_dict}  命中: {len(rows)} 条")
    header = ["operate_time", "dimension", "category", "status", "ip", "emp_name", "description"]
    print("\t".join(header))
    for r in rows:
        desc = (r.get("description") or "").replace("\n", " ").replace("\t", " ")[:80]
        print("\t".join([
            str(r.get("operate_time") or ""),
            str(r.get("dimension") or ""),
            str(r.get("category") or ""),
            str(r.get("status") or ""),
            str(r.get("ip") or ""),
            str(r.get("emp_name") or ""),
            desc,
        ]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "server_url": server_url,
                "date": args.date,
                "filter_dict": filter_dict,
                "count": len(rows),
                "rows": rows,
            }, f, ensure_ascii=False, indent=2)
        print(f"# 已导出 JSON: {args.json}")


if __name__ == "__main__":
    main()
