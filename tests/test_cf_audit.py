# -*- coding: utf-8 -*-
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cf_locate_retrofit", ROOT / "tools" / "cf_locate_retrofit.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_audit_detects_cloud_function_risks(tmp_path):
    source = tmp_path / "demo.py"
    source.write_text(
        """import traceback\n\nclass Demo:\n    def execute(self, **kwargs):\n        row = kwargs.get('row')\n        value = row['id_card']\n        print('token=', kwargs.get('token'))\n        try:\n            return value\n        except:\n            pass\n""",
        encoding="utf-8",
    )
    result = MODULE.audit_file(source)
    risk_types = {risk["type"] for risk in result["risks"]}
    assert result["has_execute"] is True
    assert "UNSAFE_SUBSCRIPT_ACCESS" in risk_types
    assert "NO_DIAGNOSTIC_CONTEXT" in risk_types
    assert "BARE_EXCEPT" in risk_types
    assert "POSSIBLE_SENSITIVE_LOG" in risk_types
    assert result["risk_level"] == "high"


def test_audit_reports_missing_execute(tmp_path):
    source = tmp_path / "not_cf.py"
    source.write_text("def helper():\n    return 1\n", encoding="utf-8")
    result = MODULE.audit_file(source)
    assert result["has_execute"] is False
    assert any(r["type"] == "NO_EXECUTE_ENTRYPOINT" for r in result["risks"])
    assert result["risk_level"] == "high"
