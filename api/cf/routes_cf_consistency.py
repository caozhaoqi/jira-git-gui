# -*- coding: utf-8 -*-
"""云函数「线上 ↔ 本地」一致性 —— 路由层（薄 HTTP 适配）。

端点：
  GET  /api/cf-consistency/config     当前网关 / 本地源码根 / 台账目录
  POST /api/cf-consistency/scan       一致性扫描（线上全量 ↔ 本地源码）
  POST /api/cf-consistency/diff       单条并排对比
  POST /api/cf-consistency/pull       线上 → 本地（原文件备份 .bak）
  POST /api/cf-consistency/push       本地 → 线上（**推送前自动归档**，形成回滚点）
  GET  /api/cf-consistency/ledger     部署台账
  GET  /api/cf-consistency/entry      单条台账详情（含归档的 before/after 源码）
  POST /api/cf-consistency/rollback   把线上恢复到某台账条目之前的版本

业务逻辑全部在 api.cf.cf_consistency，这里只做参数接收与异常 → HTTP 映射。
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.common import logger
from api.cf import cf_consistency as C

router = APIRouter()


def _err(e: Exception, status: int = 400) -> HTTPException:
    msg = str(getattr(e, "detail", None) or e)
    logger.warning("[cf一致性] %s", msg)
    return HTTPException(status, msg)


class ScanReq(BaseModel):
    target: str = ""
    token: str = ""
    roots: Optional[List[str]] = None
    with_content: bool = True
    concurrency: int = C.DEFAULT_CONCURRENCY
    category_filter: str = ""


class DiffReq(BaseModel):
    id: str
    local_path: str = ""
    target: str = ""
    token: str = ""
    context: int = 3


class PullReq(BaseModel):
    id: str
    local_path: str = ""
    target: str = ""
    token: str = ""
    create: bool = False
    # 用户在上面板勾选的源码根；None/空 = 用后端默认根（对应前端「可选」字段）
    roots: Optional[List[str]] = None


class PushReq(BaseModel):
    id: str
    local_path: str = ""
    target: str = ""
    token: str = ""
    note: str = ""
    roots: Optional[List[str]] = None


class RollbackReq(BaseModel):
    entry_id: str
    target: str = ""
    token: str = ""


@router.get("/api/cf-consistency/config")
def cf_consistency_config():
    """返回当前配置，供前端初始化表单（不触发任何网关请求）。"""
    try:
        root = C.ledger_root()
        return {
            "ok": True,
            "target": C.default_target(),
            "roots": C.default_local_roots(),
            "ledger_root": str(root),
            "model": C.CF_MODEL,
        }
    except Exception as e:
        raise _err(e, 500)


@router.post("/api/cf-consistency/scan")
async def cf_consistency_scan(req: ScanReq):
    """全量一致性扫描。耗时与已映射条目数成正比（未映射的不拉源码）。"""
    try:
        return await C.scan(
            target=req.target, token=req.token, roots=req.roots,
            with_content=req.with_content, concurrency=req.concurrency,
            category_filter=req.category_filter,
        )
    except Exception as e:
        raise _err(e, 502)


@router.post("/api/cf-consistency/diff")
async def cf_consistency_diff(req: DiffReq):
    try:
        return await C.diff_one(req.id, req.local_path, req.target, req.token, req.context)
    except Exception as e:
        raise _err(e, 502)


@router.post("/api/cf-consistency/pull")
async def cf_consistency_pull(req: PullReq):
    """线上 → 本地。写前把原文件备份为 .bak，动作本身也记入台账。"""
    try:
        return await C.pull(req.id, req.local_path, req.target, req.token, req.create, req.roots)
    except Exception as e:
        raise _err(e, 502)


@router.post("/api/cf-consistency/push")
async def cf_consistency_push(req: PushReq):
    """本地 → 线上。**推送前先把线上当前版本归档**，因此每次都留下回滚点。"""
    try:
        return await C.push(req.id, req.local_path, req.target, req.token, req.note, req.roots)
    except Exception as e:
        raise _err(e, 502)


@router.get("/api/cf-consistency/ledger")
def cf_consistency_ledger(limit: int = 100, id: str = ""):
    try:
        return C.ledger_list(limit=limit, cf_id=id)
    except Exception as e:
        raise _err(e, 500)


@router.get("/api/cf-consistency/entry")
def cf_consistency_entry(entry_id: str = ""):
    if not entry_id:
        raise HTTPException(400, "缺少 entry_id")
    try:
        return {"ok": True, "entry": C.ledger_entry(entry_id)}
    except Exception as e:
        raise _err(e, 404)


@router.post("/api/cf-consistency/rollback")
async def cf_consistency_rollback(req: RollbackReq):
    try:
        return await C.rollback(req.entry_id, req.target, req.token)
    except Exception as e:
        raise _err(e, 502)
