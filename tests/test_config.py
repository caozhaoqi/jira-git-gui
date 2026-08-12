"""单元测试：core/config.py —— .env 解析与配置映射（不发起任何网络请求）。

运行：
    cd /Users/caozhaoqi/PycharmProjects/jira-git-gui
    PYTHONPATH=. ./venv/bin/python -m unittest tests.test_config -v
"""
import os
import tempfile
import unittest
from pathlib import Path

from core.config import _parse_env_file, build_config, load_config
from core.models import ConnectConfig


class TestParseEnvFile(unittest.TestCase):
    def test_comments_blanks_and_quotes(self):
        p = Path(tempfile.mktemp(suffix=".env"))
        p.write_text(
            "# 这是注释\n\n"
            "jira_url=https://jira.example.com/\n"
            "name='bob'\n"
            'token="abc=="\n'
            "spaced = value \n",
            encoding="utf-8",
        )
        d = _parse_env_file(p)
        p.unlink()
        self.assertEqual(d["jira_url"], "https://jira.example.com/")
        self.assertEqual(d["name"], "bob")
        self.assertEqual(d["token"], "abc==")
        self.assertEqual(d["spaced"], "value")
        self.assertNotIn("# 这是注释", d)

    def test_missing_file_returns_empty(self):
        d = _parse_env_file(Path(tempfile.mktemp()))
        self.assertEqual(d, {})


class TestBuildConfig(unittest.TestCase):
    def test_aliases_and_typo_tolerance(self):
        env = {
            "jira_url": "https://jira.hcmcloud.cn/",
            "persoanl_access_token": "TYPO_PAT",       # 拼写错误键
            "personal_access_token": "GOOD_PAT",        # 标准键优先
            "cookie": "JSESSIONID=abc",
        }
        cfg = build_config(env)
        self.assertEqual(cfg.jira_url, "https://jira.hcmcloud.cn")  # 去尾斜杠
        self.assertEqual(cfg.pat, "GOOD_PAT")                       # 标准键胜出
        self.assertEqual(cfg.cookie, "JSESSIONID=abc")
        self.assertEqual(cfg.mode, "pat")                          # 缺省 pat

    def test_explicit_mode_and_username(self):
        cfg = build_config({
            "jira_url": "https://x.com",
            "username": "hb_1150118968",
            "mode": "COOKIE",
            "cookie": "JSESSIONID=z",
        })
        self.assertEqual(cfg.username, "hb_1150118968")
        self.assertEqual(cfg.mode, "cookie")                      # 转小写

    def test_empty_yields_default(self):
        cfg = build_config({})
        self.assertIsInstance(cfg, ConnectConfig)
        self.assertEqual(cfg.jira_url, "")
        self.assertEqual(cfg.mode, "pat")


class TestLoadConfig(unittest.TestCase):
    def test_reads_existing_env(self):
        d = Path(tempfile.mkdtemp())
        (d / ".env").write_text(
            "jira_url=https://example.com\n"
            "personal_access_token=PT\n"
            "cookie=JSESSIONID=xyz\n",
            encoding="utf-8",
        )
        cfg, loaded, path = load_config(d)
        self.assertTrue(loaded)
        self.assertEqual(cfg.jira_url, "https://example.com")
        self.assertEqual(cfg.pat, "PT")
        self.assertEqual(cfg.cookie, "JSESSIONID=xyz")
        self.assertTrue(path.endswith(".env"))

    def test_missing_env_file(self):
        d = Path(tempfile.mkdtemp())
        cfg, loaded, path = load_config(d)
        self.assertFalse(loaded)
        self.assertEqual(cfg.jira_url, "")

    def test_os_environ_overrides_file(self):
        d = Path(tempfile.mkdtemp())
        (d / ".env").write_text("jira_url=https://file.com\n", encoding="utf-8")
        old = os.environ.get("JIRA_URL")
        os.environ["JIRA_URL"] = "https://env-wins.com"
        try:
            cfg, loaded, _ = load_config(d)
            self.assertTrue(loaded)
            self.assertEqual(cfg.jira_url, "https://env-wins.com")
        finally:
            if old is None:
                os.environ.pop("JIRA_URL", None)
            else:
                os.environ["JIRA_URL"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
