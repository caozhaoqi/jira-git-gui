# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace

import api.cf.cf_diagnose as diagnose
from api.cf.cf_diagnose import (
    _parse_route_index,
    _redact_text,
    _resolve_doc_path,
    cf_diagnose_context,
    cf_rebuild_source_index,
    cf_save_case,
    cf_save_feedback,
    cf_feedback_metrics,
    cf_parse_error,
    parse_cf_log_content,
)


def _req(text: str, **kwargs):
    defaults = {
        "server_url": "",
        "token": "",
        "model": "",
        "object_id": "",
        "field": "",
        "max_docs": 3,
        "max_chars": 800,
        "case_limit": 5,
        "current_value": "",
        "current_present": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(text=text, **defaults)


def test_parse_location_error_and_business_code():
    parsed = cf_parse_error(
        "[定位] model=employee id=5841977 field=id_card value=null "
        "stage=field_read || 身份证号为空 errcode=400014"
    )
    assert parsed["model"] == "employee"
    assert parsed["object_id"] == "5841977"
    assert parsed["field"] == "id_card"
    assert parsed["stage"] == "field_read"
    assert parsed["errcode"] == 400014
    assert parsed["message"] == "身份证号为空 errcode=400014"


def test_parse_error_code_is_not_business_code():
    parsed = cf_parse_error("errcode: 17003 error_code: 1787706690673")
    assert parsed["errcode"] == 17003
    assert parsed["error_code"] == "1787706690673"
    assert cf_parse_error("1787706690673")["errcode"] is None


def test_parse_dynamic_log_shapes():
    structured = parse_cf_log_content(
        "{'report': 'daily_overtime', 'stage': 'start', 'dept_id': 16078507}"
    )
    assert structured["stage"] == "start"
    assert structured["dept_id"] == 16078507
    assert structured["is_error"] is False

    success = parse_cf_log_content(
        "[RID:129837] [POINTS_MODULE] [WRITE] ✓ edit 成功, record_id=23178667"
    )
    assert success["tags"]["RID"] == "129837"
    assert success["is_error"] is False

    diagnostic = parse_cf_log_content(
        "[DIAG_ERR] [定位] model=employee id=1 field=id_card "
        "value=null stage=field_read || missing"
    )
    assert diagnostic["is_error"] is True
    assert diagnostic["stage"] == "field_read"
    assert diagnostic["locate"]["object_id"] == "1"


def test_parse_cf_diag_standard_log():
    """cf_diag() 输出的标准化日志走精确解析分支。"""
    ok = parse_cf_log_content(
        "[DIAG][INFO][stage:fetch][model:Employee][id:23178667] 查询员工成功"
    )
    assert ok["schema"] == "cf-diag/v1"
    assert ok["level"] == "INFO"
    assert ok["stage"] == "fetch"
    assert ok["model"] == "Employee"
    assert ok["object_id"] == "23178667"
    assert ok["message"] == "查询员工成功"
    assert ok["is_error"] is False

    err = parse_cf_log_content(
        "[DIAG][ERROR][stage:field_read][model:Employee][id:23178667]"
        "[field:id_card] 身份证号为空"
    )
    assert err["level"] == "ERROR"
    assert err["field"] == "id_card"
    assert err["is_error"] is True


def test_parse_cf_diag_warn_level_normalized_and_extra_json():
    """WARN 归一化成 WARNING；尾部 JSON 扩展被解析且从 message 剥离。"""
    r = parse_cf_log_content(
        '[DIAG][WARN][stage:process][rid:129837] 处理较慢 {"done": 5, "total": 100}'
    )
    assert r["level"] == "WARNING"
    assert r["rid"] == "129837"
    assert r["data"] == {"done": 5, "total": 100}
    assert r["message"] == "处理较慢"


def test_parse_cf_diag_extracts_errcode_and_flags_traceback():
    """errcode 与 traceback 从扩展 JSON 提取。"""
    r = parse_cf_log_content(
        '[DIAG][ERROR][stage:save][model:Emp][id:9] KeyError: x '
        '{"errcode": 17003, "traceback": "Traceback..."}'
    )
    assert r["errcode"] == 17003
    assert r["is_error"] is True
    assert r["message"] == "KeyError: x"
    assert r["data"]["traceback"] == "Traceback..."


def test_legacy_diag_err_tag_still_uses_compatible_branch():
    """老的 [DIAG_ERR] 标签不能误命中 cf-diag 精确解析分支。"""
    r = parse_cf_log_content(
        "[DIAG_ERR] [定位] model=employee id=1 field=id_card "
        "value=null stage=field_read || missing"
    )
    assert r["schema"] is None
    assert r["is_error"] is True
    assert r["locate"]["object_id"] == "1"


def test_cf_diag_snippet_formats_roundtrip():
    """SDK 的 diag_format 输出能被后端解析器原样解析回来。"""
    import importlib.util
    from pathlib import Path

    snippet = (
        Path(__file__).resolve().parents[1]
        / "tools" / "cf_locate_kit" / "cf_diag_snippet.py"
    )
    spec = importlib.util.spec_from_file_location("cf_diag_snippet", snippet)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    content = mod.diag_format(
        level="ERROR", stage="field_read", model="Employee",
        oid="23178667", field="id_card", msg="身份证号为空",
    )
    assert content.startswith("[DIAG][ERROR]")
    assert "[field:id_card]" in content

    parsed = parse_cf_log_content(content)
    assert parsed["schema"] == "cf-diag/v1"
    assert parsed["level"] == "ERROR"
    assert parsed["stage"] == "field_read"
    assert parsed["model"] == "Employee"
    assert parsed["object_id"] == "23178667"
    assert parsed["field"] == "id_card"
    assert parsed["message"] == "身份证号为空"

    # 敏感值脱敏：长串只保留首尾
    masked = mod.diag_format(oid="110101199001011234", msg="x")
    assert "110101****1234" in masked
    assert "19900101" not in masked

    # 非法级别回退 INFO
    assert "[INFO]" in mod.diag_format(level="VERBOSE", msg="x")


def test_route_index_and_metadata_doc_resolution():
    index = _parse_route_index()
    assert len(index["errcode"]) >= 15
    assert _resolve_doc_path("metadata/specs/metadata-spec.md") is not None
    assert _resolve_doc_path("info_form_relations/01_SCHEMA_MATRIX.md") is not None


def test_diagnose_context_contains_summary_contract():
    result = cf_diagnose_context(_req("errcode: 17003 error_code: 1787706690673"))
    assert result["summary"]["root_cause"] == "OPEN_API_EXECUTION_ERROR"
    assert result["summary"]["status"] == "need_verification"
    assert result["summary"]["checks_to_run"]


def test_diagnose_context_contains_dictionary_and_wiki():
    result = cf_diagnose_context(_req("errcode: 17003 error_code: 1787706690673"))
    assert result["ok"] is True
    assert result["parsed"]["errcode"] == 17003
    assert result["errDict"]["name"] == "EXECUTE_OPEN_API_ERROR"
    assert result["wiki"]["snippets"]
    assert "HCM" in result["aiPrompt"]


def test_reference_source_evidence_and_error_catalog():
    result = cf_diagnose_context(_req("errcode: 17003 error_code: 1787706690673"))
    assert result["sourceEvidence"]["available"] is True
    assert result["sourceEvidence"]["hits"]
    assert any(item["file"] == "core/service/handlers.py" for item in result["sourceEvidence"]["hits"])
    assert result["referenceError"]["matched"][0]["name"] == "EXECUTE_OPEN_API_ERROR"
    assert result["referenceError"]["source_error_code_count"] >= 200
    assert result["logMatches"]["matches"]
    assert "参考云函数源码证据" in result["aiPrompt"]


def test_indexed_evidence_has_ai_readable_addresses():
    result = cf_diagnose_context(_req("errcode: 17003 error_code: 1787706690673"))
    assert result["sourceEvidence"]["index"]["used"] is True
    assert result["sourceEvidence"]["hits"][0]["file"] == "core/service/handlers.py"
    assert result["sourceEvidence"]["hits"][0]["hits"][0]["address"]["line"]
    assert result["wiki"]["snippets"][0]["address"]["absolute_path"]
    assert result["evidenceBundle"]["schema"] == "hcm-cf-evidence-bundle/v1"
    assert result["evidenceBundle"]["references"]
    assert result["evidenceBundle"]["canonical_paths"]
    assert "可直接提供给 AI 的证据地址" in result["aiPrompt"]


def test_source_index_rebuild_returns_project_index_path():
    result = cf_rebuild_source_index()
    assert result["ok"] is True
    assert result["file_count"] >= 300
    assert result["path"].endswith("cf_source_index.json")


def test_source_evidence_redacts_sensitive_values():
    assert "token=***" in _redact_text("token=secret-value")
    assert "id=1234****89" in _redact_text("id=123456789")


def test_case_feedback_and_metrics(tmp_path, monkeypatch):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    case = case_dir / "cf_case_17003_test_20260829_100000.md"
    case.write_text("# Test case\\n- errcode: 17003\\n", encoding="utf-8")
    monkeypatch.setattr(diagnose, "_CASE_DIR", case_dir)
    monkeypatch.setattr(diagnose, "_FEEDBACK_PATH", case_dir / "diagnosis_feedback.jsonl")

    saved = cf_save_feedback(SimpleNamespace(
        case_file=case.name,
        result="correct",
        actual_root_cause="TOKEN_EXPIRED",
        fix_applied=True,
        notes="重新登录后恢复",
        source="test",
    ))
    assert saved["ok"] is True
    metrics = cf_feedback_metrics()
    assert metrics["total"] == 1
    assert metrics["accuracy"] == 1.0


def test_case_feedback_rejects_path_traversal(tmp_path, monkeypatch):
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    monkeypatch.setattr(diagnose, "_CASE_DIR", case_dir)
    monkeypatch.setattr(diagnose, "_FEEDBACK_PATH", case_dir / "diagnosis_feedback.jsonl")
    try:
        cf_save_feedback(SimpleNamespace(
            case_file="../outside.md", result="wrong", actual_root_cause="", fix_applied=None,
            notes="", source="test",
        ))
    except ValueError as exc:
        assert "case_file" in str(exc)
    else:
        raise AssertionError("path traversal should be rejected")


def test_diagnose_context_masks_location_value():
    result = cf_diagnose_context(
        _req(
            "[定位] model=employee id=1 field=id_card "
            "value=110101199001011234 stage=field_read || invalid",
            current_value="(空)",
            current_present=False,
        )
    )
    assert result["parsed"]["value"] == "110101****1234"
    assert result["currentData"] == {"value": "(空)", "present": False}


def _patch_feedback_env(tmp_path, monkeypatch, errdict_codes=None, route_seed=""):
    """把诊断反馈闭环涉及的路径指向临时目录，避免污染真实参考文件。"""
    ref = tmp_path / "ref"
    ref.mkdir()
    cases = tmp_path / "cases"
    cases.mkdir()
    errdict = {"_meta": {"description": "test"}, "errcodes": errdict_codes or {}}
    (ref / "errdict.json").write_text(json.dumps(errdict, ensure_ascii=False), encoding="utf-8")
    route_text = (
        "# 路由索引\n\n## 一、按 errcode 路由\n\n"
        "| errcode | 名称 | 含义 | 首选文档 | 补充文档 |\n"
        "|---------|------|------|----------|----------|\n"
        + route_seed
    )
    (ref / "ERROR_ROUTE_INDEX.md").write_text(route_text, encoding="utf-8")

    monkeypatch.setattr(diagnose, "_REF_DIR", ref)
    monkeypatch.setattr(diagnose, "_ERRDICT_PATH", ref / "errdict.json")
    monkeypatch.setattr(diagnose, "_ROUTE_INDEX_PATH", ref / "ERROR_ROUTE_INDEX.md")
    monkeypatch.setattr(diagnose, "_CASE_DIR", cases)
    monkeypatch.setattr(diagnose, "_FEEDBACK_PATH", cases / "diagnosis_feedback.jsonl")
    # 清零缓存，强制重新读取临时文件
    diagnose._ERRDICT_CACHE.update(mtime=0.0, data=None)
    diagnose._ROUTE_CACHE.update(mtime=0.0, data=None)
    diagnose._INFERRED_CACHE.update(mtime=0.0, data=None)
    return ref, cases


def test_feedback_learn_preview_only_does_not_mutate(monkeypatch, tmp_path):
    """apply=False 仅产出提案，不写 errdict.json / 路由索引。"""
    ref, cases = _patch_feedback_env(tmp_path, monkeypatch, errdict_codes={"17003": {"name": "EXECUTE_OPEN_API_ERROR"}})
    case_file = cases / "cf_case_19001_x.md"
    case_file.write_text("# case\n- errcode: 19001\n- model: employee\n", encoding="utf-8")
    (cases / "diagnosis_feedback.jsonl").write_text(
        json.dumps({
            "feedback_at": "2026-08-29 10:00:00", "case_file": "logs/cf_cases/cf_case_19001_x.md",
            "result": "wrong", "actual_root_cause": "TOKEN_EXPIRED", "fix_applied": True,
            "notes": "重新登录后恢复", "source": "manual",
        }, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    res = diagnose.cf_apply_feedback_learnings(apply=False)
    assert res["applied"] is False
    assert res["sample_count"] == 1
    actions = {p["action"] for p in res["proposals"]}
    assert "new_errdict" in actions
    assert "new_route" in actions
    # 真实文件未被改写
    after = json.loads((ref / "errdict.json").read_text(encoding="utf-8"))
    assert "19001" not in after["errcodes"]
    assert "四、反馈闭环自动补充" not in (ref / "ERROR_ROUTE_INDEX.md").read_text(encoding="utf-8")
    assert res["proposal_path"] is not None


def test_feedback_learn_apply_merges_and_appends_route(monkeypatch, tmp_path):
    """apply=True 备份后把新 errcode 写入 errdict.json 并追加路由行，且路由解析可命中。"""
    ref, cases = _patch_feedback_env(tmp_path, monkeypatch, errdict_codes={"17003": {"name": "EXECUTE_OPEN_API_ERROR"}})
    case_file = cases / "cf_case_19001_x.md"
    case_file.write_text("# case\n- errcode: 19001\n- model: employee\n- field: id_card\n", encoding="utf-8")
    (cases / "diagnosis_feedback.jsonl").write_text(
        json.dumps({
            "feedback_at": "2026-08-29 10:00:00", "case_file": "logs/cf_cases/cf_case_19001_x.md",
            "result": "wrong", "actual_root_cause": "TOKEN_EXPIRED", "fix_applied": True,
            "notes": "重新登录后恢复，参考 `01_FIELD_SCHEMA.md`", "source": "manual",
        }, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    res = diagnose.cf_apply_feedback_learnings(apply=True)
    assert res["applied"] is True
    assert "19001" in res["applied_changes"]["errdict_new"]
    assert "19001" in res["applied_changes"]["route_new"]

    after = json.loads((ref / "errdict.json").read_text(encoding="utf-8"))
    assert after["errcodes"]["19001"]["verified"] is True
    assert after["errcodes"]["19001"]["source"] == "feedback"
    assert "TOKEN_EXPIRED" in after["errcodes"]["19001"]["meaning"]

    route = (ref / "ERROR_ROUTE_INDEX.md").read_text(encoding="utf-8")
    assert "四、反馈闭环自动补充" in route
    assert "| 19001 |" in route

    # 路由解析能命中新追加的行
    idx = diagnose._parse_route_index()
    assert "19001" in idx["errcode"]
    assert idx["errcode"]["19001"]["docs"]


def test_feedback_learn_updates_existing_entry_with_feedback_field(monkeypatch, tmp_path):
    """已存在的 errcode 反馈不应覆盖人工释义，而是追加 feedback 字段。"""
    ref, cases = _patch_feedback_env(
        tmp_path, monkeypatch,
        errdict_codes={"17003": {"name": "EXECUTE_OPEN_API_ERROR", "meaning": "旧释义", "fix": "旧修复"}},
    )
    case_file = cases / "cf_case_17003_x.md"
    case_file.write_text("# case\n- errcode: 17003\n", encoding="utf-8")
    (cases / "diagnosis_feedback.jsonl").write_text(
        json.dumps({
            "feedback_at": "2026-08-29 10:00:00", "case_file": "logs/cf_cases/cf_case_17003_x.md",
            "result": "partially_correct", "actual_root_cause": "TOKEN_EXPIRED", "fix_applied": True,
            "notes": "其实还是 Token 过期", "source": "manual",
        }, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    res = diagnose.cf_apply_feedback_learnings(apply=True)
    after = json.loads((ref / "errdict.json").read_text(encoding="utf-8"))
    # 人工释义保留
    assert after["errcodes"]["17003"]["meaning"] == "旧释义"
    # 反馈内容挂到 feedback 列表
    assert after["errcodes"]["17003"]["feedback"]
    assert "TOKEN_EXPIRED" in after["errcodes"]["17003"]["feedback"][0]["root_cause"]
    assert "17003" in res["applied_changes"]["errdict_updated"]


def test_feedback_learn_skips_non_actionable_results(monkeypatch, tmp_path):
    """correct / unknown 反馈不进入闭环样本。"""
    ref, cases = _patch_feedback_env(tmp_path, monkeypatch)
    (cases / "diagnosis_feedback.jsonl").write_text(
        json.dumps({
            "feedback_at": "2026-08-29 10:00:00", "case_file": "logs/cf_cases/x.md",
            "result": "correct", "actual_root_cause": "TOKEN_EXPIRED", "fix_applied": True,
            "notes": "对的", "source": "manual",
        }, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    res = diagnose.cf_apply_feedback_learnings(apply=False)
    assert res["sample_count"] == 0
    assert res["proposals"] == []


def test_lookup_errdict_prefers_verified_over_inferred(monkeypatch):
    """主词典（errdict.json）优先；命中即标记 verified=True；仅在主词典缺失时回退推断词典。"""
    main = {"17003": {"name": "EXECUTE_OPEN_API_ERROR", "meaning": "官方释义", "fix": "重试"}}
    inferred = {"17003": {"name": "EXECUTE_OPEN_API_ERROR", "meaning": "推断释义", "fix": "x", "verified": False}}
    monkeypatch.setattr(diagnose, "_load_errdict", lambda: {"errcodes": main})
    monkeypatch.setattr(diagnose, "_load_inferred_errdict", lambda: {"errcodes": inferred})

    verified_hit = diagnose._lookup_errdict(17003)
    assert verified_hit["verified"] is True
    assert verified_hit["meaning"] == "官方释义"
    assert verified_hit["source"] == "errdict.json"


def test_lookup_errdict_falls_back_to_inferred(monkeypatch):
    """主词典缺失时，从推断词典取释义并保留 verified=False 标记。"""
    inferred = {
        "17099": {
            "name": "SOME_INFERRED_ERROR", "meaning": "由常量名推断的含义",
            "fix": "方向提示", "category": "other", "verified": False,
        }
    }
    monkeypatch.setattr(diagnose, "_load_errdict", lambda: {"errcodes": {}})
    monkeypatch.setattr(diagnose, "_load_inferred_errdict", lambda: {"errcodes": inferred})

    hit = diagnose._lookup_errdict(17099)
    assert hit is not None
    assert hit["name"] == "SOME_INFERRED_ERROR"
    assert hit.get("verified") is False


def test_reference_error_info_reports_two_coverage_layers(monkeypatch):
    """覆盖率应区分已校验（errdict.json）与含推断（errdict.json+inferred）两层。"""
    monkeypatch.setattr(diagnose, "_load_errdict", lambda: {"errcodes": {"17003": {}}})
    monkeypatch.setattr(diagnose, "_load_inferred_errdict", lambda: {"errcodes": {"17003": {}, "17099": {}}})
    monkeypatch.setattr(
        diagnose, "_load_reference_error_catalog",
        lambda: {"codes": {"17003": [], "17099": [], "17100": []}, "available": True, "file": "errors.py"},
    )
    info = diagnose._reference_error_info(17003)
    assert info["verified_errdict_count"] == 1
    assert info["inferred_errdict_count"] == 2
    assert info["verified_coverage_percent"] == 33.3
    assert info["inferred_coverage_percent"] == 66.7
    assert info["missing_from_local_errdict"] == ["17100"]
