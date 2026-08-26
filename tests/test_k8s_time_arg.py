# -*- coding: utf-8 -*-
"""kubectl 时间参数容错校验（since/until 非法值不再让查询崩溃）。"""
import unittest

from api.k8s import routes_k8s as rk


class TestK8sTimeArg(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(rk._k8s_normalize_time_arg("since", "", False), (None, None))
        self.assertEqual(rk._k8s_normalize_time_arg("since", None, False), (None, None))
        self.assertEqual(rk._k8s_normalize_time_arg("since", "   ", False), (None, None))

    def test_valid_duration(self):
        for v in ("30m", "1h", "2d", "500ms", "1.5h", "90s"):
            self.assertEqual(rk._k8s_normalize_time_arg("since", v, False), (v, None))

    def test_invalid_since_rejected(self):
        # "error" 这类非法值：忽略该筛选并返回告警，而不是透传给 kubectl
        norm, warn = rk._k8s_normalize_time_arg("since", "error", False)
        self.assertIsNone(norm)
        self.assertIsNotNone(warn)
        # since 不接受绝对时间
        norm2, warn2 = rk._k8s_normalize_time_arg("since", "2026-08-25T10:00:00Z", False)
        self.assertIsNone(norm2)
        self.assertIsNotNone(warn2)

    def test_until_accepts_abs(self):
        norm, warn = rk._k8s_normalize_time_arg("until", "2026-08-25T10:00:00Z", True)
        self.assertEqual(norm, "2026-08-25T10:00:00Z")
        self.assertIsNone(warn)
        norm3, warn3 = rk._k8s_normalize_time_arg("until", "2026-08-25 10:00:00+08:00", True)
        self.assertEqual(norm3, "2026-08-25 10:00:00+08:00")
        self.assertIsNone(warn3)

    def test_invalid_until_rejected(self):
        norm, warn = rk._k8s_normalize_time_arg("until", "error", True)
        self.assertIsNone(norm)
        self.assertIsNotNone(warn)


if __name__ == "__main__":
    unittest.main()
