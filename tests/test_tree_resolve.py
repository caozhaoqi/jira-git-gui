# -*- coding: utf-8 -*-
"""路径直达 /api/tree/resolve 回归测试（离线，无真实 Jira 连接）。

守住「粘贴完整路径直达」的三条不变量：

1. **N 层并发而非串行** —— 远端每层一次请求、单次 10s 量级，逐层展开是 N×T。
   这里必须并发，墙钟时间压到 ~1×T。这条是需求的核心，串行实现会让
   「直达」退化成「自动帮你点 N 下」，没有任何收益。

2. **路径不存在要明确指出断点** —— 远端对不存在的路径只回空树、不给 404，
   不校验的话用户粘错路径只会看到一片空白，无从判断是打错还是目录真空。

3. **拒绝 .. 越权** —— 本地目录模式下路径会拼到 local_dir 后面。

运行：./venv/bin/python -m pytest tests/test_tree_resolve.py -q
"""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

# 必须导入 api.server：各业务域 router 是在那里 include 到 app 上的，
# 只导入 api.common 拿到的 app 上没有任何路由（请求会全部 404）。
import api.server  # noqa: F401
from api.common import app
import api.routes_repos as _rr

# 一条 5 层的深路径，与真实使用场景一致
DEEP = "source/common/components/_business_component/recruit-portal-job"


class _Entry:
    def __init__(self, name, path, type_, size=None):
        self.name = name
        self.path = path
        self.type = type_
        self.size = size
        self.has_children = type_ == "dir"
        self.mtime = None


class _FakeClient:
    """替身：按固定树应答，并记录每次请求与并发峰值。"""

    def __init__(self, tree, delay=0.0):
        self.repo_id = "900"
        self.branch = "master"
        self._tree = tree        # {path: [Entry]}
        self._delay = delay
        self.calls = []
        self._lock = __import__("threading").Lock()
        self.max_concurrent = 0
        self._running = 0

    def _enter(self):
        with self._lock:
            self._running += 1
            self.max_concurrent = max(self.max_concurrent, self._running)

    def _exit(self):
        with self._lock:
            self._running -= 1

    def list_level_ex(self, repo_id, branch, path, refresh=False):
        self._enter()
        try:
            self.calls.append(path)
            if self._delay:
                time.sleep(self._delay)
            return list(self._tree.get(path, [])), ""
        finally:
            self._exit()

    def list_level_local_dir(self, local_dir, path):
        return list(self._tree.get(path, []))


def _deep_tree():
    """构造 DEEP 路径对应的目录树：tree[path] = 该目录下的条目。"""
    segs = DEEP.split("/")
    tree = {}
    parent = ""
    for i, seg in enumerate(segs):
        path = "/".join(segs[: i + 1])
        tree.setdefault(parent, []).append(_Entry(seg, path, "dir"))
        parent = path
    # 目标目录里放一个文件，用于验证「直达文件」分支
    tree[DEEP] = [_Entry("index.tsx", DEEP + "/index.tsx", "file", 123)]
    return tree


@pytest.fixture()
def client_fixture(monkeypatch):
    """安装替身客户端，返回 (TestClient, fake) 。"""
    fake = _FakeClient(_deep_tree())
    monkeypatch.setattr(_rr, "client", fake, raising=True)
    return TestClient(app), fake


def test_resolve_returns_all_levels(client_fixture):
    """直达一次拿回从根到目标的每一层，条目内容正确。"""
    c, fake = client_fixture
    r = c.get("/api/tree/resolve", params={"path": DEEP})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["error"] is None
    assert [lv["path"] for lv in data["levels"]] == [""] + [
        "/".join(DEEP.split("/")[: i + 1]) for i in range(len(DEEP.split("/")))
    ]
    # 每层都真的带回了条目，最后一层是目标目录的内容
    assert all(lv["entries"] for lv in data["levels"])
    assert data["target"] == {"path": DEEP, "type": "dir"}


def test_resolve_fetches_levels_concurrently(client_fixture):
    """不变量 1：N 层并发拉取，墙钟时间接近单次而非 N 倍。"""
    import api.routes_repos as rr
    fake = _FakeClient(_deep_tree(), delay=0.30)
    rr_client_backup = rr.client
    rr.client = fake
    try:
        c = TestClient(app)
        t0 = time.time()
        r = c.get("/api/tree/resolve", params={"path": DEEP})
        elapsed = time.time() - t0
        assert r.status_code == 200, r.text
        n_levels = len(r.json()["levels"])          # 6 层（根 + 5 段）
        assert n_levels == 6
        # 串行需要 6×0.30=1.8s；并发应显著低于此
        assert elapsed < 0.30 * n_levels * 0.6, \
            "各层是串行拉取的（耗时 %.2fs），直达没有意义" % elapsed
        assert fake.max_concurrent >= 2, \
            "完全没有并发，max_concurrent=%d" % fake.max_concurrent
    finally:
        rr.client = rr_client_backup


def test_resolve_reports_broken_path(client_fixture):
    """不变量 2：路径不存在时精确报出断点，而不是返回一堆空层。"""
    c, fake = client_fixture
    bad = "source/common/components/_nope/deeper"
    r = c.get("/api/tree/resolve", params={"path": bad})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["broken_at"] == "source/common/components/_nope"
    assert data["target"]["type"] == "missing"


def test_resolve_rejects_parent_traversal(client_fixture):
    """不变量 3：.. 一律拒绝（本地目录模式下会拼到 local_dir 后面）。"""
    c, fake = client_fixture
    r = c.get("/api/tree/resolve", params={"path": "a/../../etc/passwd"})
    assert r.status_code == 200, r.text
    assert ".." in (r.json().get("error") or ""), r.text
    assert not r.json().get("levels"), "被拒绝的路径不应返回任何层级"


def test_resolve_to_file_selects_file(client_fixture):
    """直达目标是文件时，target.type 为 file（前端据此选中并预览）。"""
    c, fake = client_fixture
    r = c.get("/api/tree/resolve", params={"path": DEEP + "/index.tsx"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["target"] == {"path": DEEP + "/index.tsx", "type": "file"}
    assert data["broken_at"] is None


def test_resolve_root_path(client_fixture):
    """空路径直达根目录：只有一层，target 为 dir。"""
    c, fake = client_fixture
    r = c.get("/api/tree/resolve", params={"path": ""})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["path"] == ""
    assert [lv["path"] for lv in data["levels"]] == [""]
    assert data["target"]["type"] == "dir"


def test_resolve_reports_request_level_error(client_fixture):
    """请求级失败（如 Cookie 失效）时原样带出，不再叠加误导性的「路径不存在」。"""
    c, fake = client_fixture

    def _boom(repo_id, branch, path, refresh=False):
        return [], "Jira 返回登录页：Cookie 已失效"

    fake.list_level_ex = _boom
    r = c.get("/api/tree/resolve", params={"path": DEEP})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "Cookie 已失效" in data["error"]
    assert data["broken_at"] is None, "已有请求级错误时不应再报路径断点"
    assert data["target"]["type"] == ""
