#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成/检查 HCM 云函数 AI 诊断源码索引。

用法：
    python tools/cf_diagnose_index.py build
    python tools/cf_diagnose_index.py status

参考源码目录默认读取 HCM_REFERENCE_ROOT，未设置时使用项目约定目录。
索引写入项目内：store/downloads/895/docs/metadata/reference/cf_source_index.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.cf.cf_diagnose import (  # noqa: E402
    _SOURCE_INDEX_PATH,
    _load_source_index,
    cf_rebuild_source_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="HCM 云函数 AI 诊断源码索引工具")
    parser.add_argument("command", choices=("build", "status"))
    args = parser.parse_args()

    if args.command == "build":
        result = cf_rebuild_source_index()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    data = _load_source_index()
    output = {
        "path": str(_SOURCE_INDEX_PATH),
        "exists": _SOURCE_INDEX_PATH.is_file(),
        "source_root": data.get("source_root", ""),
        "available": data.get("available", False),
        "file_count": data.get("file_count", 0),
        "generated_at": data.get("generated_at", ""),
        "schema": data.get("schema", ""),
        "reference_root_env": os.environ.get("HCM_REFERENCE_ROOT", ""),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
