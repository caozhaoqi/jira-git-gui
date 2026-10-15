# -*- coding: utf-8 -*-
"""Regression coverage for app-wide Jira connection configuration reminders."""
import asyncio
import unittest
from unittest import mock

import api.routes_repos as routes_repos


class TestRepoConfigAlert(unittest.TestCase):
    def test_missing_cookie_has_structured_signal(self):
        with mock.patch.object(routes_repos.client.config, "cookie", ""):
            response = asyncio.run(routes_repos.api_discover_repos())

        self.assertEqual(response["repos"], [])
        self.assertEqual(response["error"], "未配置 Cookie")
        self.assertEqual(response["config_missing"], "cookie")

    def test_cookie_configured_does_not_signal_missing_config(self):
        with mock.patch.object(routes_repos.client.config, "cookie", "test-cookie"):
            with mock.patch.object(routes_repos.client, "discover_repos", return_value=[]):
                response = asyncio.run(routes_repos.api_discover_repos())

        self.assertNotIn("config_missing", response)
        self.assertEqual(response["repos"], [])


class TestSessionExpiredSignal(unittest.TestCase):
    """会话失效要能从「接口响应本身」看出来，而不是只靠 SSE 送达。"""

    LOGIN_ERR = ("Jira 返回登录页：Cookie 已失效，请到「连接设置」重新粘贴 Cookie")

    def test_auth_payload_flags_session_errors(self):
        for err in (
            self.LOGIN_ERR,
            "Jira 返回的是登录页（HTTP 200）——Cookie 已失效",
            "HTTP 401 Unauthorized",
            "请求被拒绝（403）",
        ):
            with self.subTest(err=err):
                self.assertEqual(
                    routes_repos._auth_payload(err),
                    {"auth_failed": True, "auth_detail": err},
                )

    def test_auth_payload_ignores_ordinary_errors(self):
        # 普通错误绝不能报成会话失效：否则用户会去重贴 Cookie，真问题却在别处
        for err in ("路径不存在：/foo/bar", "二进制文件，请在文件树勾选后下载查看", "", None):
            with self.subTest(err=err):
                self.assertEqual(routes_repos._auth_payload(err or ""), {})

    def test_tree_marks_session_failure(self):
        with mock.patch.object(routes_repos.client, "repo_id", "123"), \
             mock.patch.object(routes_repos.client, "branch", "master"), \
             mock.patch.object(routes_repos.client, "list_level_ex",
                               return_value=([], self.LOGIN_ERR)), \
             mock.patch.object(routes_repos, "broadcast") as bc:
            response = asyncio.run(routes_repos.api_tree(path=""))

        self.assertTrue(response["auth_failed"])
        self.assertEqual(response["auth_detail"], self.LOGIN_ERR)
        # 顺手推 SSE（在线时横幅立即出现；离线由上面的标记兜底）
        self.assertEqual(bc.call_args[0][0], "cookie_expired")

    def test_tree_does_not_flag_normal_listing(self):
        with mock.patch.object(routes_repos.client, "repo_id", "123"), \
             mock.patch.object(routes_repos.client, "branch", "master"), \
             mock.patch.object(routes_repos.client, "list_level_ex",
                               return_value=([], "")), \
             mock.patch.object(routes_repos, "broadcast") as bc:
            response = asyncio.run(routes_repos.api_tree(path=""))

        self.assertNotIn("auth_failed", response)
        bc.assert_not_called()

    def test_file_marks_session_failure(self):
        with mock.patch.object(routes_repos.client, "get_file",
                               return_value=(None, self.LOGIN_ERR)), \
             mock.patch.object(routes_repos, "broadcast"):
            response = asyncio.run(routes_repos.api_file(path="a.txt"))

        self.assertTrue(response["auth_failed"])
        self.assertEqual(response["error"], self.LOGIN_ERR)

    def test_broadcast_skipped_for_ordinary_error(self):
        with mock.patch.object(routes_repos, "broadcast") as bc:
            routes_repos._broadcast_auth_if_needed("本地文件不存在：x")

        bc.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
