"""数据模型。"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ConnectConfig:
    """连接配置（对应 UI 连接面板）。"""
    jira_url: str = ""
    username: str = ""
    mode: str = "pat"      # "pat" | "cookie"
    pat: str = ""          # Personal Access Token
    cookie: str = ""       # JSESSIONID=...; atlassian.xsrf.token=...


@dataclass
class RepoInfo:
    """发现的仓库。"""
    repo_id: str = ""
    display_name: str = ""
    clone_url: str = ""
    default_branch: str = ""  # 从 AllRepositories 页面 repoId 链接里解析到的 branchName


@dataclass
class TreeEntry:
    """文件树中的一个条目（单层）。"""
    name: str
    path: str
    type: str              # "dir" | "file"
    size: Optional[int] = None
    has_children: bool = False


@dataclass
class CommitFile:
    """某次提交涉及的一个文件变更。"""
    path: str = ""
    change_type: str = ""      # MODIFIED / ADDED / DELETED / RENAMED ...
    lines_added: int = 0
    lines_removed: int = 0


@dataclass
class Commit:
    """一条提交记录（来源：Jira Git 插件 issues/{key}/commits）。"""
    commit_id: str = ""
    display_id: str = ""       # 短 SHA（前 8 位），用于紧凑展示
    author: str = ""
    date: str = ""
    message: str = ""
    branch: str = ""
    repository_name: str = ""
    files: List[CommitFile] = field(default_factory=list)
