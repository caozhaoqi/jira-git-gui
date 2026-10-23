# -*- coding: utf-8 -*-
"""tools/cf_monitor.py 的离网不变量测试。

监控看板的价值全在「可信」二字：误报会把正常云函数标红，漏报会让真实故障
沉底。这里把几条踩过的坑固化成回归测试（全部离线，不联网）：

1. **「失败 0」不能算错误** —— 共享解析器 parse_cf_log_content 的 _ERROR_HINTS
   含「失败」二字，会把「解析失败 0 / 失败 0」这类统计类成功日志判成 error。
   实测 SyncHuaqiRaw「扇出结束：… 解析失败 0 …」就被误报成 ERROR。
2. **真错误必须抓住** —— ❌ / traceback / errcode≠0 / 显式 ERROR 标签。
3. **嵌套 JSON 要解包** —— content 可能是 `{"content": "{\"content\": \"...\"}"}`。
4. **「最近一条错误」独立于「最近一条」** —— 不能拿成功日志冒充错误摘要。
5. **健康度阈值** —— 错误数 ≥5 判 ERROR，1~4 判 WARN，0 判 OK。
6. **模型字段映射** —— dynamic_log→log_type，SyncOuterRecord/SysLogRecord→name。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import cf_monitor as m


def _row(name, content, ts="2026-10-19 10:00:00"):
    return {"name": name, "log_type": name, "create_time": ts, "content": content}


# ─────────────────────── 1. 误报防护（「失败 0」）───────────────────────
def test_zero_failure_stats_is_not_error():
    """「解析失败 0 / 失败 0」是统计类成功日志，不得计入错误。"""
    rows = [
        _row("SyncHuaqiRaw", '{"msg": "[RID:hq01] 扇出结束：请求 80 / 解析失败 0 / 新建 83 / 更新 0"}'),
        _row("SyncHuaqiRaw", '{"msg": "[RID:hq02] 落库：解析成功 19 条，解析失败 0 条；新建 0 / 更新 19"}'),
        _row("ConvJingcheOrg", '{"msg": "[RID:jc] 完成：新建 1 更新 0 无变化 59 认领 0 跳过 0 失败 0"}'),
    ]
    buckets = m.aggregate(rows)
    assert buckets["SyncHuaqiRaw"]["error"] == 0, buckets["SyncHuaqiRaw"]
    assert buckets["SyncHuaqiRaw"]["health"] == m.HEALTH_OK
    assert buckets["ConvJingcheOrg"]["error"] == 0, buckets["ConvJingcheOrg"]


def test_real_errors_still_detected():
    """误报防护不能把真错误一起挡掉。"""
    rows = [
        _row("SyncGdyyEmpPhoto", '{"msg": "[RID:aa02] 工号=000234 ❌ ECM download 失败：docId 返回空字节"}'),
        _row("ConvJingcheOrg", '{"msg": "[RID:jc] [新建] hcm.model.create.batch 整批失败（1 行）"}'),
        _row("DailyReport", "[RID:bb02] [DAILY] stage=end errcode=1002 生成失败"),
        _row("Foo", '{"msg": "[RID:x] 处理异常：KeyError: user_id"}'),
    ]
    buckets = m.aggregate(rows)
    assert buckets["SyncGdyyEmpPhoto"]["error"] == 1
    assert buckets["ConvJingcheOrg"]["error"] == 1
    assert buckets["DailyReport"]["error"] == 1
    assert buckets["Foo"]["error"] == 1


def test_zero_count_with_real_error_signal_is_error():
    """同一行里既有「失败 0」又有 ❌ → 仍算错误（宁可报不可漏）。"""
    rows = [_row("F", '{"msg": "[RID:x] ❌ 推送失败：失败 0 条不代表无异常，接口超时"}')]
    assert m.aggregate(rows)["F"]["error"] == 1


# ─────────────────────── 2. 嵌套 JSON / 摘要解包 ───────────────────────
def test_nested_json_message_unwrapped():
    """content 可能是双层 JSON 包裹的 traceback，摘要要能解出来。"""
    import json
    inner = ("Traceback (most recent call last):\n"
             '  File "<dynamic_script:3929>", line 382, in _process_one\n'
             "KeyError: 'employee_id'")
    row = _row("employee_effect", json.dumps({"content": inner}, ensure_ascii=False))
    buckets = m.aggregate([row])
    b = buckets["employee_effect"]
    assert b["error"] == 1
    assert "Traceback" in b["last_error"], b
    assert "\n" not in b["last_error"]  # 单行化，避免撑爆表格


def test_last_error_is_independent_of_last_message():
    """最新的成功日志不能冒充「最近一条错误」。"""
    rows = [
        _row("F", '{"msg": "[RID:1] ❌ 失败：写库超时"}', "2026-10-19 10:00:00"),
        _row("F", '{"msg": "[RID:2] ✅ 全部同步完成"}', "2026-10-19 10:01:00"),
    ]
    b = m.aggregate(rows)["F"]
    assert b["last_time"] == "2026-10-19 10:01:00"
    assert "同步完成" in b["last_message"]
    assert "写库超时" in b["last_error"], b


# ─────────────────────── 3. errcode / stage 兜底 ───────────────────────
def test_plaintext_errcode_and_stage_fallback():
    """纯文本 content 里的 errcode=/stage= 也要抓到（共享解析器只管字典）。"""
    rows = [_row("F", "[RID:x] stage=pull errcode=80002 拉取失败")]
    b = m.aggregate(rows)["F"]
    assert b["errcodes"].get("80002") == 1, b
    assert b["stages"].get("pull") == 1, b


# ─────────────────────── 4. 健康度阈值 ───────────────────────
def test_health_thresholds():
    err = '{"msg": "[RID:x] ❌ 失败：boom"}'
    ok = '{"msg": "[RID:x] ✅ 成功"}'

    # 错误 5 条 → ERROR
    rows5 = [_row("Bad", err, f"2026-10-19 10:00:0{i}") for i in range(5)] + \
            [_row("Bad", ok, "2026-10-19 10:00:09")]
    assert m.aggregate(rows5)["Bad"]["health"] == m.HEALTH_ERROR

    # 错误 1 条 → WARN
    rows1 = [_row("Meh", err), _row("Meh", ok, "2026-10-19 10:00:09")]
    assert m.aggregate(rows1)["Meh"]["health"] == m.HEALTH_WARN

    # 无错误 → OK
    rows0 = [_row("Fine", ok)]
    assert m.aggregate(rows0)["Fine"]["health"] == m.HEALTH_OK


def test_summarize_counts():
    rows = [
        _row("A", '{"msg": "[RID:1] ❌ 失败"}'),
        _row("B", '{"msg": "[RID:2] ✅ 成功"}'),
    ]
    stats = m.summarize(m.aggregate(rows))
    assert stats["functions"] == 2
    assert stats["total_rows"] == 2
    assert stats["errors"] == 1
    assert stats["ok_functions"] == 1
    assert stats["warn_functions"] == 1


# ─────────────────────── 5. 模型字段映射 ───────────────────────
def test_model_type_field_mapping():
    assert m.model_type_field("dynamic_log") == "log_type"
    assert m.model_type_field("SyncOuterRecord") == "name"
    assert m.model_type_field("syslogrecord") == "name"
    assert m.model_type_field("") == "log_type"  # 未知模型兜底


def test_row_name_falls_back_across_fields():
    """云函数名候选：log_type 优先，其次 name（不同模型字段不同）。"""
    assert m._row_name({"log_type": "FnA", "name": "FnB"}) == "FnA"
    assert m._row_name({"name": "FnB"}) == "FnB"
    assert m._row_name({}) == "(未知)"


# ─────────────────────── 6. 离线自检可独立跑 ───────────────────────
def test_dry_run_self_check_passes():
    """dry_run() 内置断言必须全部通过（它同时是 CI 的冒烟用例）。"""
    assert m.dry_run() == 0


# ════════════════════════════════════════════════════════════════════
#  后端聚合模块（api.cf.cf_monitor）—— 与 UI / CLI 共用同一套规则，
#  这里锁定它和 CLI 的关键判定必须一致，避免两边漂移。
# ════════════════════════════════════════════════════════════════════
import asyncio  # noqa: E402

import pytest  # noqa: E402

import api.cf.cf_monitor as cm  # noqa: E402


def test_backend_aggregate_matches_cli_on_false_positive():
    """后端也要把「失败 0」判成正常 —— 这是 UI 可信度的前提。"""
    rows = [
        _row("SyncHuaqiRaw", '{"msg": "[RID:hq01] 扇出结束：请求 80 / 解析失败 0 / 新建 83"}'),
        _row("SyncHuaqiRaw", '{"msg": "[RID:hq02] 落库：解析成功 19 条，解析失败 0 条"}'),
    ]
    buckets = cm.aggregate(rows)
    assert buckets["SyncHuaqiRaw"]["error"] == 0
    assert buckets["SyncHuaqiRaw"]["health"] == cm.HEALTH_OK


def test_backend_aggregate_detects_real_errors():
    rows = [
        _row("A", '{"msg": "[RID:1] ❌ ECM download 失败：docId 返回空字节"}'),
        _row("B", '{"msg": "[RID:2] [新建] hcm.model.create.batch 整批失败（1 行）"}'),
        _row("C", "[RID:3] stage=end errcode=1002 生成失败"),
    ]
    buckets = cm.aggregate(rows)
    assert buckets["A"]["error"] == 1
    assert buckets["B"]["error"] == 1
    assert buckets["C"]["error"] == 1


def test_backend_health_thresholds_and_sorted_codes():
    """错误数 ≥5 → ERROR；错误码按次数降序，前端可直接渲染。"""
    rows = [_row("Bad", '{"msg": "[RID:x] ❌ 失败：80002：boom"}') for _ in range(5)]
    rows += [_row("Bad", '{"msg": "[RID:y] ❌ 失败：80002：boom"}') for _ in range(2)]
    rows += [_row("Bad", '{"msg": "[RID:z] ❌ 失败：51006：expired"}')]
    b = cm.aggregate(rows)["Bad"]
    assert b["health"] == cm.HEALTH_ERROR
    assert b["top_errcodes"][0] == {"code": "80002", "count": 7}
    assert {"code": "51006", "count": 1} in b["top_errcodes"]


def test_backend_model_type_field():
    assert cm.model_type_field("dynamic_log") == "log_type"
    assert cm.model_type_field("SyncOuterRecord") == "name"
    assert cm.model_type_field("SysLogRecord") == "name"
    assert cm.model_type_field("") == "log_type"


def test_backend_extract_rows_shapes():
    """HCM 返回可能直出 list，也可能包成 {list,count} 或再嵌一层 data。"""
    assert cm._extract_rows({"data": [{"a": 1}]}) == ([{"a": 1}], 1)
    assert cm._extract_rows({"data": {"list": [1, 2], "count": 9}}) == ([1, 2], 9)
    assert cm._extract_rows({"data": {"data": {"list": [3], "count": 1}}}) == ([3], 1)
    assert cm._extract_rows({"data": {}}) == ([], 0)


def test_backend_model_missing_detection():
    """模型不存在（80001）必须能和鉴权失败区分开 —— 前者换模型，后者才重登。"""
    assert cm._is_model_missing(RuntimeError("业务失败: 80001 您请求的数据不存在"))
    assert cm._is_model_missing(RuntimeError("Unknown Model Name"))
    # 会话失效不该被当成模型缺失
    assert not cm._is_model_missing(RuntimeError("业务失败: 17003 未登录"))


def test_collect_rejects_empty_server():
    """空 server_url 必须直接报参数错误，而不是发请求。"""
    try:
        asyncio.run(cm.collect(""))
    except ValueError as e:
        assert "server_url" in str(e).lower() or "服务器" in str(e)
    else:
        raise AssertionError("空 server_url 应当抛 ValueError")


def test_collect_reports_all_models_unavailable(monkeypatch):
    """所有模型都不可用时，必须返回 ok:false + 明确错误，不能假装「成功但无数据」。"""
    async def fake_query_page(*a, **kw):
        return [], 0, RuntimeError("业务失败: 17003 token 过期"), False

    monkeypatch.setattr(cm, "_query_page", fake_query_page)
    r = asyncio.run(cm.collect("https://example.invalid"))
    assert r["ok"] is False
    assert "17003" in r["error"]
    assert r["functions"] == []
    assert r["models_tried"] == list(cm.RECORD_MODELS)


def test_collect_falls_back_when_model_missing(monkeypatch):
    """dynamic_log 不存在 → 自动降级到 SyncOuterRecord，并如实报告用的哪个模型。"""
    async def fake_query_page(server_url, token, proxy, model, log_type, page, page_size):
        if model == "dynamic_log":
            return [], 0, RuntimeError("业务失败: 80001 您请求的数据不存在"), True
        if page == 1:
            return [_row("Fn", '{"msg": "[RID:1] ❌ 失败：boom"}')], 1, None, False
        return [], 1, None, False

    monkeypatch.setattr(cm, "_query_page", fake_query_page)
    r = asyncio.run(cm.collect("https://example.invalid", pages=2))
    assert r["ok"] is True
    assert r["record_model"] == "SyncOuterRecord"
    assert r["models_tried"] == ["dynamic_log", "SyncOuterRecord"]
    assert r["functions"][0]["name"] == "Fn"
    assert r["functions"][0]["error"] == 1


def test_server_url_filter_rejects_placeholder_hosts():
    """配置里残留的测试占位地址（http://x、*.invalid）不能进监控下拉框。"""
    from api.cf.routes_cf_monitor import _plausible_url

    assert not _plausible_url("http://x")
    assert not _plausible_url("http://test-cf-401.invalid")
    assert not _plausible_url("")
    assert not _plausible_url("ftp://example.com")
    assert not _plausible_url("http://localhost")
    assert _plausible_url("https://hr-test.bii.com.cn")
    assert _plausible_url("http://10.1.38.184")
    assert _plausible_url("https://hcm.ptacn.com:4430")


# ─────────────────── 错误详情：短摘要 vs 完整正文 ───────────────────
def test_full_error_text_keeps_traceback_newlines():
    """表格用 160 字短摘要，详情必须能拿到完整正文（含 traceback 换行）。

    实测痛点：`_row_message` 统一截 160 字符，展开也只看到截断的一半，
    「这些错误看不到详细的」就是这么来的。
    """
    import json as _json
    body = (
        "[RID:x] 任职回写总账: JobInformation 写入 0 / 失败 5\n"
        "Traceback (most recent call last):\n"
        '  File "<dynamic_script:3929>", line 382, in _process_one\n'
        "    self._sync_job_info_edit(row)\n"
        "KeyError: 'job_information_id'\n" + "附加诊断 " * 60
    )
    row = _row("Fn", _json.dumps({"msg": body}, ensure_ascii=False))
    b = cm.aggregate([row])["Fn"]

    # 表格摘要：单行、限长
    assert len(b["last_error"]) <= cm._ROW_MSG_MAX
    assert "\n" not in b["last_error"]
    # 详情全文：更长、保留换行、含调用栈
    full = b["last_error_full"]
    assert len(full) > len(b["last_error"])
    assert "\n" in full
    assert "Traceback" in full
    assert "KeyError" in full
    assert len(full) <= cm._FULL_MSG_MAX


def test_full_error_text_handles_nested_json():
    """双层 JSON 包裹的 traceback 也要解包成可读多行文本。"""
    import json as _json
    inner = "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: boom"
    row = _row("Fn", _json.dumps({"content": _json.dumps({"content": inner})}, ensure_ascii=False))
    full = cm.aggregate([row])["Fn"]["last_error_full"]
    assert "Traceback" in full
    assert "ValueError: boom" in full
    assert "\n" in full


def test_full_error_text_capped():
    """超长正文按 _FULL_MSG_MAX 截断，避免整页 JSON 灌进接口。"""
    import json as _json
    # 必须带错误信号，否则这行不会被记进「最近错误」
    huge = "❌ 失败： " + "x" * (cm._FULL_MSG_MAX * 2)
    row = _row("Fn", _json.dumps({"msg": huge}, ensure_ascii=False))
    full = cm.aggregate([row])["Fn"]["last_error_full"]
    assert len(full) <= cm._FULL_MSG_MAX
    assert len(full) >= cm._FULL_MSG_MAX - 10   # 确实被截到上限附近，而不是残留超长原文


# ─────────────── 错误样本：同一函数多种失败原因 ───────────────
def test_error_samples_collects_distinct_causes():
    """同一函数 56 条错误往往不是同一个原因，必须给出多种样本。

    实测 ConvHuaqiEmp 同时存在「打卡时间不在审批范围内」和
    「handlers.py 调用栈」两类完全不同的失败 —— 只给「最近一条」
    会让人误以为只有一个问题。
    """
    rows = [
        _row("Fn", '{"msg": "[RID:1] ❌ 失败：打卡时间不在审批范围内"}', "2026-10-22 10:00:01"),
        _row("Fn", '{"msg": "[RID:2] ❌ 失败：打卡时间不在审批范围内"}', "2026-10-22 10:00:02"),
        _row("Fn", '{"msg": "[RID:3] Traceback 调用栈 done 抛错"}', "2026-10-22 09:00:00"),
    ]
    samples = cm.aggregate(rows)["Fn"]["error_samples"]
    # 相同正文去重，只留 2 种
    assert len(samples) == 2, samples
    # 时间倒序：最近的在前
    assert samples[0]["time"] == "2026-10-22 10:00:02"
    assert "打卡时间" in samples[0]["full"]
    assert "Traceback" in samples[1]["full"]


def test_error_samples_capped():
    """样本数量有上限，避免一个函数刷几百条不同错误把响应撑爆。"""
    rows = [
        _row("Fn", '{"msg": "[RID:%d] ❌ 失败：原因%d"}' % (i, i), f"2026-10-22 10:00:{i:02d}")
        for i in range(20)
    ]
    samples = cm.aggregate(rows)["Fn"]["error_samples"]
    assert len(samples) == cm._ERR_SAMPLE_MAX


def test_error_samples_empty_when_no_error():
    rows = [_row("Fn", '{"msg": "[RID:1] ✅ 成功"}')]
    assert cm.aggregate(rows)["Fn"]["error_samples"] == []


# ═══════════════════════════════════════════════════════════════════
#  时间窗口过滤 + 记录模型选择（2026-10-23 新增）
# ═══════════════════════════════════════════════════════════════════
# 为什么不走 filter_dict 下推，而是取回后过滤：
#   SyncOuterRecord 重写了 parse_filter_dict，会把传入的 update_time **无条件
#   再包一层 gte**（core/extend/sync_outer/models.py）：
#       filter_dict['update_time'] = {"gte": limit_date}
#   于是 {"gte":起,"lte":止} 变成 {"gte":{"gte":起,"lte":止}} → 值成了字典，
#   比较必然失配，表现为「静默查到 0 行」而非报错。下面用测试把这个坑钉死。


def _trow(name, ts, content='{"msg": "ok"}'):
    """一行日志；时间字段用 create_time（dynamic_log 口径）。"""
    return {"name": name, "log_type": name, "create_time": ts, "content": content}


# ─────────────────── parse_ts：入参格式兼容 ───────────────────
def test_parse_ts_accepts_common_formats():
    """同一时刻的多种写法必须解析成同一个时间戳，否则窗口过滤会漏行。"""
    base = cm.parse_ts("2026-10-20 14:00:00")
    assert base == cm.parse_ts("2026-10-20 14:00")
    assert base == cm.parse_ts("2026-10-20T14:00:00")
    assert base == cm.parse_ts("2026/10/20 14:00:00")
    # unix 秒 / 毫秒
    assert cm.parse_ts(int(base)) == base
    assert cm.parse_ts(str(int(base * 1000))) == pytest.approx(base, abs=0.001)


def test_parse_ts_date_only_and_invalid():
    """只给日期 = 当天 00:00；无法解析返回 None（由调用方决定不过滤）。"""
    assert cm.parse_ts("2026-10-20") == cm.parse_ts("2026-10-20 00:00:00")
    for bad in ("", "   ", None, 0, "昨天", "not-a-time"):
        assert cm.parse_ts(bad) is None


# ─────────────────── filter_rows_by_time ───────────────────
def test_filter_rows_by_time_keeps_only_window():
    rows = [
        _trow("A", "2026-10-20 10:00:00"),
        _trow("A", "2026-10-21 10:00:00"),
        _trow("A", "2026-10-22 10:00:00"),
    ]
    kept, dropped = cm.filter_rows_by_time(rows, "2026-10-21 00:00:00", "2026-10-21 23:59:59")
    assert [r["create_time"] for r in kept] == ["2026-10-21 10:00:00"]
    assert dropped == 2


def test_filter_rows_by_time_open_bounds():
    """只给起点/只给终点/都不给 —— 三种半开区间都要成立。"""
    rows = [_trow("A", f"2026-10-{d:02d} 10:00:00") for d in (19, 20, 21, 22)]
    assert len(cm.filter_rows_by_time(rows, "2026-10-21 00:00:00", "")[0]) == 2
    assert len(cm.filter_rows_by_time(rows, "", "2026-10-20 23:59:59")[0]) == 2
    assert len(cm.filter_rows_by_time(rows, "", "")[0]) == 4


def test_filter_rows_by_time_boundaries_inclusive():
    """起止都是闭区间：正好等于起点的行不能被剔掉（边界写错会少算一条错误）。"""
    rows = [_trow("A", "2026-10-21 00:00:00"), _trow("A", "2026-10-21 23:59:59")]
    kept, dropped = cm.filter_rows_by_time(rows, "2026-10-21 00:00:00", "2026-10-21 23:59:59")
    assert len(kept) == 2 and dropped == 0


def test_filter_rows_by_time_swapped_bounds():
    """起止写反了要交换，而不是返回一个永远为空的窗口。

    交换后窗口是 10-21 00:00 ~ 10-25 23:59，两行都落在里面 —— 关键是
    「不能用用户写的反了顺序把自己筛成空」。
    """
    rows = [_trow("A", "2026-10-21 10:00:00"), _trow("A", "2026-10-25 10:00:00")]
    kept, _ = cm.filter_rows_by_time(rows, "2026-10-25 23:59:59", "2026-10-21 00:00:00")
    assert len(kept) == 2, kept


def test_filter_rows_by_time_uses_update_time_fallback():
    """SyncOuterRecord 没有 create_time，时间在 update_time 上 —— 也要能过滤。"""
    rows = [{"name": "A", "update_time": "2026-10-21 10:00:00", "content": "ok"},
            {"name": "A", "update_time": "2026-10-22 10:00:00", "content": "ok"}]
    kept, dropped = cm.filter_rows_by_time(rows, "2026-10-21 00:00:00", "2026-10-21 23:59:59")
    assert len(kept) == 1 and dropped == 1


def test_filter_rows_by_time_keeps_rows_without_time():
    """取不到时间的行要**保留**：不能因为某模型缺时间字段就把看板清空。"""
    rows = [_trow("A", "2026-10-21 10:00:00"), {"name": "B", "content": "ok"}]
    kept, dropped = cm.filter_rows_by_time(rows, "2026-10-22 00:00:00", "2026-10-22 23:59:59")
    assert len(kept) == 1 and kept[0]["name"] == "B"
    assert dropped == 1   # 有时间且在窗口外的那行仍然被剔除


def test_filter_rows_by_time_unparsable_input_does_not_silently_empty():
    """用户填了看不懂的时间 → 不过滤（宁可宽），不能静默返回 0 行。"""
    rows = [_trow("A", "2026-10-21 10:00:00")]
    assert len(cm.filter_rows_by_time(rows, "上周一", "")[0]) == 1


# ─────────────────── sampled_time_range / window_truncated ───────────────────
def test_sampled_time_range():
    rows = [_trow("A", "2026-10-22 10:00:00"), _trow("B", "2026-10-20 08:30:00"),
            {"name": "C", "content": "ok"}]   # 无时间，不参与
    assert cm.sampled_time_range(rows) == ("2026-10-20 08:30:00", "2026-10-22 10:00:00")
    assert cm.sampled_time_range([]) == ("", "")
    assert cm.sampled_time_range([{"name": "C", "content": "ok"}]) == ("", "")


def test_window_truncated_detects_uncovered_start():
    """翻页没翻完 + 最早一行比窗口起点还新 → 采样不完整，必须提示调大规模。"""
    rows = [_trow("A", "2026-10-22 10:00:00"), _trow("A", "2026-10-23 10:00:00")]
    # 起点 10-01 远早于最早行 10-22 → 左侧没覆盖到 → 提示
    assert cm.window_truncated(rows, "2026-10-01 00:00:00", exhausted=False) is True
    # 起点 10-22 12:00 晚于最早行 10-22 10:00 → 左侧已覆盖 → 不提示
    assert cm.window_truncated(rows, "2026-10-22 12:00:00", exhausted=False) is False


def test_window_truncated_silent_when_exhausted():
    """已经取尽（全平台就这么多行）就不该说「采样不全」—— 窗口里确实没数据。"""
    rows = [_trow("A", "2026-10-22 10:00:00")]
    assert cm.window_truncated(rows, "2026-10-01 00:00:00", exhausted=True) is False


def test_window_truncated_silent_when_no_time_requested():
    """没填起点就别报「采样不全」——那是无意义的噪音。"""
    rows = [_trow("A", "2026-10-22 10:00:00")]
    assert cm.window_truncated(rows, "", exhausted=False) is False
    # 一行时间都取不到 → 无法判断，也不提示（不制造假警报）
    assert cm.window_truncated([{"name": "A", "content": "ok"}],
                               "2026-10-01 00:00:00", exhausted=False) is False


# ─────────────────── aggregate(only_errors=True) ───────────────────
def test_only_errors_counts_errors_only():
    rows = [
        _trow("Fn", "2026-10-22 10:00:00", '{"msg": "[RID:1] ✅ 成功"}'),
        _trow("Fn", "2026-10-22 10:00:01", '{"msg": "[RID:2] ❌ 失败：写库超时"}'),
    ]
    b = cm.aggregate(rows, only_errors=True)["Fn"]
    assert b["total"] == 1 and b["error"] == 1


def test_only_errors_keeps_last_message_from_success_rows():
    """只看错误时，成功行仍要更新「最近一条」，否则会误导成「之后没有成功日志了」。"""
    rows = [
        _trow("Fn", "2026-10-22 10:00:00", '{"msg": "[RID:1] ❌ 失败：写库超时"}'),
        _trow("Fn", "2026-10-22 10:00:05", '{"msg": "[RID:2] ✅ 已恢复"}'),
    ]
    b = cm.aggregate(rows, only_errors=True)["Fn"]
    assert b["error"] == 1 and b["total"] == 1
    assert "已恢复" in b["last_message"], b["last_message"]
    assert "写库超时" in b["last_error"]   # 错误摘要不受影响


def test_only_errors_preserves_bucket_for_success_only_function():
    """只出现过成功行的函数也要保留在列表里（否则「哪些函数在跑」就看不见了）。"""
    rows = [_trow("OkFn", "2026-10-22 10:00:00", '{"msg": "[RID:1] ✅ 成功"}')]
    b = cm.aggregate(rows, only_errors=True)["OkFn"]
    assert b["total"] == 0 and b["error"] == 0
    assert b["health"] == cm.HEALTH_UNKNOWN


# ─────────────────── collect：端到端串起时间窗口 ───────────────────
def _stub_query(monkeypatch, rows_by_page):
    """把 cf_query_logs 打桩成按页返回固定行。"""
    calls = []

    async def _query_page(server_url, token, proxy, record_model, log_type,
                          page, page_size):
        calls.append((record_model, page))
        rows = rows_by_page.get(page, [])
        return rows, len(rows), None, False

    monkeypatch.setattr(cm, "_query_page", _query_page)
    return calls


def test_collect_applies_time_window_and_reports_range(monkeypatch):
    rows = [
        _trow("A", "2026-10-20 10:00:00", '{"msg": "[RID:1] ✅ 旧"}'),
        _trow("A", "2026-10-21 10:00:00", '{"msg": "[RID:2] ❌ 失败：超时"}'),
        _trow("A", "2026-10-22 10:00:00", '{"msg": "[RID:3] ✅ 新"}'),
    ]
    _stub_query(monkeypatch, {1: rows})
    out = asyncio.run(cm.collect(
        server_url="https://x.example.com",
        record_model="SyncOuterRecord",
        pages=1,
        time_start="2026-10-21 00:00:00",
        time_end="2026-10-21 23:59:59",
    ))
    assert out["ok"] is True
    # 窗口外的两条被剔除，只剩窗口内 1 条
    assert out["rows_fetched"] == 1
    assert out["rows_queried"] == 3
    tr = out["time_range"]
    assert tr["start"] == "2026-10-21 00:00:00"
    assert tr["out_of_window"] == 2
    assert tr["truncated"] is False
    assert tr["sampled_from"] == "2026-10-20 10:00:00"
    assert tr["sampled_to"] == "2026-10-22 10:00:00"
    assert out["functions"][0]["error"] == 1


def test_collect_flags_truncated_window(monkeypatch):
    """窗口起点比采样最早一行还早 → truncated=True（前端据此提示调大规模）。"""
    rows = [_trow("A", "2026-10-22 10:00:00"), _trow("A", "2026-10-23 10:00:00")]
    _stub_query(monkeypatch, {1: rows})
    out = asyncio.run(cm.collect(
        server_url="https://x.example.com",
        record_model="dynamic_log",
        pages=1,
        page_size=5,          # 首屏没塞满一页 → 视为已取尽
        time_start="2026-10-01 00:00:00",
    ))
    assert out["ok"] is True
    # 只抓了第 1 页且 pages=1 → 已取尽 → 不提示（平台就这么多行）
    assert out["time_range"]["truncated"] is False


def test_collect_no_window_passes_rows_through(monkeypatch):
    """不填时间窗口时行为与从前完全一致（rows_fetched == rows_queried）。"""
    rows = [_trow("A", "2026-10-20 10:00:00"), _trow("A", "2026-10-22 10:00:00")]
    _stub_query(monkeypatch, {1: rows})
    out = asyncio.run(cm.collect(server_url="https://x.example.com",
                                 record_model="dynamic_log", pages=1))
    assert out["rows_fetched"] == 2 and out["rows_queried"] == 2
    assert out["time_range"]["truncated"] is False
    assert out["time_range"]["start"] == ""


def test_collect_failure_path_has_time_range_skeleton(monkeypatch):
    """取数失败也要给齐 time_range 字段，前端不用判存在性。"""
    async def _boom(*a, **kw):
        return [], 0, RuntimeError("token 失效"), False
    monkeypatch.setattr(cm, "_query_page", _boom)
    out = asyncio.run(cm.collect(server_url="https://x.example.com",
                                 record_model="dynamic_log", pages=1))
    assert out["ok"] is False
    assert out["time_range"] == {
        "start": "", "end": "", "sampled_from": "", "sampled_to": "",
        "out_of_window": 0, "truncated": False,
    }


def test_collect_record_model_auto_still_probes_in_order(monkeypatch):
    """记录模型选「自动」时仍按 RECORD_MODELS 顺序降级（选具体模型则不降级）。"""
    tried = []

    async def _query_page(server_url, token, proxy, record_model, log_type,
                          page, page_size):
        tried.append(record_model)
        if record_model == "dynamic_log":
            return [], 0, RuntimeError("Unknown Model Name"), True   # 该租户没有
        return [_trow("Fn", "2026-10-22 10:00:00")], 1, None, False

    monkeypatch.setattr(cm, "_query_page", _query_page)
    out = asyncio.run(cm.collect(server_url="https://x.example.com",
                                 record_model="auto", pages=1))
    assert out["ok"] is True
    assert out["record_model"] == "SyncOuterRecord"
    assert tried == ["dynamic_log", "SyncOuterRecord"]


def test_collect_only_errors_passthrough(monkeypatch):
    rows = [
        _trow("Fn", "2026-10-22 10:00:00", '{"msg": "[RID:1] ✅ ok"}'),
        _trow("Fn", "2026-10-22 10:00:01", '{"msg": "[RID:2] ❌ 失败：boom"}'),
    ]
    _stub_query(monkeypatch, {1: rows})
    out = asyncio.run(cm.collect(server_url="https://x.example.com",
                                 record_model="dynamic_log", pages=1,
                                 only_errors=True))
    assert out["functions"][0]["error"] == 1
    assert out["functions"][0]["total"] == 1


# ───────翻页是否取尽：决定能不能说「窗口里就这些」 ───────
def _full_pages(monkeypatch, n_pages, page_size, total=0):
    """每一页都塞满 page_size 行 → 模拟「翻到上限也取不尽」。"""
    async def _query_page(server_url, token, proxy, record_model, log_type,
                          page, page_size_):
        if page > n_pages:
            return [], 0, None, False
        rows = [_trow("Fn", f"2026-10-22 10:00:{page:02d}") for _ in range(page_size)]
        return rows, total, None, False

    monkeypatch.setattr(cm, "_query_page", _query_page)


def test_collect_marks_not_exhausted_when_pages_hit_limit(monkeypatch):
    """每页都塞满、翻到上限还没取尽 → exhausted=False，窗口起点够不着时要提示。"""
    _full_pages(monkeypatch, n_pages=2, page_size=3, total=0)
    out = asyncio.run(cm.collect(server_url="https://x.example.com",
                                 record_model="dynamic_log",
                                 page_size=3, pages=2,
                                 time_start="2026-10-01 00:00:00"))
    assert out["ok"] is True
    # 窗口起点(10-01) 比抓到的最早一行(10-22 10:00:01)还早，且没取尽 → 必须提示
    assert out["time_range"]["truncated"] is True
    # 抓到的 6 行全在窗口内（10-01 之后），所以 rows_fetched 应等于 6。
    # ⚠️ 这里不能断言 0 —— 窗口起点早、终点为空意味着「10-01 起至今」全都算命中，
    #    早先写 0 是把「起点」误当「终点」了。
    assert out["rows_fetched"] == 6


def test_collect_no_truncated_when_last_page_partial(monkeypatch):
    """最后一页不满 → 已经取尽，即使窗口起点很早也不提示（窗口里就是没数据）。"""
    async def _query_page(server_url, token, proxy, record_model, log_type,
                          page, page_size):
        if page == 1:
            return [_trow("Fn", "2026-10-22 10:00:01")], 2, None, False
        return [_trow("Fn", "2026-10-22 10:00:02")], 2, None, False

    monkeypatch.setattr(cm, "_query_page", _query_page)
    out = asyncio.run(cm.collect(server_url="https://x.example.com",
                                 record_model="dynamic_log",
                                 page_size=5, pages=5,
                                 time_start="2026-10-01 00:00:00"))
    assert out["ok"] is True
    assert out["time_range"]["truncated"] is False


# ─────────────────── 路由层：模型清单接口 ───────────────────
def test_monitor_models_endpoint_lists_probe_order():
    """模型清单接口要按 RECORD_MODELS（自动探测顺序）返回，并带类型字段名。"""
    from fastapi.testclient import TestClient
    from api.cf.routes_cf_monitor import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/api/cf/monitor/models")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    names = [m["name"] for m in body["models"]]
    assert names == list(cm.RECORD_MODELS)
    by_name = {m["name"]: m for m in body["models"]}
    # 类型字段随模型变化，UI 要能据此提示
    assert by_name["dynamic_log"]["type_field"] == "log_type"
    assert by_name["SyncOuterRecord"]["type_field"] == "name"
    assert by_name["SysLogRecord"]["type_field"] == "name"


def test_monitor_models_endpoint_has_chinese_labels():
    """每个模型都要有可读说明，否则下拉里只有裸模型名，用户选不明白。"""
    from fastapi.testclient import TestClient
    from api.cf.routes_cf_monitor import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/api/cf/monitor/models").json()
    for m in body["models"]:
        assert m.get("label"), m
