# -*- coding: utf-8 -*-
"""差异合并路由回归测试（离线，无真实 Jira 连接）。

守住两类曾真实发生、且后果严重到「静默丢失用户代码」的缺陷：

1. **参数顺序写反**
   ``get_file_cached`` 的签名是 ``(client, path, namespace, ttl, use_cache)``。
   ``/api/diff/merge`` 与 ``/api/diff/merge-batch`` 曾写成
   ``get_file_cached(client, namespace, req.path, ...)`` —— 拿 namespace
   （如 "895"）当文件路径去抓取，必然失败。同样的错误在 ``/api/diff/file``
   上早已修好，这两条路由却漏了。

2. **抓取失败时静默清空本地文件（数据丢失）**
   抓取失败会返回 ``None``，旧代码写 ``merge_to_local(dir, path, content or "")``，
   把 ``None`` 退化成空串；而 ``merge_to_local`` 内部是 ``open(target, "w")``，
   于是本地已有的文件被**截断为 0 字节**且接口仍返回 ``ok: true``。
   正确行为：取不到远端内容就中止，绝不改写本地文件。

运行：./venv/bin/python -m pytest tests/test_diff_merge_routes.py -s
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.routes_diff as _rd  # noqa: E402

_LOCAL_BODY = "PRECIOUS LOCAL CONTENT\n"


class _StubClient:
    """最小替身：路由只用到 client.repo_id 来拼 namespace。"""
    def __init__(self, repo_id="895"):
        self.repo_id = repo_id


def _install_spy(monkeypatch, returns):
    """替换 get_file_cached：按真实签名落参并记录，返回预设内容。

    签名必须一字不差地照抄 ``core.diff.scan_remote.get_file_cached``，
    这样一旦调用方把 path / namespace 写反，被记录的字段就会暴露出来。
    """
    calls = []

    def spy(client, path, namespace="default", ttl=86400,
            use_cache=True, content_hash=""):
        calls.append({"path": path, "namespace": namespace})
        return returns(path) if callable(returns) else returns

    monkeypatch.setattr(_rd._differ, "get_file_cached", spy)
    monkeypatch.setattr(_rd, "client", _StubClient("895"))
    return calls


def _seed(tmp_path, rel="keep.txt"):
    local = tmp_path / "local"
    local.mkdir()
    target = local / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_LOCAL_BODY, encoding="utf-8")
    return local, target


# ---- 1. 参数顺序 ------------------------------------------------------------ #
def test_merge_passes_path_not_namespace(monkeypatch, tmp_path):
    """/api/diff/merge 必须把「文件路径」放在 path 位、「repo_id」放在 namespace 位。"""
    calls = _install_spy(monkeypatch, returns="remote body\n")
    local, _ = _seed(tmp_path)

    resp = asyncio.run(_rd.api_diff_merge(
        _rd.MergeReq(local_dir=str(local), path="keep.txt", use_cache=False)))

    assert resp["ok"] is True, resp
    assert calls, "应调用 get_file_cached"
    assert calls[0]["path"] == "keep.txt", \
        "path 位传成了 %r（把 namespace 当路径了？）" % calls[0]["path"]
    assert calls[0]["namespace"] == "895", \
        "namespace 位传成了 %r（应为 repo_id）" % calls[0]["namespace"]
    assert (local / "keep.txt").read_text() == "remote body\n"


def test_merge_batch_passes_path_not_namespace(monkeypatch, tmp_path):
    """批量合并同样不能写反。"""
    calls = _install_spy(monkeypatch, returns="remote body\n")
    local, _ = _seed(tmp_path)

    res = asyncio.run(_rd.api_diff_merge_batch(
        [_rd.MergeReq(local_dir=str(local), path="keep.txt", use_cache=False)]))

    assert res["results"][0]["ok"] is True, res
    assert calls[0]["path"] == "keep.txt", \
        "path 位传成了 %r" % calls[0]["path"]
    assert calls[0]["namespace"] == "895", \
        "namespace 位传成了 %r" % calls[0]["namespace"]


# ---- 2. 抓取失败不得改写本地文件 -------------------------------------------- #
def test_merge_fetch_failure_does_not_truncate_local_file(monkeypatch, tmp_path):
    """回归核心：远端取不到内容时，本地文件必须原封不动。

    旧实现会写空串 → open(target,"w") 把文件截断为 0 字节，且仍返回 ok。
    """
    _install_spy(monkeypatch, returns=None)  # 模拟抓取失败
    local, target = _seed(tmp_path)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_rd.api_diff_merge(
            _rd.MergeReq(local_dir=str(local), path="keep.txt", use_cache=False)))

    assert ei.value.status_code == 502, ei.value.status_code
    assert target.read_text() == _LOCAL_BODY, \
        "本地文件被改写（数据丢失！）实际内容：%r" % target.read_text()


def test_merge_batch_fetch_failure_skips_and_keeps_local(monkeypatch, tmp_path):
    """批量合并：单个文件抓取失败只应计入失败，不得清空本地文件。"""
    _install_spy(monkeypatch, returns=None)
    local, target = _seed(tmp_path)

    res = asyncio.run(_rd.api_diff_merge_batch(
        [_rd.MergeReq(local_dir=str(local), path="keep.txt", use_cache=False)]))

    row = res["results"][0]
    assert row["ok"] is False, "抓取失败不应记为成功：%r" % row
    assert "远端内容获取失败" in (row["error"] or ""), row
    assert target.read_text() == _LOCAL_BODY, \
        "本地文件被改写（数据丢失！）实际内容：%r" % target.read_text()


# ---- 3. 边界：远端确实是空文件时应照常写入 --------------------------------- #
def test_merge_writes_empty_remote_file(monkeypatch, tmp_path):
    """远端 0 字节文件时 content 为 b''（不是 None），属合法内容，必须正常写入。

    防止「防截断」的守卫被写成过度防御（把 b'' 也当成失败跳过）。
    """
    _install_spy(monkeypatch, returns=b"")
    local, target = _seed(tmp_path)

    resp = asyncio.run(_rd.api_diff_merge(
        _rd.MergeReq(local_dir=str(local), path="keep.txt", use_cache=False)))

    assert resp["ok"] is True, resp
    assert target.read_bytes() == b"", "空远端内容应把本地文件清空，实际：%r" % target.read_bytes()
