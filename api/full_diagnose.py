# -*- coding: utf-8 -*-
"""一键诊断编排：CF + K8s + 远程 dynamic_log + JSON 元数据。

该模块只做编排和证据裁剪，不改变既有 /api/diagnose、/api/cf/logs 行为。
目标是让 AI 一次调用拿到经过脱敏、结构化、关联后的证据包和提示词。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from api.common import _PROJECT_ROOT, logger
from api.cf.cf_diagnose import _redact_text, cf_parse_error, cf_parse_log_rows
from api.cf.cf_logs import cf_query_logs
from api.unified_diagnose import unified_diagnose

_REFERENCE_DIR = _PROJECT_ROOT / "store" / "downloads" / "895" / "docs" / "metadata" / "reference"
_CODING_RULES_PATH = _PROJECT_ROOT / "deliverables" / "gstack" / "cf-coding-standard.md"
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|authorization|cookie|token|access_key|private_key)"
)


def _extract_rows(data) -> list:
    """兼容 HCM list 返回的 result/data/list/items 多种形态。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "rows", "items", "data", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            rows = _extract_rows(value)
            if rows:
                return rows
    return []


def _redact_json(value, depth: int = 0):
    """递归脱敏元数据中的凭据字段，同时限制异常深度。"""
    if depth > 12:
        return "<max-depth>"
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                out[str(key)] = "***"
            else:
                out[str(key)] = _redact_json(val, depth + 1)
        return out
    if isinstance(value, list):
        return [_redact_json(v, depth + 1) for v in value[:500]]
    if isinstance(value, str):
        return _redact_text(value, 2000)
    return value


def _metadata_allowed(path: Path) -> bool:
    """只允许项目目录或内置 reference 目录下的 JSON，避免接口任意读本机文件。"""
    try:
        resolved = path.resolve()
        roots = (_PROJECT_ROOT.resolve(), _REFERENCE_DIR.resolve())
        return any(resolved == root or root in resolved.parents for root in roots)
    except OSError:
        return False


def _load_metadata(req) -> dict:
    """加载内联 metadata 和受限 JSON 文件，返回可直接给 AI 的脱敏摘要。"""
    result = {"available": False, "inline": {}, "files": [], "errors": []}
    inline = req.metadata if isinstance(req.metadata, dict) else {}
    if inline:
        result["inline"] = _redact_json(inline)
        result["available"] = True

    for raw_path in (req.metadata_files or [])[:10]:
        path = Path(str(raw_path)).expanduser()
        if path.suffix.lower() != ".json" or not _metadata_allowed(path):
            result["errors"].append(f"不允许读取的 JSON 路径: {raw_path}")
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                result["errors"].append(f"JSON 文件过大（上限 2MB）: {raw_path}")
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            result["files"].append({
                "path": str(path),
                "data": _redact_json(data),
            })
            result["available"] = True
        except (OSError, UnicodeError, ValueError) as e:
            result["errors"].append(f"读取 JSON 失败 {raw_path}: {e}")
    return result


def _load_coding_rules(req) -> dict:
    if not bool(req.include_coding_rules):
        return {"available": False, "file": str(_CODING_RULES_PATH), "content": ""}
    try:
        max_chars = max(1000, min(int(req.coding_rules_max_chars or 12000), 30000))
    except (TypeError, ValueError):
        max_chars = 12000
    try:
        text = _CODING_RULES_PATH.read_text(encoding="utf-8")
        return {
            "available": True,
            "file": str(_CODING_RULES_PATH),
            "content": text[:max_chars],
            "truncated": len(text) > max_chars,
        }
    except (OSError, UnicodeError) as e:
        return {"available": False, "file": str(_CODING_RULES_PATH), "content": "", "error": str(e)}


def _parsed_target(req) -> dict:
    parsed = cf_parse_error(req.text or "")
    for key in ("model", "object_id", "field"):
        value = getattr(req, key, "") or ""
        if value:
            parsed[key] = value
    return parsed


def _public_dynamic_row(row: dict) -> dict:
    pc = row.get("parsed_content") if isinstance(row.get("parsed_content"), dict) else {}
    raw = str(row.get("content") or "")
    return {
        "id": row.get("id"),
        "create_time": row.get("create_time"),
        "log_type": row.get("log_type"),
        "content": _redact_text(raw, 2000),
        "parsed_content": {
            key: pc.get(key)
            for key in (
                "level", "stage", "dept_id", "object_id", "errcode",
                "error_code", "is_error", "locate", "message", "model", "field",
            )
            if pc.get(key) is not None
        },
    }


def _correlate_dynamic_rows(rows: list, target: dict, keyword: str = "", limit: int = 20) -> dict:
    """按 error_code/errcode/定位字段/log_type 对远端 dynamic_log 打分。"""
    keyword = (keyword or "").strip().lower()
    matches = []
    levels, stages, errcodes = {}, {}, {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("content") or "")
        if keyword and keyword not in raw.lower():
            continue
        pc = row.get("parsed_content") if isinstance(row.get("parsed_content"), dict) else {}
        hay = (raw + " " + json.dumps(pc, ensure_ascii=False)).lower()
        score = 0
        error_code = str(target.get("error_code") or "")
        errcode = target.get("errcode")
        if error_code and error_code in hay:
            score += 12
        if errcode is not None and re.search(rf"\berrcode\D*{re.escape(str(errcode))}\b", hay):
            score += 8
        for key, weight in (("model", 3), ("object_id", 4), ("field", 3), ("log_type", 2)):
            value = str(target.get(key) or "").lower()
            if value and value in hay:
                score += weight
        if pc.get("is_error"):
            score += 1
        level = str(pc.get("level") or "OTHER")
        levels[level] = levels.get(level, 0) + 1
        if pc.get("stage"):
            stages[str(pc["stage"])] = stages.get(str(pc["stage"]), 0) + 1
        if pc.get("errcode") is not None:
            key = str(pc["errcode"])
            errcodes[key] = errcodes.get(key, 0) + 1
        if score:
            public = _public_dynamic_row(row)
            public["match_score"] = score
            matches.append(public)
    matches.sort(key=lambda item: (-item["match_score"], str(item.get("create_time") or "")))
    return {
        "matched": matches[:max(1, min(int(limit or 20), 100))],
        "matched_count": len(matches),
        "summary": {"levels": levels, "stages": stages, "errcodes": errcodes},
    }


async def _fetch_dynamic_log(req, target: dict) -> dict:
    log_type = (req.dynamic_log_type or target.get("log_type") or "").strip()
    result = {
        "available": False,
        "queried": False,
        "log_type": log_type,
        "total": 0,
        "returned_count": 0,
        "matched_count": 0,
        "matched": [],
        "summary": {},
        "error": "",
    }
    if not req.dynamic_log_enabled:
        result["error"] = "dynamic_log 采集已关闭"
        return result
    if not (req.server_url or "").strip():
        result["error"] = "未提供 server_url，跳过远程 dynamic_log 查询"
        return result
    query_req = SimpleNamespace(
        server_url=req.server_url,
        token=req.token,
        log_type=log_type,
        page_index=max(1, int(req.dynamic_log_page_index or 1)),
        page_size=max(1, min(int(req.dynamic_log_page_size or 200), 1000)),
        proxy=req.proxy or "",
    )
    try:
        response = await cf_query_logs(query_req)
        result["queried"] = True
        payload = response.get("result") if isinstance(response, dict) else response
        data = payload.get("data") if isinstance(payload, dict) else payload
        rows = _extract_rows(data if data is not None else payload)
        parsed_rows = cf_parse_log_rows(rows)
        correlation = _correlate_dynamic_rows(
            parsed_rows, target, req.dynamic_log_keyword, req.dynamic_log_match_limit,
        )
        result.update({
            "available": True,
            "total": _extract_total(data if isinstance(data, dict) else payload, len(rows)),
            "returned_count": len(rows),
            **correlation,
        })
        return result
    except Exception as e:
        logger.warning(f"[FULL-DIAG] dynamic_log 查询失败: {e}")
        result["error"] = f"dynamic_log 查询失败: {type(e).__name__}: {e}"
        return result


def _extract_total(data, fallback: int) -> int:
    if isinstance(data, dict):
        for key in ("total", "count", "row_count"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    return fallback


def _build_full_prompt(base_prompt: str, target: dict, dynamic: dict,
                       metadata: dict, coding_rules: dict) -> str:
    lines = [base_prompt or "# CF + K8s 诊断上下文", "", "# 一键诊断 AI 执行规范", ""]
    lines += [
        "你是 HCM 云函数故障诊断 AI。必须先区分‘已验证事实’和‘候选推断’，不要仅凭单条日志下结论。",
        "证据优先级：K8s previous/current 日志与事件 > 匹配的 dynamic_log > [定位] 错误文本 > JSON 元数据 > 源码/Wiki 推断。",
        "如果 K8s OOM/Crash/NodeNotReady 与应用错误同时出现，先判断基础设施是否覆盖应用层结论。",
        "如果 dynamic_log 没有命中，不得声称‘没有发生’，只能说‘当前采样范围未命中’。",
        "输出必须包含：根因层级、事实证据、候选根因及置信度、排除项、下一步检查、最小修复方案、代码规范风险。",
        "写代码建议必须遵守：类内状态、输入/输出日志、避免冗余 import、异常记录后保留 traceback、关系 key 批量读取、safe_get/[定位]、敏感日志脱敏、插件少日志。",
        "",
        "## 关联目标",
        json.dumps(target, ensure_ascii=False),
        "",
        "## dynamic_log 命中样本",
    ]
    if dynamic.get("matched"):
        for row in dynamic["matched"][:20]:
            lines.append(json.dumps(row, ensure_ascii=False))
    else:
        lines.append("当前没有匹配的 dynamic_log 样本。")
    lines += ["", "## JSON 元数据"]
    metadata_text = json.dumps(metadata, ensure_ascii=False)
    lines.append(metadata_text[:20000])
    lines += ["", "## 云函数代码书写规则"]
    if coding_rules.get("available"):
        lines.append(coding_rules.get("content", "")[:20000])
    else:
        lines.append("代码规范文件不可用，请至少遵守本提示中的规则。")
    lines += [
        "",
        "## 最终回答格式",
        "1. 结论（application / infrastructure / mixed / unknown）",
        "2. 证据链（按来源逐条列出，附时间、Pod、log_type、errcode、model/id/field）",
        "3. 候选根因与置信度",
        "4. 需要人工或系统验证的检查项",
        "5. 最小代码/配置修复与回归建议",
    ]
    return "\n".join(lines)[:60000]


async def full_diagnose(req) -> dict:
    """一次编排 CF + K8s + remote dynamic_log + metadata + coding rules。"""
    target = _parsed_target(req)
    try:
        base = unified_diagnose(req)
    except Exception as e:
        logger.exception(f"[FULL-DIAG] 基础联合诊断失败: {e}")
        base = {
            "ok": False,
            "unified_summary": {"root_cause": "UNKNOWN", "status": "need_verification"},
            "cf_diagnosis": {},
            "k8s_diagnosis": {"available": False, "error": str(e)},
            "aiPrompt": "",
        }
    dynamic = await _fetch_dynamic_log(req, target)
    metadata = _load_metadata(req)
    coding_rules = _load_coding_rules(req)

    references = []
    cf_bundle = (base.get("cf_diagnosis") or {}).get("evidenceBundle") or {}
    references.extend(cf_bundle.get("references") or [])
    for row in dynamic.get("matched") or []:
        references.append({
            "kind": "dynamic_log",
            "reason": "按 error_code/errcode/model/object_id/field/log_type 关联",
            "id": row.get("id"),
            "create_time": row.get("create_time"),
            "log_type": row.get("log_type"),
        })
    for item in metadata.get("files") or []:
        references.append({"kind": "metadata", "reason": "请求指定 JSON 元数据", "file": item.get("path")})
    full_bundle = {
        **(cf_bundle if isinstance(cf_bundle, dict) else {}),
        "references": references,
        "dynamic_log_available": dynamic.get("available", False),
        "metadata_available": metadata.get("available", False),
        "coding_rules_file": coding_rules.get("file"),
    }
    prompt = _build_full_prompt(base.get("aiPrompt", ""), target, dynamic, metadata, coding_rules)
    return {
        "ok": True,
        "diagnosed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "correlation": {
            "target": target,
            "dynamic_log_type": dynamic.get("log_type"),
            "k8s_env": getattr(req, "k8s_env", ""),
            "k8s_namespace": getattr(req, "k8s_namespace", ""),
            "k8s_pod_filter": getattr(req, "k8s_pod_filter", ""),
        },
        "summary": base.get("unified_summary", {}),
        "cf_diagnosis": base.get("cf_diagnosis", {}),
        "k8s_diagnosis": base.get("k8s_diagnosis", {}),
        "dynamic_log": dynamic,
        "metadata": metadata,
        "coding_rules": {
            "available": coding_rules.get("available", False),
            "file": coding_rules.get("file"),
            "truncated": coding_rules.get("truncated", False),
        },
        "evidenceBundle": full_bundle,
        "aiPrompt": prompt,
    }
