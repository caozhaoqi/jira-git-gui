# -*- coding: utf-8 -*-
"""测试 Clash 默认值配置读取（config/clash_defaults.local.json）。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api import routes_clash as rc


def _write(tmp: Path, data):
    (tmp / "clash_defaults.local.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


class TestClashDefaults(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._dir = Path(self._tmp.name)

    def _patch_file(self):
        return mock.patch.object(
            rc, "_CLASH_DEFAULTS_FILE", self._dir / "clash_defaults.local.json"
        )

    def test_read_valid(self):
        _write(self._dir, {"default_ips": ["73.2.3.27", "73.2.192.1", "83.0.16.1", "73.8.0.10"], "lan_device": "en13"})
        with self._patch_file():
            d = rc._load_clash_defaults()
        self.assertEqual(d["default_ips"], ["73.2.3.27", "73.2.192.1", "83.0.16.1", "73.8.0.10"])
        self.assertEqual(d["lan_device"], "en13")

    def test_missing_file(self):
        with self._patch_file():
            d = rc._load_clash_defaults()
        self.assertEqual(d["default_ips"], [])
        self.assertEqual(d["lan_device"], "")

    def test_corrupt_json(self):
        (self._dir / "clash_defaults.local.json").write_text("{ not json", encoding="utf-8")
        with self._patch_file():
            d = rc._load_clash_defaults()
        self.assertEqual(d["default_ips"], [])

    def test_blank_entries_filtered(self):
        _write(self._dir, {"default_ips": ["  ", "73.2.3.27", "", " 73.8.0.10 "], "lan_device": "  "})
        with self._patch_file():
            d = rc._load_clash_defaults()
        self.assertEqual(d["default_ips"], ["73.2.3.27", "73.8.0.10"])
        self.assertEqual(d["lan_device"], "")


if __name__ == "__main__":
    unittest.main()
