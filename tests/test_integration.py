"""集成测试：真正访问 jira.hcmcloud.cn 验证「发现仓库 → 查看文件」全链路。

这些测试需要真实凭据，默认**跳过**：设置以下环境变量后才会执行，
且建议在本机（有网络/代理可达）运行，而不是沙箱。

    export JIRA_URL=https://jira.hcmcloud.cn
    export JIRA_COOKIE="JSESSIONID=...; atlassian.xsrf.token=..."
    # 可选：PAT 克隆链路
    export JIRA_PAT="<Personal Access Token>"
    export JIRA_USERNAME="hb_1150118968"
    export JIRA_REPO_ID="1032"
    export JIRA_REPO_NAME="hcm-cloud-vue"

运行：
    cd /Users/caozhaoqi/PycharmProjects/jira-git-gui
    PYTHONPATH=. ./venv/bin/python -m unittest tests.test_integration -v
"""
import os
import unittest

from core.client import JiraGitClient


JIRA_URL = os.environ.get("JIRA_URL", "https://jira.hcmcloud.cn")
JIRA_COOKIE = os.environ.get("JIRA_COOKIE")
JIRA_PAT = os.environ.get("JIRA_PAT")
JIRA_USERNAME = os.environ.get("JIRA_USERNAME", "")
JIRA_REPO_ID = os.environ.get("JIRA_REPO_ID")
JIRA_REPO_NAME = os.environ.get("JIRA_REPO_NAME", "")

skip_cookie = unittest.skipUnless(
    JIRA_COOKIE, "未设置 JIRA_COOKIE，跳过 Web 集成测试")
skip_pat = unittest.skipUnless(
    JIRA_PAT and JIRA_REPO_ID and JIRA_REPO_NAME,
    "未设置 JIRA_PAT / JIRA_REPO_ID / JIRA_REPO_NAME，跳过 PAT 克隆集成测试")


class TestDiscoverIntegration(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()
        self.c.config.jira_url = JIRA_URL
        self.c.config.cookie = JIRA_COOKIE or ""

    @skip_cookie
    def test_discover_via_all_repos_page(self):
        """从 AllRepositories 页面真实发现仓库列表。"""
        repos = self.c.discover_repos()
        self.assertTrue(repos, "未从 AllRepositories 页面发现任何仓库（检查 Cookie 是否有效）")
        for r in repos:
            self.assertTrue(r.repo_id.isdigit(), f"repo_id 应为数字：{r.repo_id!r}")
            self.assertTrue(r.display_name, "display_name 不应为空")
        print(f"  [集成] 发现 {len(repos)} 个仓库，示例："
              f"{repos[0].display_name} (id={repos[0].repo_id}, branch={repos[0].default_branch or '默认'})")

    @skip_cookie
    def test_view_files_of_first_repo(self):
        """发现后选中第一个仓库，验证能拉到文件树根目录。"""
        repos = self.c.discover_repos()
        self.assertTrue(repos, "未发现仓库")
        r = repos[0]
        self.c.set_repo(r.repo_id, r.display_name, r.default_branch)
        entries = self.c.list_level("")
        self.assertIsInstance(entries, list)
        print(f"  [集成] 仓库 {r.display_name} 根目录返回 {len(entries)} 个条目")
        for e in entries[:5]:
            print(f"    - {e.type}: {e.path}")


class TestCloneIntegration(unittest.TestCase):
    def setUp(self):
        self.c = JiraGitClient()
        self.c.config.jira_url = JIRA_URL
        self.c.config.pat = JIRA_PAT or ""
        self.c.config.username = JIRA_USERNAME

    @skip_pat
    def test_clone_repo(self):
        """用 PAT 真实克隆一个仓库（写入 store/repos/<repoId>）。"""
        ok, msg, path = self.c.clone_repo(
            JIRA_REPO_ID, JIRA_REPO_NAME, "", JIRA_PAT, JIRA_USERNAME)
        print(f"  [集成] 克隆结果 ok={ok} msg={msg} path={path}")
        # 即便已存在本地克隆（ok=True, 走 fetch 分支）也应视为成功
        self.assertTrue(ok, f"克隆失败：{msg}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
