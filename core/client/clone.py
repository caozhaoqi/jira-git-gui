# -*- coding: utf-8 -*-
"""JiraGitClient 的「git 克隆 / PAT 诊断 / 断点续传清单」Mixin。

拆分自 ``core/client.py``。负责：构造 PAT 克隆所需的账号/密码候选、执行 git clone、
认证失败诊断，以及批量下载 / 整库下载用的断点续传清单读写。

共享常量在此重新定义（与 ``core/client.py`` 顶部一致），避免与聚合主类形成循环 import。
"""
import json
import subprocess
import urllib.parse
from pathlib import Path
from typing import Callable, List, Optional

from core.constants import REPOS_DIR
from .connection import ConnectionMixin  # 复用其 b64_prefix_account 等静态工具

# 断点续传清单文件名
_MANIFEST_NAME = ".jira_git_manifest.json"


class CloneMixin:
    """git 克隆 / 诊断 / 清单能力。"""

    # ----------------------------------------------------------- git 克隆
    @staticmethod
    def _clone_user_candidates(pat: str, username: str) -> list:
        """为 PAT 克隆构造 username 候选（去重/去空，按优先级排序）。

        Jira Git 插件要求 username 为 PAT 所属账号、PAT 本身作 password。
        PAT 前缀 base64 解码后通常内嵌账号 ID，最权威，放最前；
        其次为用户显式配置的用户名。空候选会被丢弃（避免用空用户名发起无效克隆）。
        """
        cands: list = []
        acct = ConnectionMixin.b64_prefix_account(pat)
        for c in (acct, username):
            c = (c or "").strip()
            if c and c not in cands:
                cands.append(c)
        return cands

    def clone_repo(self, repo_id: str, repo_name: str, branch: str,
                   pat: str, username: str,
                   on_log: Optional[Callable[[str], None]] = None) -> tuple:
        """git clone 到本地，返回 (ok, msg, local_path)。"""
        def log(m):
            if on_log:
                on_log(m)

        host = self.host_of(self.config.jira_url)
        local_path = REPOS_DIR / str(repo_id)
        if local_path.exists():
            try:
                subprocess.run([self._git_bin, "-C", str(local_path), "fetch", "--all"],
                               capture_output=True, text=True, timeout=120)
                log("已存在本地克隆，已 fetch 更新")
                return True, "已存在，已 fetch 更新", str(local_path)
            except Exception as ex:
                return True, f"已存在本地克隆（fetch 跳过：{ex}）", str(local_path)

        candidates = self._clone_user_candidates(pat, username)
        if not candidates:
            return False, ("克隆失败：缺少可用的 username（PAT 未内嵌账号且未配置用户名）。"
                           "请在「连接设置」填写用户名后重试。"), None

        _enc = lambda p: urllib.parse.quote(p, safe="")
        passwords = [_enc(pat)]
        secret = self._pat_secret(pat)
        if secret:
            passwords.append(_enc(secret))

        last_err = ""
        auth_rejected = False
        for pw in passwords:
            for user in candidates:
                clone_url = (f"https://{user}:{pw}@{host}"
                             f"/git/{repo_id}/{repo_name}.git")
                cmd = [self._git_bin, "-c", "credential.helper=", "clone", "--depth", "1"]
                if branch:
                    cmd += ["-b", branch]
                cmd += [clone_url, str(local_path)]
                log(f"正在克隆（用户 {user}）...")
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if res.returncode == 0:
                        return True, f"克隆成功（用户 {user}）", str(local_path)
                    combined = (res.stderr or "") + "\n" + (res.stdout or "")
                    last_err = combined.strip().splitlines()[-1] if combined.strip() else ""
                    if any(k in last_err for k in (
                        "login.jsp", "permissionViolation", "Authentication failed",
                        "fatal: Authentication", "401",
                        "remote: Invalid username or password",
                    )):
                        auth_rejected = True
                except subprocess.TimeoutExpired:
                    last_err = "克隆超时"
                except Exception as ex:
                    last_err = str(ex)

        if auth_rejected:
            return False, self._pat_diag(pat, username), None
        return False, f"克隆失败：{last_err}", None

    # ----------------------------------------------------- PAT 诊断 / 轻量连通测试
    def _pat_diag(self, pat: str, username: str) -> str:
        """构造 PAT 认证被拒时的诊断信息（克隆与快速测试共用）。"""
        acct = self.b64_prefix_account(pat) or username
        secret = self._pat_secret(pat)
        tried = "完整 PAT" + (" + 内嵌密钥" if secret else "")
        host = self.host_of(self.config.jira_url)
        return ("认证被服务器拒绝（凭据无效，或该账号无此仓库克隆权限）。\n"
                f"（已分别用「{tried}」两种方式尝试验证，均被拒绝。）\n"
                "请确认：\n"
                "  ① PAT 有效且未过期 / 未吊销；\n"
                f"  ② 该 PAT 所属账号（{acct}）对仓库 {self.repo_id}/{self.repo_name} 有浏览/克隆权限；\n"
                "  ③ 必要时在 Jira 重新生成 PAT（克隆范围）。\n"
                "可先用终端手动验证，以排除是 GUI 问题：\n"
                f"  git ls-remote https://{acct}:<PAT>@{host}/git/{self.repo_id}/{self.repo_name}.git")

    def _pat_test_quick(self, pat: str, username: str) -> tuple:
        """用 ``git ls-remote --heads`` 秒级验证 PAT 能否访问指定仓库（不克隆、不下载）。

        相比完整 ``git clone``（最长 300s 且会拉取大量对象），速度快且鉴权失败立即返回诊断。
        返回 (ok, msg)。
        """
        host = self.host_of(self.config.jira_url)
        users = self._clone_user_candidates(pat, username)
        if not users:
            return False, ("缺少可用的 username（PAT 未内嵌账号且未配置用户名）。"
                           "请在「连接设置」填写用户名后重试。")
        passwords = [urllib.parse.quote(pat, safe="")]
        secret = self._pat_secret(pat)
        if secret:
            passwords.append(urllib.parse.quote(secret, safe=""))
        ident = f"{self.repo_id}/{self.repo_name}"
        for pw in passwords:
            for user in users:
                url = f"https://{user}:{pw}@{host}/git/{ident}.git"
                try:
                    res = subprocess.run(
                        [self._git_bin, "ls-remote", "--heads", url],
                        capture_output=True, text=True, timeout=30)
                except subprocess.TimeoutExpired:
                    return False, "PAT 探测超时（ls-remote 无响应）"
                except Exception as ex:
                    return False, f"PAT 探测异常：{ex}"
                combined = (res.stderr or "") + "\n" + (res.stdout or "")
                if res.returncode == 0:
                    n = len([l for l in res.stdout.splitlines() if l.strip()])
                    return True, f"PAT 认证通过（用户 {user}，远端分支数 {n}）"
                if any(k in combined for k in (
                        "permissionViolation", "Authentication failed", "401",
                        "Invalid username or password", "fatal: Authentication",
                        "fatal: unable to access")):
                    return False, self._pat_diag(pat, username)
        return False, "PAT 认证失败（ls-remote 未返回有效结果，请检查仓库 ID / 名称）。"

    # ------------------------------------------------------ 断点续传清单
    def _manifest_path(self, dest_root) -> Path:
        return Path(dest_root) / _MANIFEST_NAME

    def _load_manifest(self, dest_root) -> dict:
        """载入断点续传清单：{path: size}。已存在且大小一致的文件可跳过。"""
        p = self._manifest_path(dest_root)
        try:
            if p.exists():
                return dict(json.loads(p.read_text(encoding="utf-8")).get("files", {}))
        except Exception:
            pass
        return {}

    def _save_manifest(self, dest_root, manifest: dict) -> None:
        """即时落盘断点续传清单，确保中断后再次运行能跳过已完成文件。"""
        p = self._manifest_path(dest_root)
        try:
            p.write_text(json.dumps({"files": manifest}, ensure_ascii=False),
                         encoding="utf-8")
        except Exception:
            pass
