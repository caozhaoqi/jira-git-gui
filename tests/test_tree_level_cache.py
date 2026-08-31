# -*- coding: utf-8 -*-
"""远端文件树层级缓存回归测试（离线，无真实 Jira 连接）。

守住「list_level 短 TTL 缓存」的三条不变量：

1. **第二次同层请求必须零远端请求** —— 来回切目录 / 刷新页面 / 重复跑差异扫描时，
   同一 (repo, branch, path) 只能打一次 Jira，其余全部命中 ``core.cache``。

2. **空目录也算命中** —— 远端空目录解析出 ``[]``，若用真值判断（``if cached:``）
   会把空列表当成未命中，导致每个空目录都被反复回源。必须用 ``is not None`` 判定。

3. **失败响应绝不落盘** —— 登录页 / 404 / 解析不出树都提前 return，
   绝不能把空结果缓存成「这个目录是空的」，否则 Cookie 一失效就永久空树。

运行：./venv/bin/python -m pytest tests/test_tree_level_cache.py -q
"""
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.client import browse as _browse
from core.client.browse import BrowseMixin
from core import cache as _cache

BRANCH = "master"


def _page(files):
    """伪造 GIJBrowseGit.jspa 的异步渲染页面（内联 ns.data JSON）。"""
    import json
    return 'var ns = {}; ns.data = {"files":%s};' % json.dumps(files)


def _dir(name):
    return {"path": name, "name": name, "directory": True}


def _file(name):
    return {"path": name, "name": name, "directory": False}


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class _StubBrowse(BrowseMixin):
    """最小替身：只保留 _list_level_cookie_ex 依赖的接口。"""

    def __init__(self, pages, login_page=False):
        self.config = SimpleNamespace(
            mode="cookie", cookie="JSESSIONID=stub",
            jira_url="https://jira.example.com")
        self.repo_id = "900"
        self._pages = pages
        self._login_page = login_page
        self.http_paths = []       # 真实打到远端的 path 列表

    def _resolve_branch_ex(self, repo_id, branch):
        return (BRANCH, "")

    def cookie_headers(self):
        return {"Cookie": "JSESSIONID=stub"}

    def http_get(self, url, headers=None, retries=5, watchdog=None):
        m = re.search(r"[?&]path=([^&]*)", url)
        p = unquote(m.group(1)) if m else ""
        self.http_paths.append(p)
        if self._login_page:
            return _Resp("<title>登录到 Atlassian - Jira</title>"
                         '<input name="os_username">')
        return _Resp(self._pages.get(p, self._pages.get("__default__", "")))


@pytest.fixture()
def tmp_cache(tmp_path, monkeypatch):
    """把 core.cache 的落盘目录重定向到临时目录，避免污染真实缓存。"""
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path / "cache", raising=True)
    return tmp_path / "cache"


def test_second_call_hits_cache(tmp_cache):
    """不变量 1：同一层第二次请求不再打 Jira。"""
    c = _StubBrowse({"": _page([_dir("src"), _file("README.md")])})
    first, err = c._list_level_cookie_ex("900", BRANCH, "")
    assert err == "" and len(first) == 2
    assert c.http_paths == [""]

    second, err = c._list_level_cookie_ex("900", BRANCH, "")
    assert err == "" and [e.name for e in second] == ["src", "README.md"]
    assert c.http_paths == [""], "第二次应命中缓存，实际又打了远端：%r" % c.http_paths


def test_cache_is_scoped_by_path_and_repo(tmp_cache):
    """不同 path / 不同 repo 互不串味。"""
    c = _StubBrowse({
        "": _page([_dir("a")]),
        "a": _page([_file("a/1.txt")]),
    })
    c._list_level_cookie_ex("900", BRANCH, "")
    c._list_level_cookie_ex("900", BRANCH, "a")
    c._list_level_cookie_ex("901", BRANCH, "")
    assert sorted(c.http_paths) == ["", "", "a"]

    # 全部命中
    c._list_level_cookie_ex("900", BRANCH, "")
    c._list_level_cookie_ex("900", BRANCH, "a")
    c._list_level_cookie_ex("901", BRANCH, "")
    assert len(c.http_paths) == 3, "应全部命中缓存：%r" % c.http_paths


def test_empty_dir_is_cached_as_hit(tmp_cache):
    """不变量 2：空目录解析出 [] 也算命中（不能用真值判断）。"""
    c = _StubBrowse({"": _page([])})
    first, err = c._list_level_cookie_ex("900", BRANCH, "")
    assert err == "" and first == []
    assert c.http_paths == [""]

    c._list_level_cookie_ex("900", BRANCH, "")
    c._list_level_cookie_ex("900", BRANCH, "")
    assert c.http_paths == [""], \
        "空目录被当成未命中而反复回源：%r" % c.http_paths


def test_failure_is_never_cached(tmp_cache):
    """不变量 3：登录页（失败响应）不落盘，下次仍会重试远端。"""
    c = _StubBrowse({}, login_page=True)
    entries, err = c._list_level_cookie_ex("900", BRANCH, "src")
    assert entries == [] and err, "Cookie 失效必须带出原因，而不是静默返回空"
    c._list_level_cookie_ex("900", BRANCH, "src")
    assert len(c.http_paths) == 2, "失败响应被写进缓存了：%r" % c.http_paths

    # 恢复可用后能立刻拿到数据（没被错误缓存挡住）
    c._login_page = False
    c._pages = {"src": _page([_file("src/a.py")])}
    entries, err = c._list_level_cookie_ex("900", BRANCH, "src")
    assert err == "" and len(entries) == 1, "缓存污染导致恢复后仍拿不到数据"


def test_unparsable_page_is_never_cached(tmp_cache):
    """浏览页 200 但解析不出树 —— 同样不落盘（判据同失败响应）。"""
    c = _StubBrowse({"": "<html><body>no tree here</body></html>"})
    entries, err = c._list_level_cookie_ex("900", BRANCH, "")
    assert entries == [] and err
    c._list_level_cookie_ex("900", BRANCH, "")
    assert len(c.http_paths) == 2, "解析失败的页面被缓存了：%r" % c.http_paths


def test_refresh_bypasses_cache_and_rewrites(tmp_cache):
    """refresh=True 强制回源，并把新结果写回缓存。"""
    c = _StubBrowse({"": _page([_file("old.txt")])})
    c._list_level_cookie_ex("900", BRANCH, "")
    assert c.http_paths == [""]

    entries, _ = c._list_level_cookie_ex("900", BRANCH, "", refresh=True)
    assert c.http_paths == ["", ""], "refresh 未绕过缓存"

    # 新结果已回写：后续普通请求命中新版
    entries, _ = c._list_level_cookie_ex("900", BRANCH, "")
    assert c.http_paths == ["", ""], "refresh 后未回写缓存"
    assert [e.name for e in entries] == ["old.txt"]


def test_invalidate_remote_tree_cache(tmp_cache):
    """整体失效：清完后再请求必须回源。"""
    c = _StubBrowse({"": _page([_dir("a")])})
    c._list_level_cookie_ex("900", BRANCH, "")
    c._list_level_cookie_ex("901", BRANCH, "")

    cleared = _browse.invalidate_remote_tree_cache()
    assert cleared >= 2, "整体失效清掉的条目数不对：%r" % cleared

    c._list_level_cookie_ex("900", BRANCH, "")
    c._list_level_cookie_ex("901", BRANCH, "")
    assert len(c.http_paths) == 4, "失效后应重新回源：%r" % c.http_paths


def test_invalidate_single_repo_only(tmp_cache):
    """按 repo 失效：只清指定仓库。"""
    c = _StubBrowse({"": _page([_dir("a")])})
    c._list_level_cookie_ex("900", BRANCH, "")
    c._list_level_cookie_ex("901", BRANCH, "")

    _browse.invalidate_remote_tree_cache("900")
    c._list_level_cookie_ex("900", BRANCH, "")   # 回源
    c._list_level_cookie_ex("901", BRANCH, "")   # 仍命中
    assert c.http_paths == ["", "", ""], "按 repo 失效范围不对：%r" % c.http_paths
