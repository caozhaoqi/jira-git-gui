# -*- coding: utf-8 -*-
"""云函数「线上 ↔ 本地」一致性看板 + 部署台账（api/cf/cf_consistency.py）
================================================================================
为什么需要
----------
云函数部署**绕过了 CI/CD**：`.gitlab-ci.yml` 的 deploy 阶段只推 4 个镜像
（hcm-core / hcm-celery / hcm-celery-monitor / hcm-script），云函数不在其中。
真实做法是把源码写成 Python 字符串字面量，用 ``hcm.model.edit(DynamicScriptList)``
直推平台，再靠 md5 比对确认写入（见 dongruan_sync/_probe_693_source.py）。

后果：没有 diff 评审、没有版本台账、没有回滚点；``_cf_online_backup/`` 是**手工**补丁，
忘了刷新就失去回滚依据。「线上 CF 与本地源码不一致导致某实体组报错」这类缺陷
正是这个流程缺口的直接产物。

本模块补上这一环：
    scan      列出线上全部云函数，与本地源码比对（五种状态）
    diff      单条并排对比（在线源码 vs 本地源码 + 统一 diff）
    pull      线上 → 本地（原文件先备份为 .bak）
    push      本地 → 线上（**推送前自动归档**，形成可回滚的台账）
    ledger    部署台账
    rollback  从台账把线上恢复到某个历史版本

设计约束
--------
- 只通过 ``api.hcm.hcm_core.hcm_direct`` 访问平台（复用其自动重登与加解密）。
- 内容比对必须**归一化后再算 md5**：线上脚本常无文件头、行尾/首尾空行不一致，
  直接比 md5 会 100% 判为「不同」，看板立刻失去意义。
- 本地源码可能有多个根（现场实际就是散的：dongruan_sync 根、_cf_online_backup/、
  output/env121_stripped/），故按「多根 + 三级映射」处理，并把「同一 id 命中多个
  本地文件」显式报成 dup_local —— 那个状态本身就是需要暴露的问题。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api.common import logger

# ─────────────────────────────── 常量 ───────────────────────────────
CF_MODEL = "DynamicScriptList"
LEDGER_DIRNAME = "cf_deploy_ledger"
INDEX_FILENAME = "index.jsonl"
META_FILENAME = "meta.json"

# 状态取值（前端据此配色/筛选）
ST_IN_SYNC = "in_sync"          # 本地与线上一致
ST_DIFFERS = "differs"          # 两边都有，内容不同
ST_ONLY_ONLINE = "only_online"  # 线上有，本地找不到
ST_ONLY_LOCAL = "only_local"    # 本地有，线上没有
ST_DUP_LOCAL = "dup_local"      # 同一 id 命中多个本地文件（本身就是要暴露的问题）
ST_UNKNOWN = "unknown"          # 未取内容（with_content=False）

# 映射来源
MATCH_STEM = "stem"    # 文件名 == id
MATCH_ENTRY = "entry"  # 文件内 class 名 snake_case == id

# 批量取源码时的并发上限：网关单次 ~200-400ms，8 路足够且不至于压垮测试环境
DEFAULT_CONCURRENCY = 8

_CLASS_RE = re.compile(r"^[ \t]*class[ \t]+([A-Za-z_]\w*)[ \t]*[\(:]", re.MULTILINE)


# ─────────────────────────────── 路径 ───────────────────────────────
def ledger_root() -> Path:
    """台账落盘根目录（应用数据目录下，跟随冻结/开发两种形态）。"""
    from core.app_paths import get_data_root

    p = get_data_root() / LEDGER_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cfdebug_env_file() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config" / "cfdebug_env.local.json"


def default_local_roots() -> List[str]:
    """默认本地源码根候选（存在才返回）。

    现场实际是散落的，这里把已知的三个位置都列上：
      1. cfdebug 配置的 functions_root（当前指向 dongruan_sync）
      2. <root>/_cf_online_backup      —— 手工备份目录
      3. <root>/output/env121_stripped —— 某次全量导出快照
    """
    roots: List[str] = []
    try:
        cfg = json.loads(_cfdebug_env_file().read_text("utf-8"))
        fr = (cfg.get("functions_root") or "").strip()
    except Exception:
        fr = ""
    if fr and Path(fr).is_dir():
        roots.append(fr)
        for sub in ("_cf_online_backup", "output/env121_stripped"):
            p = Path(fr) / sub
            if p.is_dir():
                roots.append(str(p))
    return roots


def default_target() -> str:
    """默认网关：优先 hcm_whitelist 的 proxy_target，其次 cf_accounts 里可达的。"""
    try:
        from api.hcm.hcm_core import _HCM_PROXY_TARGET  # type: ignore

        if _HCM_PROXY_TARGET:
            return _HCM_PROXY_TARGET
    except Exception:
        pass
    return ""


# ─────────────────────────── 归一化与指纹 ───────────────────────────
def normalize_source(text: Optional[str]) -> str:
    """归一化云函数源码后再比对。

    线上脚本经平台往返后常见的无意义差异：CRLF、行尾空格、首尾空行、
    以及本地文件带 ``# -*- coding: utf-8 -*-`` 之外的空行差异。
    这些都不该被判成「内容不同」，否则看板全是噪声。
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in t.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        # 全空白内容统一归一成 ""。否则 normalize("") = ""（上面的早退）而
        # normalize("   ") = "\n"，同一个「空脚本」算出两个 md5，看板会平白报差异。
        return ""
    return "\n".join(lines) + "\n"


def source_md5(text: Optional[str]) -> str:
    """归一化后的 md5（比对用）。"""
    return hashlib.md5(normalize_source(text).encode("utf-8")).hexdigest()


def raw_md5(text: Optional[str]) -> str:
    """原始字节 md5（台账留档用，保证可字节级还原）。"""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def snake_case(name: str) -> str:
    """CamelCase → snake_case（``ConvJingcheEmp`` → ``conv_jingche_emp``）。"""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name or "")
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", s)
    return s.lower()


# ─────────────────────────── 本地源码索引 ───────────────────────────
def read_local(path: str) -> Optional[str]:
    try:
        return Path(path).read_text("utf-8")
    except Exception as e:
        logger.warning("[cf一致性] 读取本地源码失败 %s: %s", path, e)
        return None


def declared_classes(text: str) -> List[str]:
    """取文件里声明的类名（云函数源是 ``class Xxx(BasePrivateApiService)``）。"""
    try:
        return _CLASS_RE.findall(text or "")
    except Exception:
        return []


def build_local_index(roots: Sequence[str], max_files: int = 20000) -> Dict[str, Any]:
    """扫描本地源码根，建立 id → 文件 的索引。

    返回 ``{"by_id": {id: [fileinfo, ...]}, "files": n, "roots": [...], "errors": [...]}``。
    同一 id 命中多个文件时全部保留（由调用方判 dup_local）。
    """
    by_id: Dict[str, List[Dict[str, Any]]] = {}
    files = 0
    errors: List[str] = []
    seen_paths: set = set()

    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            errors.append(f"本地根不存在：{root}")
            continue
        for p in sorted(rp.rglob("*.py")):
            if files >= max_files:
                errors.append(f"本地文件数超过上限 {max_files}，已截断")
                break
            name = p.name
            if name.startswith(".") or name.startswith("_"):
                # 下划线前缀是本仓约定的一次性脚本，仍然可能是线上云函数，故不跳过；
                # 只跳过真正的隐藏/缓存文件。
                if name.startswith("."):
                    continue
            spath = str(p)
            if spath in seen_paths:
                continue
            seen_paths.add(spath)
            try:
                st = p.stat()
            except OSError:
                continue
            text = read_local(spath)
            if text is None:
                continue
            files += 1
            info = {
                "path": spath,
                "root": root,
                "name": name,
                "stem": p.stem,
                "classes": declared_classes(text),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "md5": source_md5(text),
            }
            keys = {p.stem.lower()}
            for cls in info["classes"]:
                keys.add(snake_case(cls))
            for k in keys:
                if not k:
                    continue
                by_id.setdefault(k, []).append(info)

    return {"by_id": by_id, "files": files, "roots": list(roots), "errors": errors}


def match_local(index: Dict[str, Any], cf_id: str) -> Tuple[List[Dict[str, Any]], str]:
    """按三级规则给线上 id 找本地文件，返回 (命中的文件列表, 匹配方式)。

    规则优先级：文件名 == id > 类名 snake_case == id（``by_id`` 已把两者合并建键，
    故这里只需区分「命中的是文件名键还是类名键」以给出可读的 match_by）。

    线上 id 恒为 CamelCase，本地文件名**多数**同名，但偶尔会写成 snake_case
    （``sync_beta.py`` ↔ ``SyncBeta``）。只按小写比对会漏掉这一类，白报成
    「only_online + only_local」两条，看板的数字就虚高 —— 所以补一层 snake_case 回退。
    """
    cid = cf_id or ""
    if not cid:
        return [], ""

    want = {cid.lower(), snake_case(cid)}
    cands = index["by_id"].get(cid.lower()) or []
    if not cands:
        alt = snake_case(cid)
        if alt and alt != cid.lower():
            cands = index["by_id"].get(alt) or []
    if not cands:
        return [], ""

    exact = [c for c in cands if c["stem"].lower() in want]
    if exact:
        return exact, MATCH_STEM
    return cands, MATCH_ENTRY


# ─────────────────────────── 平台访问 ───────────────────────────
def _req(api_name: str, params: dict, target: str, token: str = "", model: str = ""):
    return SimpleNamespace(
        api_name=api_name, params=params, token=token, model=model,
        target=target, sql_debug=False, profile_debug=False,
    )


async def _call(api_name: str, params: dict, target: str, token: str = "",
                model: str = "", retries: int = 3) -> dict:
    """调 HCM 网关；hcm_direct 失败时抛 HTTPException，这里重试并转结构化错误。"""
    from api.hcm.hcm_core import hcm_direct

    last = ""
    for i in range(max(1, retries)):
        try:
            r = await hcm_direct(_req(api_name, params, target, token, model))
        except Exception as e:  # HTTPException / 网络异常统一收敛
            last = str(getattr(e, "detail", None) or e)
            if i < retries - 1:
                await asyncio.sleep(1.5 * (i + 1))
                continue
            raise RuntimeError(last) from e
        if isinstance(r, dict) and r.get("ok"):
            return r
        last = json.dumps(r, ensure_ascii=False)[:300] if isinstance(r, dict) else str(r)
        if i < retries - 1:
            await asyncio.sleep(1.5 * (i + 1))
    raise RuntimeError(last or "网关调用失败")


async def fetch_online_list(target: str, token: str = "", page_size: int = 2000) -> List[dict]:
    """列出线上全部云函数（单次请求，实测 252 条 / 1 页）。"""
    r = await _call("hcm.model.list", {
        "model": CF_MODEL, "page_index": 1, "page_size": page_size,
    }, target, token, model=CF_MODEL)
    data = r.get("data") or {}
    return list(data.get("list") or [])


async def fetch_online_script(target: str, cf_id: str, token: str = "") -> Optional[str]:
    """取单个云函数的完整源码。"""
    r = await _call("hcm.model.get", {"model": CF_MODEL, "id_": cf_id},
                    target, token, model=CF_MODEL)
    data = r.get("data") or {}
    return data.get("script")


async def write_online_script(target: str, cf_id: str, script: str, token: str = "",
                              enabled: bool = True, verify_retries: int = 3) -> Dict[str, Any]:
    """把源码写回平台并校验（沿用现场既有做法：写后 get 比对归一化 md5）。

    写后校验是必要的：``hcm.model.edit`` 对 DynamicScriptList 存在过写入未生效的情况，
    现场脚本一律会重试到 md5 匹配为止。
    """
    want = source_md5(script)
    info: Dict[str, Any] = {"script": script}
    if enabled:
        info["enabled"] = True
    last = ""
    for i in range(max(1, verify_retries)):
        try:
            await _call("hcm.model.edit", {"model": CF_MODEL, "id_": cf_id, "info": info},
                        target, token, model=CF_MODEL, retries=2)
        except Exception as e:
            last = str(e)
            continue
        got = await fetch_online_script(target, cf_id, token)
        if source_md5(got) == want:
            return {"ok": True, "attempts": i + 1, "md5": want}
        last = "写入后校验不一致（平台返回内容与提交内容不同）"
    return {"ok": False, "error": last, "md5": want}


# ─────────────────────────── 扫描 ───────────────────────────
async def scan(target: str = "", token: str = "", roots: Optional[Sequence[str]] = None,
               with_content: bool = True, concurrency: int = DEFAULT_CONCURRENCY,
               category_filter: str = "") -> Dict[str, Any]:
    """线上 ↔ 本地一致性扫描。

    ``with_content=False`` 时只做映射，不拉源码（快，但状态停在 mapped/unknown）；
    为 True 时对**已映射**的条目并发拉源码并算出 in_sync / differs。
    """
    t0 = time.time()
    target = (target or default_target()).strip()
    roots = list(roots) if roots else default_local_roots()
    if not target:
        raise RuntimeError("未指定 HCM 网关地址（可在 config/hcm_whitelist.local.json 配 proxy_target）")
    if not roots:
        raise RuntimeError("未找到本地源码根：请先在「云函数调试 → 环境」里设置 functions_root")

    online = await fetch_online_list(target, token)
    if category_filter:
        cf = category_filter.lower()
        online = [c for c in online if cf in str(c.get("id") or "").lower()
                  or cf in str(c.get("category") or "").lower()]

    # 本地扫描是纯阻塞 I/O（数百次 read_text），必须下放线程，否则冻住事件循环
    index = await asyncio.to_thread(build_local_index, roots)

    entries: List[Dict[str, Any]] = []
    matched_paths: set = set()
    for c in online:
        cf_id = str(c.get("id") or "")
        cands, how = match_local(index, cf_id)
        for cd in cands:
            matched_paths.add(cd["path"])
        entry: Dict[str, Any] = {
            "id": cf_id,
            "category": c.get("category") or "",
            "description": c.get("description") or "",
            "enabled": bool(c.get("enabled", True)),
            "online_mtime": c.get("modify_date") or c.get("operate_time") or "",
            "online_operator": c.get("modifier_id") or c.get("operator_id") or "",
            "match_by": how,
            "local_path": cands[0]["path"] if cands else "",
            "local_root": cands[0]["root"] if cands else "",
            "local_mtime": cands[0]["mtime"] if cands else 0,
            "local_size": cands[0]["size"] if cands else 0,
            "local_md5": cands[0]["md5"] if cands else "",
            "dup_paths": [x["path"] for x in cands[1:]] if len(cands) > 1 else [],
            "online_md5": "",
            "status": ST_UNKNOWN,
            "error": "",
        }
        if not cands:
            entry["status"] = ST_ONLY_ONLINE
        elif len(cands) > 1:
            # 同一 id 命中多个本地文件：内容可能与线上一致，但「哪个才是源」本身是问题
            entry["status"] = ST_DUP_LOCAL
        else:
            entry["status"] = ST_UNKNOWN
        entries.append(entry)

    # 本地有、线上没有
    online_ids = {str(c.get("id") or "").lower() for c in online}
    local_only: List[Dict[str, Any]] = []
    for key, infos in index["by_id"].items():
        if key in online_ids:
            continue
        for info in infos:
            if info["path"] in matched_paths:
                continue
            if info["stem"].lower() != key:
                continue  # 类名键命中的重复项不重复列出
            local_only.append({
                "id": info["stem"],
                "local_path": info["path"],
                "local_mtime": info["mtime"],
                "local_size": info["size"],
                "local_md5": info["md5"],
                "classes": info["classes"],
                "status": ST_ONLY_LOCAL,
            })

    # 并发取线上源码（只对已映射的条目，省掉 200+ 次无意义请求）
    if with_content:
        todo = [e for e in entries if e["local_path"]]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(e: Dict[str, Any]) -> None:
            async with sem:
                try:
                    src = await fetch_online_script(target, e["id"], token)
                except Exception as ex:
                    e["error"] = str(ex)
                    return
                e["online_md5"] = source_md5(src)
                e["online_size"] = len(src or "")
                if e["status"] == ST_DUP_LOCAL:
                    # 多个本地文件里只要有任意一个与线上一致，仍标 dup（源不唯一）
                    pass
                elif e["online_md5"] == e["local_md5"]:
                    e["status"] = ST_IN_SYNC
                else:
                    e["status"] = ST_DIFFERS

        if todo:
            await asyncio.gather(*[_one(e) for e in todo])

    summary: Dict[str, int] = {
        ST_IN_SYNC: 0, ST_DIFFERS: 0, ST_ONLY_ONLINE: 0,
        ST_DUP_LOCAL: 0, ST_UNKNOWN: 0, ST_ONLY_LOCAL: len(local_only),
    }
    for e in entries:
        summary[e["status"]] = summary.get(e["status"], 0) + 1

    return {
        "ok": True,
        "target": target,
        "roots": roots,
        "online_count": len(online),
        "local_files": index["files"],
        "entries": entries,
        "local_only": local_only,
        "summary": summary,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "errors": index["errors"],
    }


# ─────────────────────────── diff ───────────────────────────
async def diff_one(cf_id: str, local_path: str = "", target: str = "",
                   token: str = "", context: int = 3) -> Dict[str, Any]:
    """单条并排对比：返回两侧源码 + 统一 diff（归一化后比较）。"""
    target = (target or default_target()).strip()
    if not target:
        raise RuntimeError("未指定 HCM 网关地址")
    if not cf_id:
        raise RuntimeError("缺少云函数 id")

    online_src = await fetch_online_script(target, cf_id, token)

    if not local_path:
        idx = await asyncio.to_thread(build_local_index, default_local_roots())
        cands, _ = match_local(idx, cf_id)
        local_path = cands[0]["path"] if cands else ""
    local_src = await asyncio.to_thread(read_local, local_path) if local_path else None

    on_norm = normalize_source(online_src)
    lo_norm = normalize_source(local_src)
    same = bool(local_src is not None) and on_norm == lo_norm

    udiff = ""
    if local_src is not None and not same:
        udiff = "".join(difflib.unified_diff(
            lo_norm.splitlines(keepends=True), on_norm.splitlines(keepends=True),
            fromfile=f"local:{local_path}", tofile=f"online:{cf_id}", n=context,
        ))
    elif local_src is None:
        udiff = f"（本地无对应文件，线上 {len(on_norm.splitlines())} 行）"

    return {
        "ok": True,
        "id": cf_id,
        "target": target,
        "local_path": local_path,
        "online": {"script": online_src or "", "md5": source_md5(online_src),
                   "lines": len(on_norm.splitlines())},
        "local": {"script": local_src if local_src is not None else "",
                  "md5": source_md5(local_src) if local_src is not None else "",
                  "lines": len(lo_norm.splitlines()) if local_src is not None else 0,
                  "exists": local_src is not None},
        "same": same,
        "diff": udiff,
    }


# ─────────────────────────── 台账 ───────────────────────────
def _new_entry_id(cf_id: str) -> str:
    """台账条目 id。

    必须带微秒后缀：原先只用 ``%Y%m%d-%H%M%S``，同一秒内的两次动作会撞成同一个
    id。而「推完发现不对、立刻回滚」恰恰是最自然的操作顺序 —— 一秒内完成完全正常。
    撞 id 的后果是回滚条目写进推送条目的目录，把 ``before.py`` 覆盖掉，
    于是这个模块存在的全部理由（有回滚点）当场失效。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", cf_id)[:60]
    micro = time.time_ns() // 1000 % 1_000_000
    return "%s-%06d-%s" % (time.strftime("%Y%m%d-%H%M%S"), micro, safe)


def _index_upsert(root: Path, eid: str, meta: Dict[str, Any]) -> None:
    """把条目写进 ``index.jsonl``，**同一 entry_id 只保留最后一条**。

    一次 push / rollback 会分两步落盘：先归档（拿到回滚点，此时 ok 还是 False）、
    写回平台后再补最终结果。早先这里是纯 append，于是同一次部署在台账里出现两行
    —— 先显示「失败」再显示「成功」；前端 ``key={e.entry_id}`` 还会撞 React key。
    """
    idx = root / INDEX_FILENAME
    kept: List[str] = []
    if idx.exists():
        try:
            for line in idx.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("entry_id") or "") != eid:
                    kept.append(json.dumps(row, ensure_ascii=False))
        except Exception as e:
            logger.warning("[cf一致性] 重写台账索引失败（退化为追加）: %s", e)
    kept.append(json.dumps(meta, ensure_ascii=False))
    idx.write_text("\n".join(kept) + "\n", "utf-8")


def ledger_record(meta: Dict[str, Any], before: Optional[str], after: Optional[str]) -> str:
    """把一次部署动作落盘：``<data_root>/cf_deploy_ledger/<entry_id>/{meta.json,before.py,after.py}``。

    **before 必须先落盘**，否则回滚点不存在——这是本模块存在的全部理由。
    ``before`` / ``after`` 为 None 表示「本次只更新元信息」，不会删除已归档的文件。
    """
    root = ledger_root()
    eid = meta["entry_id"]
    d = root / eid
    d.mkdir(parents=True, exist_ok=True)
    (d / META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    if before is not None:
        (d / "before.py").write_text(before, "utf-8")
    if after is not None:
        (d / "after.py").write_text(after, "utf-8")
    _index_upsert(root, eid, meta)
    return eid


def ledger_list(limit: int = 100, cf_id: str = "") -> Dict[str, Any]:
    """读部署台账（倒序，最新在前）。"""
    root = ledger_root()
    idx = root / INDEX_FILENAME
    rows: List[Dict[str, Any]] = []
    if idx.exists():
        try:
            for line in idx.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        except Exception as e:
            logger.warning("[cf一致性] 读取台账失败: %s", e)
    if cf_id:
        rows = [r for r in rows if r.get("cf_id") == cf_id]
    rows.reverse()
    for r in rows:
        d = root / str(r.get("entry_id") or "")
        r["has_before"] = (d / "before.py").exists()
        r["has_after"] = (d / "after.py").exists()
    return {"ok": True, "ledger_root": str(root), "total": len(rows), "entries": rows[:max(1, limit)]}


def ledger_entry(entry_id: str) -> Dict[str, Any]:
    root = ledger_root() / entry_id
    meta_f = root / META_FILENAME
    if not meta_f.exists():
        raise RuntimeError(f"台账条目不存在：{entry_id}")
    meta = json.loads(meta_f.read_text("utf-8"))
    for k, fn in (("before", "before.py"), ("after", "after.py")):
        p = root / fn
        meta[k + "_script"] = p.read_text("utf-8") if p.exists() else None
    meta["dir"] = str(root)
    return meta


# ─────────────────────────── pull / push / rollback ───────────────────────────
def backup_local(path: str) -> str:
    """本地文件写前备份为 ``<name>.bak``（覆盖上一份 bak）。"""
    p = Path(path)
    if not p.exists():
        return ""
    bak = p.with_suffix(p.suffix + ".bak")
    try:
        bak.write_text(p.read_text("utf-8"), "utf-8")
        return str(bak)
    except Exception as e:
        logger.warning("[cf一致性] 本地备份失败 %s: %s", path, e)
        return ""


async def pull(cf_id: str, local_path: str = "", target: str = "", token: str = "",
               create: bool = False, roots: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """线上 → 本地。写前把原文件备份为 .bak（不覆盖时需显式 create=True）。"""
    target = (target or default_target()).strip()
    src = await fetch_online_script(target, cf_id, token)
    if src is None:
        raise RuntimeError(f"线上云函数 {cf_id} 不存在或未返回 script")

    path = (local_path or "").strip()
    if not path:
        cands_root = list(roots) if roots else default_local_roots()
        if not cands_root:
            raise RuntimeError("未配置本地源码根")
        path = str(Path(cands_root[0]) / f"{cf_id}.py")

    p = Path(path)
    if p.exists() and not create and not local_path:
        raise RuntimeError(f"本地已存在 {path}，请显式指定 local_path 或 create=True")
    prev = await asyncio.to_thread(p.read_text, "utf-8") if p.exists() else None
    bak = await asyncio.to_thread(backup_local, str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(p.write_text, src, "utf-8")

    eid = _new_entry_id(cf_id)
    await asyncio.to_thread(ledger_record, {
        "entry_id": eid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cf_id": cf_id,
        "action": "pull", "target": target, "local_path": str(p),
        "before_md5": raw_md5(prev), "after_md5": raw_md5(src),
        "ok": True, "note": f"线上 → 本地，原文件备份 {bak or '（无）'}", "error": "",
    }, None, src)
    return {"ok": True, "entry_id": eid, "local_path": str(p), "backup": bak,
            "md5": source_md5(src), "bytes": len(src.encode("utf-8"))}


async def push(cf_id: str, local_path: str = "", target: str = "", token: str = "",
               note: str = "", roots: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """本地 → 线上。**推送前先把线上当前版本归档到台账**，否则回滚点不存在。"""
    target = (target or default_target()).strip()
    if not cf_id:
        raise RuntimeError("缺少云函数 id")

    path = (local_path or "").strip()
    if not path:
        cands_root = list(roots) if roots else default_local_roots()
        idx = await asyncio.to_thread(build_local_index, cands_root) if cands_root else {"by_id": {}}
        cands, _ = match_local(idx, cf_id)
        if not cands:
            raise RuntimeError(f"本地找不到 {cf_id} 的源码文件")
        if len(cands) > 1:
            raise RuntimeError(
                f"{cf_id} 命中多个本地文件，无法确定推哪个：" +
                "、".join(c["path"] for c in cands))
        path = cands[0]["path"]

    src = await asyncio.to_thread(read_local, path)
    if src is None:
        raise RuntimeError(f"读取本地源码失败：{path}")

    # ① 先归档线上现状（回滚点）
    before: Optional[str] = None
    try:
        before = await fetch_online_script(target, cf_id, token)
    except Exception as e:
        logger.warning("[cf一致性] 推送前拉取线上版本失败（将记为无回滚点）: %s", e)

    eid = _new_entry_id(cf_id)
    meta = {
        "entry_id": eid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cf_id": cf_id,
        "action": "push", "target": target, "local_path": str(path),
        "before_md5": source_md5(before), "after_md5": source_md5(src),
        "ok": False, "note": note, "error": "",
    }
    await asyncio.to_thread(ledger_record, meta, before, src)

    # ② 写回平台并校验
    try:
        res = await write_online_script(target, cf_id, src, token)
    except Exception as e:
        meta.update({"ok": False, "error": str(e)})
        await asyncio.to_thread(ledger_record, meta, None, None)
        return {"ok": False, "entry_id": eid, "error": str(e),
                "before_md5": meta["before_md5"], "after_md5": meta["after_md5"]}

    meta.update({"ok": bool(res.get("ok")), "error": res.get("error", ""),
                 "attempts": res.get("attempts", 0)})
    await asyncio.to_thread(ledger_record, meta, None, None)
    return {"ok": bool(res.get("ok")), "entry_id": eid, "attempts": res.get("attempts", 0),
            "error": res.get("error", ""), "before_md5": meta["before_md5"],
            "after_md5": meta["after_md5"]}


async def rollback(entry_id: str, target: str = "", token: str = "") -> Dict[str, Any]:
    """把线上恢复到某台账条目**之前**的版本（用该条目的 before.py）。"""
    entry = await asyncio.to_thread(ledger_entry, entry_id)
    before = entry.get("before_script")
    if before is None:
        raise RuntimeError(f"台账条目 {entry_id} 没有归档的线上版本，无法回滚")
    cf_id = entry.get("cf_id") or ""
    tgt = (target or entry.get("target") or default_target()).strip()

    current = None
    try:
        current = await fetch_online_script(tgt, cf_id, token)
    except Exception as e:
        logger.warning("[cf一致性] 回滚前读取线上版本失败: %s", e)

    new_id = _new_entry_id(cf_id)
    meta = {
        "entry_id": new_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "cf_id": cf_id,
        "action": "rollback", "target": tgt, "local_path": entry.get("local_path", ""),
        "before_md5": source_md5(current), "after_md5": source_md5(before),
        "rollback_of": entry_id, "ok": False,
        "note": f"回滚到 {entry_id} 之前的版本", "error": "",
    }
    await asyncio.to_thread(ledger_record, meta, current, before)

    res = await write_online_script(tgt, cf_id, before, token)
    meta.update({"ok": bool(res.get("ok")), "error": res.get("error", "")})
    await asyncio.to_thread(ledger_record, meta, None, None)
    return {"ok": bool(res.get("ok")), "entry_id": new_id, "rollback_of": entry_id,
            "cf_id": cf_id, "error": res.get("error", "")}
