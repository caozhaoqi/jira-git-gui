# -*- coding: utf-8 -*-
"""一键 CF + K8s + dynamic_log 编排测试，可直接 python3 运行。"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api.full_diagnose as full
from api.diagnosis_capabilities import build_query_plan, capability_manifest


def _req(**kwargs):
    defaults = {
        "text": "[定位] model=Employee id=123 field=department value=null stage=field_read || 部门为空 errcode=400014",
        "server_url": "https://hcm.example.com",
        "token": "token-should-not-appear",
        "model": "",
        "object_id": "",
        "field": "",
        "max_docs": 3,
        "max_chars": 800,
        "case_limit": 5,
        "current_value": "",
        "current_present": None,
        "k8s_env": "dev",
        "k8s_namespace": "hcm",
        "k8s_pod_filter": "hcm-core",
        "k8s_tail": 100,
        "dynamic_log_enabled": True,
        "dynamic_log_type": "salary_push",
        "dynamic_log_page_index": 1,
        "dynamic_log_page_size": 200,
        "dynamic_log_keyword": "",
        "proxy": "",
        "metadata": {"field_schema": {"department": {"required": True}}, "token": "secret-value"},
        "metadata_files": [],
        "include_coding_rules": True,
        "coding_rules_max_chars": 3000,
        "dynamic_log_match_limit": 20,
        "k8s_time_window_minutes": 15,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_full_orchestration_correlates_and_redacts():
    original_unified = full.unified_diagnose
    original_query = full.cf_query_logs

    def fake_unified(req):
        return {
            "ok": True,
            "unified_summary": {"root_cause": "FIELD_MISSING", "infrastructure_status": "healthy"},
            "cf_diagnosis": {"evidenceBundle": {"references": [{"kind": "wiki", "file": "01_FIELD_SCHEMA.md"}]}},
            "k8s_diagnosis": {"available": True, "abnormal_pods": [], "warning_events": []},
            "aiPrompt": "base prompt",
        }

    async def fake_query(req):
        return {
            "result": {
                "method": "cookie",
                "data": {
                    "list": [
                        {
                            "id": "log-1",
                            "create_time": "2026-08-29 14:00:00",
                            "log_type": "salary_push",
                            "content": "[定位] model=Employee id=123 field=department value=null stage=field_read || 部门为空 errcode=400014 token=secret",
                        },
                        {
                            "id": "log-2",
                            "create_time": "2026-08-29 13:59:00",
                            "log_type": "other_job",
                            "content": "[INFO] unrelated success",
                        },
                    ],
                    "total": 2,
                },
            }
        }

    full.unified_diagnose = fake_unified
    full.cf_query_logs = fake_query
    try:
        result = asyncio.run(full.full_diagnose(_req()))
    finally:
        full.unified_diagnose = original_unified
        full.cf_query_logs = original_query

    assert result["ok"] is True
    assert result["dynamic_log"]["available"] is True
    assert result["dynamic_log"]["matched_count"] == 1
    assert result["dynamic_log"]["matched"][0]["id"] == "log-1"
    assert result["metadata"]["inline"]["token"] == "***"
    assert result["coding_rules"]["available"] is True
    assert any(r["kind"] == "dynamic_log" for r in result["evidenceBundle"]["references"])
    assert "token-should-not-appear" not in result["aiPrompt"]
    assert "secret-value" not in result["aiPrompt"]
    assert "代码书写规则" in result["aiPrompt"]


def test_capability_manifest_and_adaptive_plan():
    manifest = capability_manifest()
    assert manifest["recommended_entrypoint"] == "POST /api/diagnose/full"
    paths = {item["path"] for item in manifest["capabilities"]}
    assert "/api/cf/logs" in paths
    assert "/api/cf/cases/feedback" in paths

    req = _req(server_url="", token="", k8s_env="", metadata={})
    plan = build_query_plan(req, {"errcode": 400014}, dynamic={}, base={}, metadata={})
    assert plan["strategy"] == "full_first_adaptive_fallback"
    assert any(item["field"] == "server_url/token" for item in plan["missing_inputs"])
    assert any(item["action"] == "collect_k8s" and item["status"] == "skipped" for item in plan["steps"])
    assert plan["evidence_completeness"]["score_percent"] < 60


def test_full_orchestration_degrades_without_dynamic_log_credentials(): 
    original_unified = full.unified_diagnose
    full.unified_diagnose = lambda req: {
        "ok": True,
        "unified_summary": {},
        "cf_diagnosis": {},
        "k8s_diagnosis": {"available": False},
        "aiPrompt": "base",
    }
    try:
        req = _req(server_url="", token="")
        result = asyncio.run(full.full_diagnose(req))
    finally:
        full.unified_diagnose = original_unified
    assert result["ok"] is True
    assert result["dynamic_log"]["available"] is False
    assert "未提供 server_url" in result["dynamic_log"]["error"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("3/3 passed")
