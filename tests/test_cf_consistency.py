# -*- coding: utf-8 -*-
"""云函数「线上 ↔ 本地」一致性看板 + 部署台账 回归测试（离线，无真实网关）。

## 这个模块守的是什么

云函数部署**绕过了 CI/CD**（`.gitlab-ci.yml` 只推 4 个镜像，云函数不在其中）：
现场做法是把源码写成 Python 字符串字面量、用 ``hcm.model.edit(DynamicScriptList)``
直推平台，靠 md5 比对确认。于是没有 diff 评审、没有版本台账、没有回滚点。

本测试锁死这条链路上**最容易悄悄坏掉、坏了又最贵**的几件事：

1. **归一化**：线上脚本经平台往返后必然带 CRLF / 行尾空格 / 首尾空行。
   若直接比 md5，252 个函数会 100% 判成「不一致」，看板当场失去意义。
   同时台账留档必须用**原始** md5，否则失去字节级还原的依据。

2. **五种状态**：in_sync / differs / only_online / dup_local / only_local。
   尤其 ``dup_local``——「同一 id 命中多个本地文件」本身就是需要暴露的问题
   （现场源码散落在 dongruan_sync / _cf_online_backup / output/env121_stripped），
   不能静默取第一个了事。

3. **推送前必须先归档**：``push`` 一旦先写平台再归档，回滚点就永远拿不到了。
   这里用「调用顺序断言」锁死：fetch(归档) 一定早于 write(推送)。

4. **台账条目 id 不能在同一秒内相撞**：现场最自然的操作就是「推完发现不对、
   立刻回滚」，一秒内完成完全正常。相撞的后果是回滚条目写进推送条目的目录、
   把 ``before.py`` 覆盖掉 —— 这个模块存在的全部理由当场失效。

5. **一次动作在台账里只能有一行**：push 分两步落盘（先归档、写完补结果），
   早先索引是纯 append，于是同一次部署显示两行（先「失败」后「成功」），
   前端 ``key={entry_id}`` 还会撞 React key。

6. **网关不可达要明确失败**：不能假装扫出「全部 only_online」。

运行：./venv/bin/python -m pytest tests/test_cf_consistency.py -s
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.cf import cf_consistency as C  # noqa: E402

GW = "http://gw.test"


# --------------------------------------------------------------------------- #
#  夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """台账落到临时目录（不污染真实 data_root）。"""
    d = tmp_path / "ledger"
    d.mkdir()
    monkeypatch.setattr(C, "ledger_root", lambda: d)
    return d


class FakeGateway:
    """最小网关替身：一份 {id: 源码} + 调用顺序记录。"""

    def __init__(self, scripts: dict):
        self.scripts = dict(scripts)
        self.calls: list = []

    async def fetch_online_list(self, target, token="", page_size=2000):
        return [
            {
                "id": k,
                "category": "private.dongruan_sync",
                "description": f"desc {k}",
                "enabled": True,
                "modify_date": "2026-10-14 17:02:11",
                "modifier_id": 1001,
            }
            for k in self.scripts
        ]

    async def fetch_online_script(self, target, cf_id, token=""):
        self.calls.append(("fetch", cf_id))
        return self.scripts.get(cf_id)

    async def write_online_script(self, target, cf_id, script, token="",
                                  enabled=True, verify_retries=3):
        self.calls.append(("write", cf_id))
        self.scripts[cf_id] = script
        return {"ok": True, "attempts": 1, "md5": C.source_md5(script)}


def install(monkeypatch, gw: FakeGateway) -> FakeGateway:
    monkeypatch.setattr(C, "fetch_online_list", gw.fetch_online_list)
    monkeypatch.setattr(C, "fetch_online_script", gw.fetch_online_script)
    monkeypatch.setattr(C, "write_online_script", gw.write_online_script)
    return gw


def run(coro):
    return asyncio.run(coro)


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
#  1. 归一化与指纹
# --------------------------------------------------------------------------- #
def test_normalize_ignores_crlf_trailing_space_and_blank_edges():
    """平台往返必然带来的无意义差异，不该被判成「内容不同」。"""
    online = "class SyncA(BasePrivateApiService):\r\n    x = 1   \r\n\r\n\r\n"
    local = "class SyncA(BasePrivateApiService):\n    x = 1\n"
    assert C.normalize_source(online) == C.normalize_source(local)
    assert C.source_md5(online) == C.source_md5(local)


def test_raw_md5_stays_byte_exact_for_ledger():
    """台账用原始 md5：归一化后相同，但字节不同必须能区分（否则还原无依据）。"""
    a = "x = 1\r\n"
    b = "x = 1\n"
    assert C.source_md5(a) == C.source_md5(b)
    assert C.raw_md5(a) != C.raw_md5(b)


def test_normalize_blank_only_content_is_consistent():
    """空脚本与全空白脚本必须归一到同一个值，否则同一个「空」算出两个 md5。"""
    vals = {C.normalize_source(None), C.normalize_source(""),
            C.normalize_source("   "), C.normalize_source("\n\n\t\n")}
    assert vals == {""}, f"空内容归一化不一致：{vals!r}"
    assert C.source_md5("") == C.source_md5("   \n\n")


def test_normalize_keeps_meaningful_leading_indent():
    """只裁行尾与首尾空行，不能碰行首缩进（Python 语义所在）。"""
    src = "\n\n    indented = 1\n\n"
    assert C.normalize_source(src) == "    indented = 1\n"


@pytest.mark.parametrize("raw,want", [
    ("ConvJingcheEmp", "conv_jingche_emp"),
    ("SyncGdyyIncremental", "sync_gdyy_incremental"),
    ("SyncGdyyIncrementalV2", "sync_gdyy_incremental_v2"),
    ("HTTPServer", "http_server"),
    ("U_CodeTable", "u_code_table"),
    ("AlreadySnake", "already_snake"),
    ("", ""),
])
def test_snake_case(raw, want):
    assert C.snake_case(raw) == want


def test_declared_classes():
    src = (
        "import os\n"
        "\n"
        "class SyncA(BasePrivateApiService):\n"
        "    pass\n"
        "\n"
        "class SyncB:\n"
        "    pass\n"
        "\n"
        "# class Commented(Base):  不应命中\n"
        "def helper():\n"
        "    class Nested(Base):\n"
        "        pass\n"
    )
    assert C.declared_classes(src) == ["SyncA", "SyncB", "Nested"]


# --------------------------------------------------------------------------- #
#  2. 本地索引与三级映射
# --------------------------------------------------------------------------- #
def _clean_tree(tmp_path: Path):
    """文件名与线上 id 完全同名（现场的实际命名）+ 两个本地根。

    两个根是有意的：``_cf_online_backup/`` 与工作目录各存一份同名文件，
    正是 ``dup_local`` 的现实来源。
    """
    r1 = tmp_path / "work"
    r2 = tmp_path / "backup"
    write(r1 / "SyncAlpha.py", "class SyncAlpha(BasePrivateApiService):\n    pass\n")
    write(r1 / "SyncBeta.py", "class SyncBeta(BasePrivateApiService):\n    pass\n")
    write(r1 / "SyncDup.py", "class SyncDup(BasePrivateApiService):\n    a = 1\n")
    write(r2 / "SyncDup.py", "class SyncDup(BasePrivateApiService):\n    a = 2\n")
    write(r1 / "SyncOrphan.py", "class SyncOrphan(BasePrivateApiService):\n    pass\n")
    write(r1 / ".hidden.py", "class Hidden(Base):\n    pass\n")   # 应被跳过
    return [str(r1), str(r2)]


def _class_named_tree(tmp_path: Path):
    """文件名与 id 不同：一份靠类名映射，一份文件名写成 snake_case。"""
    r = tmp_path / "work"
    write(r / "legacy_name.py", "class SyncGamma(BasePrivateApiService):\n    pass\n")
    write(r / "sync_delta.py", "class SyncDelta(BasePrivateApiService):\n    pass\n")
    return [str(r)]


def test_build_local_index_skips_hidden_and_dedups_two_roots(tmp_path):
    idx = C.build_local_index(_clean_tree(tmp_path))

    assert idx["files"] == 5, f"隐藏文件不该计入：{idx['files']}"
    assert idx["errors"] == []
    assert [Path(i["path"]).name for i in idx["by_id"]["syncalpha"]] == ["SyncAlpha.py"]
    assert len(idx["by_id"]["syncdup"]) == 2, "两个根下的同名文件都要保留"
    assert "hidden" not in idx["by_id"]


def test_build_local_index_indexes_declared_class_names(tmp_path):
    """文件名对不上时，靠文件内 ``class Xxx(...)`` 的 snake_case 建键。"""
    idx = C.build_local_index(_class_named_tree(tmp_path))
    assert [Path(i["path"]).name for i in idx["by_id"]["sync_gamma"]] == ["legacy_name.py"]


def test_build_local_index_reports_missing_root(tmp_path):
    idx = C.build_local_index([str(tmp_path / "nope")])
    assert idx["files"] == 0
    assert any("本地根不存在" in e for e in idx["errors"])


def test_match_local_prefers_filename_stem_then_class_name(tmp_path):
    idx = C.build_local_index(_class_named_tree(tmp_path))

    # 文件名写成 snake_case：要靠 id → snake_case 回退才认得出来（否则白报 only_online）
    cands, how = C.match_local(idx, "SyncDelta")
    assert how == C.MATCH_STEM and Path(cands[0]["path"]).name == "sync_delta.py"

    # 文件名完全无关：只能靠类名映射
    cands, how = C.match_local(idx, "SyncGamma")
    assert how == C.MATCH_ENTRY and Path(cands[0]["path"]).name == "legacy_name.py"

    assert C.match_local(idx, "NoSuchFunction") == ([], "")
    assert C.match_local(idx, "") == ([], "")


def test_match_local_returns_all_candidates_for_dup(tmp_path):
    idx = C.build_local_index(_clean_tree(tmp_path))
    cands, _ = C.match_local(idx, "SyncDup")
    assert len(cands) == 2
    assert len({c["path"] for c in cands}) == 2


# --------------------------------------------------------------------------- #
#  3. 五种状态
# --------------------------------------------------------------------------- #
def _board(tmp_path, monkeypatch):
    """构造一个能同时覆盖五种状态的场景。"""
    roots = _clean_tree(tmp_path)
    gw = FakeGateway({
        "SyncAlpha": "class SyncAlpha(BasePrivateApiService):\n    pass\n",   # in_sync
        "SyncBeta": "class SyncBeta(BasePrivateApiService):\n    pass\n    changed = True\n",
        "SyncGone": "class SyncGone(Base):\n    pass\n",                      # only_online
        "SyncDup": "class SyncDup(BasePrivateApiService):\n    a = 1\n",      # dup_local
    })
    install(monkeypatch, gw)
    return roots, run(C.scan(target=GW, roots=roots))


def test_scan_reports_all_five_states(tmp_path, monkeypatch):
    _, res = _board(tmp_path, monkeypatch)

    assert res["ok"] is True
    assert res["online_count"] == 4
    assert res["summary"] == {
        C.ST_IN_SYNC: 1,
        C.ST_DIFFERS: 1,
        C.ST_ONLY_ONLINE: 1,
        C.ST_DUP_LOCAL: 1,
        C.ST_UNKNOWN: 0,
        C.ST_ONLY_LOCAL: 1,          # SyncOrphan.py
    }

    by_id = {e["id"]: e for e in res["entries"]}
    assert by_id["SyncAlpha"]["status"] == C.ST_IN_SYNC
    assert by_id["SyncBeta"]["status"] == C.ST_DIFFERS
    assert by_id["SyncGone"]["status"] == C.ST_ONLY_ONLINE
    assert by_id["SyncGone"]["local_path"] == ""
    assert by_id["SyncDup"]["status"] == C.ST_DUP_LOCAL
    assert len(by_id["SyncDup"]["dup_paths"]) == 1

    assert [x["id"] for x in res["local_only"]] == ["SyncOrphan"]


def test_scan_without_content_leaves_mapped_entries_unknown(tmp_path, monkeypatch):
    """不拉源码时只做映射：已映射的停在 unknown，不能瞎猜 in_sync。"""
    roots = _clean_tree(tmp_path)
    gw = FakeGateway({"SyncAlpha": "x", "SyncGone": "y"})
    install(monkeypatch, gw)

    res = run(C.scan(target=GW, roots=roots, with_content=False))
    assert res["summary"][C.ST_UNKNOWN] == 1        # SyncAlpha（已映射但未取内容）
    assert res["summary"][C.ST_ONLY_ONLINE] == 1    # SyncGone
    assert res["summary"][C.ST_IN_SYNC] == 0
    assert res["summary"][C.ST_DIFFERS] == 0
    assert all(not e["online_md5"] for e in res["entries"])
    assert not any(c[0] == "fetch" for c in gw.calls), "with_content=False 不该拉任何源码"


def test_scan_gateway_down_fails_loudly(tmp_path, monkeypatch):
    """网关不可达必须明确失败 —— 绝不能假装扫出「全部 only_online」。"""
    roots = _clean_tree(tmp_path)

    async def boom(*a, **kw):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(C, "fetch_online_list", boom)
    with pytest.raises(RuntimeError, match="connect refused"):
        run(C.scan(target=GW, roots=roots))


def test_scan_requires_target_and_roots(tmp_path, monkeypatch):
    """未配置网关 / 未配置源码根时给出可读错误，而不是 TypeError。"""
    monkeypatch.setattr(C, "default_target", lambda: "")
    monkeypatch.setattr(C, "default_local_roots", lambda: [])

    with pytest.raises(RuntimeError, match="未指定 HCM 网关地址"):
        run(C.scan(target="", roots=[str(tmp_path)]))

    with pytest.raises(RuntimeError, match="未找到本地源码根"):
        run(C.scan(target=GW, roots=[]))


# --------------------------------------------------------------------------- #
#  4. diff
# --------------------------------------------------------------------------- #
def test_diff_reports_unified_diff_with_normalization(tmp_path, monkeypatch):
    local = write(tmp_path / "SyncA.py", "a = 1\nb = 2\n")
    gw = FakeGateway({"SyncA": "a = 1\nb = 3\n"})
    install(monkeypatch, gw)

    d = run(C.diff_one("SyncA", str(local), GW))
    assert d["same"] is False
    assert "-b = 2" in d["diff"]
    assert "+b = 3" in d["diff"]
    assert d["local"]["exists"] is True


def test_diff_treats_crlf_only_difference_as_same(tmp_path, monkeypatch):
    local = write(tmp_path / "SyncA.py", "a = 1\nb = 2\n")
    gw = FakeGateway({"SyncA": "a = 1\r\nb = 2\r\n\r\n"})
    install(monkeypatch, gw)

    d = run(C.diff_one("SyncA", str(local), GW))
    assert d["same"] is True
    assert d["diff"] == ""


def test_diff_handles_missing_local_file(tmp_path, monkeypatch):
    gw = FakeGateway({"SyncGone": "a = 1\n"})
    install(monkeypatch, gw)

    d = run(C.diff_one("SyncGone", "", GW))
    assert d["same"] is False
    assert d["local"]["exists"] is False
    assert "本地无对应文件" in d["diff"]


# --------------------------------------------------------------------------- #
#  5. 台账：条目 id 唯一 + 一次动作一行
# --------------------------------------------------------------------------- #
def test_entry_id_does_not_collide_within_same_second():
    """「推完立刻回滚」是常态操作，一秒内两次动作不能拿到同一个 id。"""
    ids = {C._new_entry_id("SyncGdyyIncremental") for _ in range(200)}
    assert len(ids) == 200, "同一秒内产生了重复 entry_id"


def test_ledger_record_upserts_one_row_per_entry(ledger):
    """push 分两步落盘，索引里必须只留最终那一行（否则台账显示两行、前端撞 key）。"""
    eid = C._new_entry_id("SyncA")
    meta = {"entry_id": eid, "cf_id": "SyncA", "action": "push", "ok": False, "ts": "t0"}
    C.ledger_record(meta, "old\n", "new\n")
    meta = {**meta, "ok": True, "attempts": 2}
    C.ledger_record(meta, None, None)          # 只更新元信息，不删已归档文件

    lst = C.ledger_list()
    assert lst["total"] == 1, f"同一次动作出现了 {lst['total']} 行"
    row = lst["entries"][0]
    assert row["ok"] is True and row["attempts"] == 2
    assert row["has_before"] is True

    entry = C.ledger_entry(eid)
    assert entry["before_script"] == "old\n"   # None 不能把已归档内容抹掉
    assert entry["after_script"] == "new\n"


def test_ledger_entry_missing_raises(ledger):
    with pytest.raises(RuntimeError, match="台账条目不存在"):
        C.ledger_entry("nope-123")


# --------------------------------------------------------------------------- #
#  6. push：必须先归档再写
# --------------------------------------------------------------------------- #
OLD = "class SyncA(BasePrivateApiService):\n    version = 1\n"
NEW = "class SyncA(BasePrivateApiService):\n    version = 2\n"


def test_push_archives_online_version_before_writing(ledger, tmp_path, monkeypatch):
    local = write(tmp_path / "SyncA.py", NEW)
    gw = install(monkeypatch, FakeGateway({"SyncA": OLD}))

    res = run(C.push("SyncA", str(local), GW))
    assert res["ok"] is True

    # 顺序锁：归档(fetch) 一定早于 写入(write)
    assert ("fetch", "SyncA") in gw.calls and ("write", "SyncA") in gw.calls
    assert gw.calls.index(("fetch", "SyncA")) < gw.calls.index(("write", "SyncA"))

    entry = C.ledger_entry(res["entry_id"])
    assert entry["before_script"] == OLD, "回滚点必须是推送前的线上内容"
    assert entry["after_script"] == NEW
    assert entry["action"] == "push"
    assert gw.scripts["SyncA"] == NEW


def test_push_refuses_ambiguous_local_source(ledger, tmp_path, monkeypatch):
    """同一 id 命中多份本地文件时必须报错，不能猜一个推上去。"""
    roots = _clean_tree(tmp_path)
    install(monkeypatch, FakeGateway({"SyncDup": "x"}))
    with pytest.raises(RuntimeError, match="命中多个本地文件"):
        run(C.push("SyncDup", "", GW, roots=roots))


def test_push_without_rollback_point_is_recorded_and_rollback_refuses(
        ledger, tmp_path, monkeypatch):
    """归档失败仍允许推送（不能因为备份失败就卡住现场），但要如实记「无回滚点」。"""
    local = write(tmp_path / "SyncA.py", NEW)
    gw = FakeGateway({"SyncA": OLD})
    install(monkeypatch, gw)

    async def archive_down(target, cf_id, token=""):
        raise RuntimeError("归档超时")

    monkeypatch.setattr(C, "fetch_online_script", archive_down)

    res = run(C.push("SyncA", str(local), GW))
    assert res["ok"] is True, "归档失败不该阻断推送"
    assert C.ledger_entry(res["entry_id"])["before_script"] is None

    with pytest.raises(RuntimeError, match="没有归档的线上版本"):
        run(C.rollback(res["entry_id"], GW))


# --------------------------------------------------------------------------- #
#  7. rollback：把线上恢复到归档版本
# --------------------------------------------------------------------------- #
def test_rollback_restores_archived_version(ledger, tmp_path, monkeypatch):
    local = write(tmp_path / "SyncA.py", NEW)
    gw = install(monkeypatch, FakeGateway({"SyncA": OLD}))

    pushed = run(C.push("SyncA", str(local), GW))
    assert gw.scripts["SyncA"] == NEW

    rb = run(C.rollback(pushed["entry_id"], GW))
    assert rb["ok"] is True
    assert rb["rollback_of"] == pushed["entry_id"]
    assert gw.scripts["SyncA"] == OLD, "线上应回到推送前的版本"

    # 推送与回滚通常在同一秒内发生：两条台账必须各自独立、互不覆盖
    assert rb["entry_id"] != pushed["entry_id"]
    assert C.ledger_entry(pushed["entry_id"])["before_script"] == OLD
    assert C.ledger_entry(rb["entry_id"])["action"] == "rollback"


def test_rollback_records_its_own_undo_point(ledger, tmp_path, monkeypatch):
    """回滚本身也要留 before，否则「回滚错了」就再也回不去。"""
    local = write(tmp_path / "SyncA.py", NEW)
    gw = install(monkeypatch, FakeGateway({"SyncA": OLD}))
    pushed = run(C.push("SyncA", str(local), GW))

    rb = run(C.rollback(pushed["entry_id"], GW))
    entry = C.ledger_entry(rb["entry_id"])
    assert entry["before_script"] == NEW     # 回滚前的线上内容（= 刚推上去的）
    assert entry["after_script"] == OLD


# --------------------------------------------------------------------------- #
#  8. pull：线上 → 本地，原文件先备份
# --------------------------------------------------------------------------- #
def test_pull_overwrites_local_and_keeps_bak(ledger, tmp_path, monkeypatch):
    local = write(tmp_path / "SyncA.py", "local old\n")
    install(monkeypatch, FakeGateway({"SyncA": NEW}))

    res = run(C.pull("SyncA", str(local), GW))
    assert res["ok"] is True
    assert local.read_text(encoding="utf-8") == NEW
    assert Path(res["backup"]).read_text(encoding="utf-8") == "local old\n"
    assert Path(res["backup"]).name.endswith(".py.bak")

    entry = C.ledger_entry(res["entry_id"])
    assert entry["action"] == "pull"
    assert entry["after_script"] == NEW


def test_pull_creates_file_when_no_local_path(ledger, tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    install(monkeypatch, FakeGateway({"SyncNew": NEW}))

    res = run(C.pull("SyncNew", "", GW, roots=[str(root)]))
    assert Path(res["local_path"]).parent == root
    assert Path(res["local_path"]).name == "SyncNew.py"
    assert Path(res["local_path"]).read_text(encoding="utf-8") == NEW
    assert res["backup"] == ""


def test_pull_errors_when_online_function_missing(ledger, tmp_path, monkeypatch):
    install(monkeypatch, FakeGateway({}))
    with pytest.raises(RuntimeError, match="不存在或未返回 script"):
        run(C.pull("SyncNope", str(tmp_path / "x.py"), GW))


# --------------------------------------------------------------------------- #
#  9. 路由层：注册齐 + 网关不可达是 502 而不是 500
# --------------------------------------------------------------------------- #
def test_routes_registered():
    import api.server  # noqa: F401  —— 必须 import 才有全部路由

    paths = api.server.app.openapi()["paths"]
    for p in (
        "/api/cf-consistency/config",
        "/api/cf-consistency/scan",
        "/api/cf-consistency/diff",
        "/api/cf-consistency/pull",
        "/api/cf-consistency/push",
        "/api/cf-consistency/ledger",
        "/api/cf-consistency/entry",
        "/api/cf-consistency/rollback",
    ):
        assert p in paths, f"路由未注册：{p}"


def test_scan_route_maps_gateway_failure_to_502(ledger, monkeypatch):
    """路由层把「网关不可达」翻成 502（上游故障），而不是 500（本程序 bug）。"""
    from fastapi.testclient import TestClient
    from api.auth import API_TOKEN

    import api.server
    from api.common import app

    async def boom(*a, **kw):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(C, "fetch_online_list", boom)
    monkeypatch.setattr(C, "default_target", lambda: GW)
    monkeypatch.setattr(C, "default_local_roots", lambda: ["/tmp"])

    headers = {"X-API-Token": API_TOKEN}
    with TestClient(app, headers=headers, raise_server_exceptions=False) as c:
        r = c.post("/api/cf-consistency/scan", json={})
    assert r.status_code == 502, r.text
    assert "connect refused" in r.text

    # 未给 id 的写接口应被参数校验拦下（422），不会真的去连网关
    with TestClient(app, headers=headers, raise_server_exceptions=False) as c:
        assert c.post("/api/cf-consistency/push", json={}).status_code == 422
        assert c.post("/api/cf-consistency/diff", json={}).status_code == 422
    _ = api.server  # 保持 import 显式，便于阅读
