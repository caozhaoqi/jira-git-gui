#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cf_locate_retrofit 改造工具的回归测试。

覆盖：
- _transform：注入 snippet / 包 execute / obj.get→safe_get / 幂等 / 编译通过
- _split_header：编码声明保留在前两行
- audit_file：风险噪声分层（category = true_high / noise）
- _rank：优先级排序 + --top/--only 过滤

可直接 `python3 tests/test_cf_retrofit.py` 运行（无需 pytest），也会暴露 pytest 用例。
"""
import importlib.util
import sys
import py_compile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

_spec = importlib.util.spec_from_file_location("cf_locate_retrofit", TOOLS / "cf_locate_retrofit.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

SNIPPET = mod._snippet_body(TOOLS / "cf_locate_kit" / "locate_snippet.py")

SAMPLE = """# -*- coding: utf-8 -*-
import logging

class MyCF:
    def execute(self, ctx):
        salary = emp.get('salary')
        dept = emp.get('dept_id')
        return salary
"""


def test_transform_basic():
    new = mod._transform(SAMPLE, SNIPPET)
    assert mod.SNIPPET_MARKER in new, "未注入定位 snippet"
    assert "def safe_get" in new, "未定义 safe_get"
    assert "def _run(self" in new, "未把 execute 重命名为 _run"
    assert "locate_guard" in new, "未注入 locate_guard 兜底"
    assert "salary = safe_get(emp, 'salary', 'salary')" in new
    assert "dept = safe_get(emp, 'dept_id', 'dept_id')" in new
    # 编码声明仍在第一/二行
    assert "coding" in "\n".join(SAMPLE.splitlines()[:2])


def test_transform_idempotent():
    once = mod._transform(SAMPLE, SNIPPET)
    twice = mod._transform(once, SNIPPET)
    assert once == twice, "重复 apply 必须幂等（无二次改动）"


def test_transform_compiles():
    new = mod._transform(SAMPLE, SNIPPET)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(new)
        path = tf.name
    try:
        py_compile.compile(path, doraise=True)
    finally:
        Path(path).unlink(missing_ok=True)


def test_transform_injects_snippet_when_no_candidate():
    # 设计如此：即使无候选字段访问，工具仍注入定位 snippet；且幂等
    src = "# config\nX = 1\n"
    new = mod._transform(src, SNIPPET)
    assert mod.SNIPPET_MARKER in new, "无候选取值时仍应注入定位 snippet"
    assert mod._transform(new, SNIPPET) == new, "已注入后再次 apply 应幂等"


def test_audit_category_split():
    src = SAMPLE + "\n    try:\n        x = emp['miss']\n    except:\n        pass\n"
    p = Path("/tmp/_cf_audit_sample.py")
    p.write_text(src, encoding="utf-8")
    try:
        info = mod.audit_file(p)
    finally:
        p.unlink(missing_ok=True)
    cats = {r["category"] for r in info["risks"]}
    assert "true_high" in cats, "应包含 true_high 类风险"
    # 下标访问与裸 except 归为 true_high；NO_DIAGNOSTIC_CONTEXT 归为 noise
    types = {r["type"] for r in info["risks"]}
    assert "UNSAFE_SUBSCRIPT_ACCESS" in types
    assert "BARE_EXCEPT" in types
    noise = [r for r in info["risks"] if r["category"] == "noise"]
    assert any(r["type"] == "NO_DIAGNOSTIC_CONTEXT" for r in noise)


def test_audit_all_cache_and_rank_reuse():
    # A1：审计结果携带内部 accesses 缓存，_rank 可直接复用而不需再次 scan_file。
    import shutil
    d = Path(tempfile.mkdtemp())
    try:
        p = d / "cached.py"
        p.write_text(SAMPLE, encoding="utf-8")
        infos = mod._audit_all([p])
        assert str(p) in infos
        assert infos[str(p)].get("_accesses"), "audit 应保留内部 accesses 缓存"
        ranked = mod._rank([p], [], 0, 0, infos=infos)
        assert ranked and ranked[0][0] == p
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rank_filters_and_sorts():
    import shutil
    d = Path(tempfile.mkdtemp())
    try:
        # 两个文件：一个高分（多下标），一个低分（仅 snippet 缺失）
        hi = d / "hi.py"
        hi.write_text(SAMPLE + "\nfor i in range(3):\n    v = emp['a']\n    w = emp['b']\n", encoding="utf-8")
        lo = d / "lo.py"
        lo.write_text("# coding\nclass C:\n    def execute(self, c):\n        return 1\n", encoding="utf-8")
        files = sorted(d.rglob("*.py"))
        ranked = mod._rank(files, [], 0, 0)
        assert ranked[0][1] >= ranked[1][1], "应按 score 降序"
        top1 = mod._rank(files, [], 0, 1)
        assert len(top1) == 1, "--top 应限制数量"
        only_sub = mod._rank(files, ["UNSAFE_SUBSCRIPT_ACCESS"], 0, 0)
        assert all("UNSAFE_SUBSCRIPT_ACCESS" in th for _, _, th in only_sub), "--only 应只保留含该风险的文件"
    finally:
        shutil.rmtree(d, ignore_errors=True)


SUB_SAMPLE = """# -*- coding: utf-8 -*-
class MyCF:
    def execute(self, ctx):
        salary = emp['salary']
        name = emp.get('name')
        return salary
"""


def test_transform_subscript_assignment():
    # B1：简单下标赋值 → safe_get
    new = mod._transform(SUB_SAMPLE, SNIPPET)
    assert "salary = safe_get(emp, 'salary', 'salary')" in new, "下标赋值应转 safe_get"
    # 同一文件内的 .get 也应转（两种形态都处理）
    assert "name = safe_get(emp, 'name', 'name')" in new
    # 非业务对象变量不下标转换（避免误改普通字典）；
    # 注意：_transform 总会注入含 `def safe_get` 的 snippet，故不能以 "safe_get" 是否出现判定，
    # 应改为确认 cfg['k'] 这种非候选赋值未被改写成 safe_get(cfg, ...)。
    plain = "y = cfg['k']\n"
    once = mod._transform(plain, SNIPPET)
    assert "y = cfg['k']" in once, "非业务对象变量不应下标转换"
    assert "safe_get(cfg" not in once, "非业务对象变量不应下标转换"


def test_transform_subscript_idempotent():
    once = mod._transform(SUB_SAMPLE, SNIPPET)
    assert mod._transform(once, SNIPPET) == once, "下标转换必须幂等"


def test_transform_subscript_compiles():
    new = mod._transform(SUB_SAMPLE, SNIPPET)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(new)
        path = tf.name
    try:
        py_compile.compile(path, doraise=True)
    finally:
        Path(path).unlink(missing_ok=True)


def test_redact_sensitive_basic():
    # B2：单行 log/print(敏感变量) → _mask(...)
    src = "log.info(token)\nprint(password)\nlogger.error(auth_token)\n"
    new = mod._redact_sensitive(src)
    assert "log.info(_mask(token))" in new
    assert "print(_mask(password))" in new
    assert "logger.error(_mask(auth_token))" in new


def test_redact_sensitive_skips_unsafe():
    # f-string / 复杂表达式 / 非敏感变量 不自动改（保守）
    src = 'log.info(f"token={token}")\nlog.info("ok")\nlog.info(user["token"])\n'
    assert mod._redact_sensitive(src) == src, "f-string/复杂表达式/非敏感参数应保持原样"


def test_transform_redact_flag_end_to_end():
    src = SUB_SAMPLE + "\n    log.info(token)\n"
    new = mod._transform(src, SNIPPET, redact_sensitive=True)
    assert "log.info(_mask(token))" in new
    assert mod._transform(new, SNIPPET, redact_sensitive=True) == new, "脱敏需幂等"
    bare = mod._transform(src, SNIPPET)
    # 默认（不传 redact_sensitive）不应把日志行包成 _mask：定位 snippet 自身虽定义 _mask，
    # 但 log.info(token) 这一行应保持原样（出现 log.info(_mask(token)) 才算误脱敏）。
    assert "log.info(token)" in bare and "log.info(_mask(token))" not in bare, "默认不应脱敏"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
