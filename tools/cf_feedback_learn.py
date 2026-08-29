#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断→规范闭环工具：根据人工反馈反哺 errdict.json 与 ERROR_ROUTE_INDEX.md。

用法：
    # 仅预览提案（默认，安全，不写任何文件）
    python tools/cf_feedback_learn.py preview
    # 备份后回写词典与路由索引
    python tools/cf_feedback_learn.py apply

反馈来源：logs/cf_cases/diagnosis_feedback.jsonl
输出提案：logs/cf_cases/feedback_learnings_proposal_<时间戳>.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.cf.cf_diagnose import cf_apply_feedback_learnings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="HCM 云函数诊断反馈闭环工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preview", help="仅生成提案预览，不回写文件（默认安全模式）")
    apply_p = sub.add_parser("apply", help="备份后回写 errdict.json 与路由索引")
    apply_p.add_argument("--max-proposals", type=int, default=100)

    args = parser.parse_args()

    if args.command == "preview":
        result = cf_apply_feedback_learnings(apply=False)
    else:
        result = cf_apply_feedback_learnings(apply=True, max_proposals=getattr(args, "max_proposals", 100))

    print(json.dumps({
        "applied": result.get("applied"),
        "sample_count": result.get("sample_count"),
        "applied_changes": result.get("applied_changes"),
        "proposal_path": result.get("proposal_path"),
        "proposals": result.get("proposals"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
