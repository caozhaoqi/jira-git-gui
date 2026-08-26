# -*- coding: utf-8 -*-
"""结构化文件 diff 格式化展示（仅展示层）回归测试。

覆盖：
- canonical_text 对 JSON / JSONC / XML 的规范化展开
- file_diff 对单行压缩文件生成行级 diff（不再整行标红）
- CRLF 早退逻辑不被破坏
- compute_diff 字节相等判定不被规范化影响（minified vs pretty 仍判 MODIFIED）
- 非结构化 / 解析失败文件原样返回，绝不抛异常
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.diff import (
    DiffStatus, canonical_text, file_diff, compute_diff,
)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class CanonicalTextTest(unittest.TestCase):
    def test_minified_json_expanded(self):
        out = canonical_text("a.json", '{"name":"x","value":1}')
        self.assertIn("\n", out)
        self.assertIn('"name": "x"', out)
        self.assertIn('  "value": 1', out)

    def test_minified_xml_expanded(self):
        out = canonical_text("a.xml", "<root><a>1</a><b>2</b></root>")
        self.assertIn("\n", out)
        self.assertIn("<a>1</a>", out)
        self.assertIn("<b>2</b>", out)

    def test_jsonc_strips_comments(self):
        src = '{\n  // 注释\n  "k": 1 /* 行尾注释 */\n}'
        out = canonical_text("a.jsonc", src)
        self.assertNotIn("//", out)
        self.assertNotIn("/*", out)
        self.assertIn('"k": 1', out)

    def test_non_structured_passthrough(self):
        src = "just a line\nanother line"
        self.assertEqual(canonical_text("a.txt", src), src)

    def test_invalid_json_passthrough(self):
        # 解析失败必须原样返回，不能抛异常
        src = "{not valid json"
        self.assertEqual(canonical_text("a.json", src), src)

    def test_empty_passthrough(self):
        self.assertEqual(canonical_text("a.json", ""), "")


class FileDiffFormatTest(unittest.TestCase):
    def _write(self, root: Path, name: str, content: str) -> Path:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_minified_json_line_level_diff(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lp = self._write(root, "c.json", '{"name":"x","value":1}')
            remote = '{"name":"x","value":2}'
            diff = file_diff(str(lp), remote)
            # 不再是整行标红：value 的两种取值应分别出现在 - / + 行
            self.assertIn('"value": 1', diff)
            self.assertIn('"value": 2', diff)
            self.assertIn("-", diff)
            self.assertIn("+", diff)

    def test_minified_xml_line_level_diff(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lp = self._write(root, "c.xml", "<root><a>1</a><b>2</b></root>")
            remote = "<root><a>1</a><b>9</b></root>"
            diff = file_diff(str(lp), remote)
            self.assertIn("<b>2</b>", diff)
            self.assertIn("<b>9</b>", diff)

    def test_crlf_only_still_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lp = self._write(root, "c.txt", "a\r\nb\r\nc")
            remote = "a\nb\nc"
            self.assertEqual(file_diff(str(lp), remote), "")


class ComputeDiffEqualityUnchangedTest(unittest.TestCase):
    def test_minified_vs_pretty_still_modified(self):
        # 仅格式化展示模式：字节相等判定不变，minified vs pretty 仍判 MODIFIED
        minified = '{"a":1,"b":2}'
        pretty = '{\n  "a": 1,\n  "b": 2\n}'
        local_files = {
            "c.json": {
                "size": len(minified),
                "hash": _md5(minified),
                "norm_hash": _md5(minified),
                "norm_size": len(minified),
            }
        }
        remote_files = {"c.json": {"size": len(pretty), "hash": _md5(pretty)}}
        res = compute_diff(local_files, remote_files, ignore_line_endings=True)
        entry = res.entries[0]
        self.assertEqual(entry.status, DiffStatus.MODIFIED)
        self.assertEqual(res.modified, 1)


if __name__ == "__main__":
    unittest.main()
