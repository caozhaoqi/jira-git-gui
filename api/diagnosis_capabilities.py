# -*- coding: utf-8 -*-
"""AI 诊断接口能力清单与自适应调用计划。

该模块不执行外部调用，只告诉 AI：有哪些只读接口、何时调用、缺什么参数、
当前证据是否完整以及下一步如何降级。这样 AI 不需要凭记忆猜接口。
"""
from __future__ import annotations

from typing import Any


CAPABILITY_SCHEMA = "hcm-ai-diagnosis-capabilities/v1"

_CAPABILITIES = [
    {
        "id": "full_diagnosis",
        "method": "POST",
        "path": "/api/diagnose/full",
        "purpose": "一次编排 CF 错误、K8s hcm-core、远程 dynamic_log、JSON 元数据和代码规范",
        "when": "默认首选；需要跨应用层、基础设施层和日志层联合判断时",
        "inputs": ["text", "server_url", "token", "k8s_env", "k8s_namespace", "k8s_pod_filter", "metadata", "metadata_files"],
        "outputs": ["summary", "dynamic_log", "k8s_diagnosis", "metadata", "evidenceBundle", "aiPrompt"],
        "read_only": True,
        "priority": 1,
    },
    {
        "id": "cf_k8s_diagnosis",
        "method": "POST",
        "path": "/api/diagnose",
        "purpose": "CF + K8s 联合诊断，不主动查询远程 dynamic_log",
        "when": "没有 HCM 查询凭据，或只需要应用层与 K8s 证据时",
        "inputs": ["text", "k8s_env", "k8s_namespace", "k8s_pod_filter"],
        "outputs": ["unified_summary", "cf_diagnosis", "k8s_diagnosis", "aiPrompt"],
        "read_only": True,
        "priority": 2,
    },
    {
        "id": "dynamic_log_query",
        "method": "POST",
        "path": "/api/cf/logs",
        "purpose": "查询远程 HCM dynamic_log",
        "when": "需要扩大日志采样、full 诊断未命中或需要指定 log_type 时",
        "inputs": ["server_url", "token", "log_type", "page_index", "page_size", "proxy"],
        "outputs": ["rows", "total", "raw response"],
        "read_only": True,
        "priority": 3,
    },
    {
        "id": "dynamic_log_parse",
        "method": "POST",
        "path": "/api/cf/logs/parse",
        "purpose": "将 dynamic_log content 转为 level/stage/errcode/定位字段",
        "when": "拿到原始日志行但没有 parsed_content 时",
        "inputs": ["rows"],
        "outputs": ["rows[].parsed_content"],
        "read_only": True,
        "priority": 4,
    },
    {
        "id": "k8s_diagnosis",
        "method": "GET",
        "path": "/api/diagnose/k8s",
        "purpose": "只查询 K8s Pod、事件、崩溃日志和资源 Top",
        "when": "full 诊断没有 K8s 环境，或需要单独刷新基础设施证据时",
        "inputs": ["env", "namespace", "pod_filter", "tail"],
        "outputs": ["abnormal_pods", "warning_events", "crash_logs", "top_consumers", "pattern_matches"],
        "read_only": True,
        "priority": 5,
    },
    {
        "id": "cf_context",
        "method": "POST",
        "path": "/api/cf/diagnose-context",
        "purpose": "查询 CF 错误码、Wiki、源码证据、本地导出日志和案例",
        "when": "full 诊断不可用，或需要只刷新 CF 侧证据时",
        "inputs": ["text", "server_url", "token", "model", "object_id", "field"],
        "outputs": ["summary", "errDict", "wiki", "sourceEvidence", "logMatches", "evidenceBundle"],
        "read_only": True,
        "priority": 6,
    },
    {
        "id": "source_index_rebuild",
        "method": "POST",
        "path": "/api/cf/diagnose-index/rebuild",
        "purpose": "重建 hcm-core 源码证据索引",
        "when": "源码发生变化或 sourceEvidence 明显过旧时",
        "inputs": [],
        "outputs": ["index status"],
        "read_only": True,
        "priority": 7,
    },
    {
        "id": "feedback",
        "method": "POST",
        "path": "/api/cf/cases/feedback",
        "purpose": "保存人工确认结果，驱动诊断准确率和规则闭环",
        "when": "AI 诊断完成且人工确认根因后",
        "inputs": ["case_file", "result", "actual_root_cause", "fix_applied", "notes"],
        "outputs": ["feedback record"],
        "read_only": False,
        "priority": 8,
    },
]


def capability_manifest() -> dict:
    return {
        "schema": CAPABILITY_SCHEMA,
        "recommended_entrypoint": "POST /api/diagnose/full",
        "discovery_endpoint": "GET /api/diagnose/capabilities",
        "principles": [
            "先用 full diagnosis 一次取齐证据，避免 AI 盲目串行调用多个接口",
            "缺少凭据时使用 CF + K8s 降级路径，不因 dynamic_log 不可用而停止诊断",
            "只有证据过旧或缺失时才调用专项刷新接口",
            "反馈接口只在人工确认后调用，不允许 AI 把猜测当事实写回规范",
        ],
        "capabilities": [dict(item) for item in _CAPABILITIES],
    }


def _has(value: Any) -> bool:
    return bool(str(value or "").strip())


def build_query_plan(req, target: dict, dynamic: dict | None = None,
                     base: dict | None = None, metadata: dict | None = None) -> dict:
    """基于当前输入和已有结果生成 AI 下一步调用计划。"""
    dynamic = dynamic or {}
    base = base or {}
    metadata = metadata or {}
    missing_inputs = []
    optional_enrichment = []
    steps = [
        {"order": 1, "action": "parse_error", "status": "completed", "endpoint": None,
         "reason": "从 text 提取 errcode/error_code/model/object_id/field/log_type"},
    ]

    has_hcm = _has(getattr(req, "server_url", "")) and _has(getattr(req, "token", ""))
    has_k8s = _has(getattr(req, "k8s_env", ""))
    has_target_key = any(_has(target.get(key)) for key in ("error_code", "errcode", "model", "object_id", "field", "log_type"))

    if has_hcm:
        steps.append({"order": 2, "action": "query_dynamic_log", "status": "completed" if dynamic.get("queried") else "pending",
                      "endpoint": "POST /api/cf/logs", "reason": "使用 error_code/errcode/定位字段关联远程日志"})
    else:
        missing_inputs.append({"field": "server_url/token", "needed_for": "remote dynamic_log", "fallback": "继续使用 CF + K8s + 本地证据"})
        steps.append({"order": 2, "action": "query_dynamic_log", "status": "skipped", "endpoint": "POST /api/cf/logs",
                      "reason": "缺少 HCM server_url 或 token"})

    if has_k8s:
        steps.append({"order": 3, "action": "collect_k8s", "status": "completed" if (base.get("k8s_diagnosis") or {}).get("available") else "pending",
                      "endpoint": "GET /api/diagnose/k8s", "reason": "采集 hcm-core Pod/事件/current/previous 日志"})
    else:
        missing_inputs.append({"field": "k8s_env", "needed_for": "K8s hcm-core evidence", "fallback": "仅分析 CF 和 dynamic_log"})
        steps.append({"order": 3, "action": "collect_k8s", "status": "skipped", "endpoint": "GET /api/diagnose/k8s",
                      "reason": "未提供 K8s 环境"})

    if metadata.get("available"):
        steps.append({"order": 4, "action": "load_metadata", "status": "completed", "endpoint": None,
                      "reason": "已加载并脱敏 JSON 元数据"})
    else:
        optional_enrichment.append({"field": "metadata/metadata_files", "reason": "字段 schema/关系约束未提供，无法完成元数据交叉验证"})
        steps.append({"order": 4, "action": "load_metadata", "status": "skipped", "endpoint": None,
                      "reason": "未提供 JSON 元数据"})

    if not has_target_key:
        missing_inputs.append({"field": "error_code/errcode/model/object_id/field/log_type", "needed_for": "high-precision correlation",
                               "fallback": "使用全文和时间范围做低精度匹配"})
    if not _has(getattr(req, "k8s_pod_filter", "")) and has_k8s:
        optional_enrichment.append({"field": "k8s_pod_filter", "reason": "建议填写 hcm-core，缩小日志范围"})

    dynamic_match_count = int(dynamic.get("matched_count") or 0)
    k8s = base.get("k8s_diagnosis") or {}
    cf = base.get("cf_diagnosis") or {}
    evidence = {
        "error_text": bool(_has(getattr(req, "text", ""))),
        "cf_context": bool(cf),
        "source_evidence": bool(cf.get("sourceEvidence")),
        "dynamic_log": bool(dynamic.get("available")),
        "dynamic_log_match": dynamic_match_count > 0,
        "k8s": bool(k8s.get("available")),
        "metadata": bool(metadata.get("available")),
        "coding_rules": bool(getattr(req, "include_coding_rules", True)),
    }
    score = round(sum(1 for value in evidence.values() if value) / len(evidence) * 100, 1)
    if dynamic.get("queried") and dynamic_match_count == 0:
        optional_enrichment.append({"field": "dynamic_log_page_size/page_index/keyword", "reason": "当前采样未命中，可扩大窗口或取消关键词"})

    return {
        "strategy": "full_first_adaptive_fallback",
        "recommended_first_call": "POST /api/diagnose/full",
        "steps": steps,
        "missing_inputs": missing_inputs,
        "optional_enrichment": optional_enrichment,
        "evidence_completeness": {"score_percent": score, "signals": evidence},
        "next_actions": [
            "先阅读 summary、evidenceBundle 和 aiPrompt，再决定是否调用专项接口",
            "不要重复调用已经 completed 的步骤，除非扩大采样范围或证据已过期",
            "人工确认根因后再调用 POST /api/cf/cases/feedback",
        ],
    }
