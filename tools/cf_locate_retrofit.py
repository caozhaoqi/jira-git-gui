#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HCM 云函数「错误定位」改造工具（开发期 CLI，不改框架源码）。

安全原则
--------
- 默认是**只读**扫描/预览，绝不自动改文件；只有显式 --apply 才写盘，且原文件备份为 .bak。
- 改动只作用于「你自己的云函数 .py」，不碰 hcm-core 框架任何文件。

子命令
------
  scan    扫描字段访问，输出「云函数 → 对象变量 → 字段」清单；
          给 --meta 时与模型元数据字段做对照，标出元数据里不存在/疑似已删除的字段。
  diff    生成「带定位版本」的统一 diff（不写盘），供人工评审。
  apply   应用改动（写盘 + .bak 备份）：把简单取值（obj.get / obj['field']）换成 safe_get，
         给 execute 套兜底 try；加 --redact-sensitive 还可把单行 log/print(敏感变量) 包成 _mask(...)。

用法示例
--------
  # 扫描 cloud_functions 与备份目录
  python3 tools/cf_locate_retrofit.py scan --dir hcm-core/cloud_functions --dir config/cf_backup

  # 与元数据字段对照（meta.json 为 hcm.model.meta 返回的 fields 数组或含 fields 的对象）
  python3 tools/cf_locate_retrofit.py scan --dir hcm-core/cloud_functions --meta meta.json

  # 预览改造 diff
  python3 tools/cf_locate_retrofit.py diff --dir hcm-core/cloud_functions

  # 应用改造（先备份）
  python3 tools/cf_locate_retrofit.py apply --dir hcm-core/cloud_functions

  # 增量审计：只看当前仓库自 HEAD 以来变化的文件（含未跟踪文件）
  python3 tools/cf_locate_retrofit.py audit --dir tools --since HEAD --json
"""

import argparse
import ast
import difflib
import json
import os
import re
import subprocess
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# 被扫描的云函数源码里常有未加 r 前缀的正则字符串字面量，Python 3.12+ 解析时会抛
# SyntaxWarning（属被扫描文件自身问题，非本工具缺陷），这里静默掉以免污染报告输出。
warnings.filterwarnings('ignore', category=SyntaxWarning)

SNIPPET_MARKER = "# <<< CF_LOCATE_SNIPPET >>>"

# 典型 HCM 业务对象变量名（用于判断某个 .get() 是否大概率是对象字段访问）
OBJ_VAR_HINT = re.compile(
    r'(emp|employee|staff|dept|depart|unit|org|user|record|rec|row|item|obj|data|info|'
    r'member|post|position|salary|payroll|leave|entry|entry_info|sc_record|relation|'
    r'student|teacher|customer|supplier|contract|project|task)', re.I)

# 非字段的 .get() 调用（配置/字典工具取值），避免误判
NON_FIELD_KEYS = {
    'id', 'name', 'key', 'value', 'type', 'code', 'msg', 'message', 'result',
    'data', 'list', 'total', 'count', 'page', 'size', 'status', 'error',
    'errcode', 'errmsg', 'description', 'model', 'params', 'config', 'token',
}

# 真高风险类型：改造工具「不会自动修复」，需人工优先关注（下标访问/敏感日志/无入口/裸 except）
TRUE_HIGH_TYPES = {
    "UNSAFE_SUBSCRIPT_ACCESS", "POSSIBLE_SENSITIVE_LOG",
    "NO_EXECUTE_ENTRYPOINT", "BARE_EXCEPT",
}
TRUE_HIGH_WEIGHT = {
    "UNSAFE_SUBSCRIPT_ACCESS": 6, "POSSIBLE_SENSITIVE_LOG": 8,
    "NO_EXECUTE_ENTRYPOINT": 4, "BARE_EXCEPT": 3,
}
# 噪声型风险：改造工具会自动补齐（注入 snippet/包装 execute）或低信号，不进真高风险评分
NOISE_TYPES = {
    "NO_DIAGNOSTIC_CONTEXT", "UNINSTRUMENTED_FIELD_ACCESS",
    "NO_TRACEBACK_LOGGING", "UNSTRUCTURED_DYNAMIC_LOG",
}
# 小目录直接串行，大目录再启用多进程，避免 spawn 开销超过收益。
_PARALLEL_MIN_FILES = 20


def _priority_score(audit_info: dict, accesses) -> int:
    """优先级分：真高风险项权重之和 + 候选字段访问面（改造收益近似）。"""
    score = sum(TRUE_HIGH_WEIGHT.get(r["type"], 0)
                for r in audit_info.get("risks", []) if r["type"] in TRUE_HIGH_TYPES)
    cand = [a for a in accesses if _is_candidate(a[1], a[2])]
    return score + len(cand)


def _true_high_types(audit_info: dict) -> set:
    return {r["type"] for r in audit_info.get("risks", []) if r["type"] in TRUE_HIGH_TYPES}


def _rank(files, only_types, min_score, top, infos=None):
    """对文件按优先级排序并过滤，返回 [(path, score, true_high_set), ...]（排除解析失败）。

    用于 diff/apply 的 --top/--only/--min-score 与 audit 的 --queue，避免生成全量 blanket patch。
    ``infos`` 可复用 audit 阶段结果，避免同一批文件再次读取/解析。
    """
    ranked = []
    infos = infos if infos is not None else _audit_all(files)
    for f in files:
        a = infos.get(str(f)) or audit_file(f)
        if a.get('parse_error') or a.get('read_error'):
            continue
        # audit_file 已解析过一次，直接复用它的 accesses，避免二次 AST parse
        accesses = a.get('_accesses') or []
        ranked.append((f, _priority_score(a, accesses), _true_high_types(a)))
    if only_types:
        wanted = set(only_types)
        ranked = [r for r in ranked if r[2] & wanted]
    if min_score:
        ranked = [r for r in ranked if r[1] >= min_score]
    ranked.sort(key=lambda r: r[1], reverse=True)
    if top:
        ranked = ranked[:top]
    return ranked




def _audit_all(files):
    """批量审计一批文件，返回 {str(path): info}。

    A3 性能优化：文件数 >= _PARALLEL_MIN_FILES 时用 ProcessPoolExecutor 并行（AST 解析是
    CPU-bound）；否则串行，避免小目录上多进程 spawn 开销反超。任何异常都回退串行，保证行为一致。
    """
    file_list = list(files)
    if len(file_list) < _PARALLEL_MIN_FILES:
        return {str(f): audit_file(f) for f in file_list}
    try:
        with ProcessPoolExecutor() as ex:
            infos = list(ex.map(audit_file, file_list))
        return {str(f): info for f, info in zip(file_list, infos)}
    except Exception as e:  # 并行不可用时（沙箱/权限/pickle）回退
        print(f"[警告] 并行审计不可用，回退串行: {e}", file=sys.stderr)
        return {str(f): audit_file(f) for f in file_list}


def _filter_changed_since(files, rev):
    """A4 增量：只保留自 git 版本 rev 以来改动过的 .py（git 不可用时回退全量）。

    注意：git 在「当前工作目录」下执行，因此 --dir 应位于该仓库内；否则可能过滤为空。
    """
    file_list = list(files)
    try:
        out = subprocess.run(['git', 'diff', '--name-only', rev],
                             capture_output=True, text=True, cwd=str(Path.cwd()))
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[警告] --since 执行失败，回退全量: {e}", file=sys.stderr)
        return file_list
    if out.returncode != 0:
        print(f"[警告] git diff 失败，回退全量审计: {out.stderr.strip()}", file=sys.stderr)
        return file_list
    # git diff 不包含未跟踪文件；动态云函数/本工具常是新建文件，需并入增量集合。
    try:
        untracked = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard'],
                                    capture_output=True, text=True, cwd=str(Path.cwd()))
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[警告] git ls-files 执行失败，忽略未跟踪文件: {e}", file=sys.stderr)
        untracked = None
    if untracked is not None and untracked.returncode != 0:
        print(f"[警告] git ls-files 失败，忽略未跟踪文件: {untracked.stderr.strip()}",
              file=sys.stderr)
    cwd = Path.cwd().resolve()
    changed = set()
    untracked_lines = untracked.stdout.splitlines() if untracked is not None and untracked.returncode == 0 else []
    for line in (out.stdout.splitlines() + untracked_lines):
        line = line.strip()
        if not line:
            continue
        try:
            changed.add((cwd / line).resolve())
        except OSError:
            continue
    kept = []
    for f in file_list:
        try:
            if f.resolve() in changed:
                kept.append(f)
        except OSError:
            continue
    print(f"[提示] --since {rev}: 仅 {len(kept)}/{len(file_list)} 个文件有改动",
          file=sys.stderr)
    return kept


def _snippet_body(snippet_path: Path) -> str:
    """读取 locate_snippet.py 中 CF_LOCATE_BEGIN/END 之间的可注入代码。"""
    raw = snippet_path.read_text(encoding='utf-8')
    m = re.search(r'#\s*<<<\s*CF_LOCATE_BEGIN\s*>>>(.*?)#\s*<<<\s*CF_LOCATE_END\s*>>>',
                  raw, re.S)
    if not m:
        raise SystemExit(f"[错误] {snippet_path} 缺少 CF_LOCATE_BEGIN/END 标记")
    return m.group(1).strip('\n') + "\n"


def _split_header(src: str):
    """
    把文件头（shebang + coding 声明 + 模块 docstring）与正文分开。
    注入必须放在头部之后，否则会破坏 PEP263 编码声明（coding 必须在前两行）。
    """
    lines = src.split('\n')
    i = 0
    if lines and lines[0].startswith('#!'):
        i = 1
    # coding 声明必须落在前两行
    for j in range(i, min(i + 2, len(lines))):
        if re.search(r'coding[:=]', lines[j]):
            i = j + 1
            break
    return '\n'.join(lines[:i]), '\n'.join(lines[i:])


def _load_meta_fields(meta_path: Path) -> set:
    """从 hcm.model.meta 的导出里提取字段名集合。支持 list[dict] 或 {fields:[...]}。"""
    data = json.loads(meta_path.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        data = data.get('fields') or data.get('result', {}).get('fields') or []
    fields = set()
    for f in data or []:
        if isinstance(f, dict):
            for k in ('name', 'key', 'field', 'field_name'):
                if f.get(k):
                    fields.add(str(f[k]))
                    break
        elif isinstance(f, str):
            fields.add(f)
    return fields


def _scan_source(src: str):
    """解析已读入的源码，返回 (字段访问清单, 是否有 execute, 类信息)。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return None, False, [], f"语法错误无法解析: {e}"

    accesses = []   # (lineno, obj_var, field, raw_line)

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            # obj.get('field') / obj.get("field")
            if (isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                    and isinstance(node.func.value, ast.Name) and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                accesses.append((node.lineno, node.func.value.id, node.args[0].value, 'get'))
            self.generic_visit(node)

        def visit_Subscript(self, node):
            # obj['field']
            if (isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                accesses.append((node.lineno, node.value.id, node.slice.value, 'subscript'))
            self.generic_visit(node)

    V().visit(tree)

    has_execute = bool(re.search(r'^\s*def\s+execute\s*\(', src, re.M))
    classes = re.findall(r'^\s*class\s+(\w+)\s*\(([^)]*)\)', src, re.M)
    return accesses, has_execute, classes, None


def scan_file(path: Path):
    """读取并解析单个文件，返回 (字段访问清单, 是否有 execute, 类信息)。"""
    src = path.read_text(encoding='utf-8')
    return _scan_source(src)


def _is_candidate(obj_var: str, field: str) -> bool:
    """判断该取值是否大概率是「业务对象字段访问」（用于报告高亮与自动改造）。"""
    if not OBJ_VAR_HINT.search(obj_var):
        return False
    if field.lower() in NON_FIELD_KEYS and obj_var.lower() in ('kwargs', 'params', 'cfg', 'config'):
        return False
    return True


def _transform(src: str, snippet: str, redact_sensitive: bool = False) -> str:
    """
    保守改造：
      1) 顶部注入 locate_snippet（若未注入过）
      2) execute → _run，并插入带 locate_guard 兜底的新 execute
      3) 简单赋值形态 `x = obj.get('field')` → `x = safe_get(obj, 'field', '别名')`
      3b) 简单下标形态 `x = obj['field']` → `x = safe_get(obj, 'field', 'field')`（B1）
      4) 若 redact_sensitive：把「单行 log/print(敏感变量)」包成 `_mask(...)`（B2）
    """
    out = src

    # 1) 注入 snippet：紧接文件头（shebang/coding/docstring）之后，保证编码声明仍在前两行
    if SNIPPET_MARKER not in out:
        header, rest = _split_header(out)
        inject = "\n\n" + SNIPPET_MARKER + "\n" + snippet + SNIPPET_MARKER + "\n"
        out = (header + "\n" if header else "") + inject + rest

    # 2) 简单取值 → safe_get（仅处理 `x = obj.get('field')` 单行形态，保守）
    def _repl(m):
        indent, target, obj, field = m.group(1), m.group(2), m.group(3), m.group(4)
        if not _is_candidate(obj, field):
            return m.group(0)
        return f"{indent}{target} = safe_get({obj}, '{field}', '{field}')"

    out = re.sub(r'^(\s*)(\w+)\s*=\s*(\w+)\.get\(\s*[\'"](\w+)[\'"]\s*\)\s*$',
                 _repl, out, flags=re.M)

    # 3b) 简单下标取值 → safe_get（B1：消 UNSAFE_SUBSCRIPT_ACCESS，缺失即抛带定位的错）
    def _repl_sub(m):
        indent, target, obj, field = m.group(1), m.group(2), m.group(3), m.group(4)
        if not _is_candidate(obj, field):
            return m.group(0)
        return f"{indent}{target} = safe_get({obj}, '{field}', '{field}')"

    out = re.sub(r'^(\s*)(\w+)\s*=\s*(\w+)\[[\'"](\w+)[\'"]\]\s*$',
                 _repl_sub, out, flags=re.M)

    # 3) execute 兜底包装
    if 'def _run(' not in out:
        m = re.search(r'^(\s*)def\s+execute\s*\(\s*self\s*,?\s*(.*?)\)\s*:\s*$', out, re.M)
        if m:
            indent = m.group(1)
            sig = m.group(2).strip()
            out = out[:m.start()] + f"{indent}def _run(self, {sig}):" + out[m.end():]
            wrapper = (
                f"\n{indent}def execute(self, {sig}):\n"
                f"{indent}    self._cur = None\n"
                f"{indent}    self._step = None\n"
                f"{indent}    try:\n"
                f"{indent}        return self._run({_call_args(sig)})\n"
                f"{indent}    except AppException:\n"
                f"{indent}        raise\n"
                f"{indent}    except Exception as _e:\n"
                f"{indent}        raise locate_guard(self, _e)\n"
            )
            # 插到 _run 定义之前
            pos = out.index(f"{indent}def _run(self")
            out = out[:pos] + wrapper.lstrip('\n') + "\n" + out[pos:]
    if redact_sensitive:
        out = _redact_sensitive(out)
    return out


# 敏感字段关键词（日志脱敏启发式）；与 audit 的 POSSIBLE_SENSITIVE_LOG 同源
_SENSITIVE_RE = re.compile(
    r'(?i)(password|passwd|secret|authorization|cookie|token|id_card|idcard|phone)')
# 仅匹配「单行、无嵌套括号」的 log/print/logger.*/_diag 调用（含方法调用态 log.info/log.warning 等）
_LOG_CALL_RE = re.compile(r'^(\s*)((?:log(?:\.\w+)?|print|logger\.\w+|_diag))\s*\(\s*([^()]*?)\s*\)\s*$')
# audit 模式逐行识别「疑似含敏感字段的日志」（与 POSSIBLE_SENSITIVE_LOG 同源）；提到模块级，
# 避免对每个文件重复 re.compile。
_SENSITIVE_LOG_LINE_RE = re.compile(
    r'(?i)(?:log|print|logger\.(?:info|warning|error)|_diag).*?'
    r'(?:password|passwd|secret|authorization|cookie|token|id_card|phone)')


def _redact_sensitive(src: str) -> str:
    """保守脱敏（B2）：仅把「单行 log/print 调用且参数为敏感命名标识符」包成 `_mask(...)`。

    不处理 f-string / 复杂表达式 / 字典字面量（保留原样，由 audit 继续标记人工处理），避免误改。
    幂等：已含 `_mask(` 的行不再处理。
    """
    lines = src.split('\n')
    changed = False
    for i, ln in enumerate(lines):
        if '_mask(' in ln:
            continue
        if not _SENSITIVE_RE.search(ln):
            continue
        m = _LOG_CALL_RE.match(ln)
        if not m:
            continue
        indent, call, arg = m.group(1), m.group(2), m.group(3).strip()
        if not re.fullmatch(r'[\w.]+', arg):
            continue
        if not _SENSITIVE_RE.search(arg):
            continue
        lines[i] = f"{indent}{call}(_mask({arg}))"
        changed = True
    return '\n'.join(lines) if changed else src


def _call_args(sig: str) -> str:
    """根据签名生成 _run 调用参数（**kwargs 原样传）。"""
    s = sig.strip()
    if not s:
        return ''
    if s.startswith('**'):
        return f"{s}"
    return s


def audit_file(path: Path):
    """只读审计单个云函数，返回风险项和可供 AI 消费的能力摘要。

    只读、幂等、可安全并行（ProcessPoolExecutor worker）。读取/解析异常都转成风险项返回，
    不向外抛，便于 _audit_all 批量收集。
    """
    try:
        src = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as e:
        return {'path': str(path), 'read_error': str(e), 'risk_level': 'high', 'risks': []}
    accesses, has_execute, classes, err = _scan_source(src)
    if err:
        return {'path': str(path), 'parse_error': err, 'risk_level': 'high', 'risks': []}

    risks = []
    candidate_accesses = [a for a in accesses if _is_candidate(a[1], a[2])]
    for line, obj, field, kind in candidate_accesses:
        if kind == 'subscript':
            risks.append({
                'type': 'UNSAFE_SUBSCRIPT_ACCESS', 'severity': 'high',
                'line': line, 'object': obj, 'field': field,
                'message': '直接下标取字段，缺失字段可能抛 KeyError',
            })
        elif 'safe_get(' not in src:
            risks.append({
                'type': 'UNINSTRUMENTED_FIELD_ACCESS', 'severity': 'medium',
                'line': line, 'object': obj, 'field': field,
                'message': '对象字段访问没有统一定位上下文',
            })

    if not has_execute:
        risks.append({'type': 'NO_EXECUTE_ENTRYPOINT', 'severity': 'high', 'line': None,
                      'message': '未发现 execute() 入口，需确认是否为可部署云函数'})
    has_diag = bool(re.search(r'\[定位\]|locate_guard|assert_field|safe_get\s*\(|self\._diag\s*\(', src))
    if not has_diag:
        risks.append({'type': 'NO_DIAGNOSTIC_CONTEXT', 'severity': 'medium', 'line': None,
                      'message': '没有发现可供 hide_error_msg 安全携带的定位信息'})
    if 'traceback.format_exc' not in src and 'traceback' not in src:
        risks.append({'type': 'NO_TRACEBACK_LOGGING', 'severity': 'low', 'line': None,
                      'message': '异常日志没有明显 traceback 记录'})
    if re.search(r'^\s*except\s*:\s*$', src, re.M):
        risks.append({'type': 'BARE_EXCEPT', 'severity': 'medium', 'line': None,
                      'message': '存在裸 except，可能吞掉真实异常'})
    sensitive_log = _SENSITIVE_LOG_LINE_RE
    for number, line in enumerate(src.splitlines(), 1):
        if sensitive_log.search(line):
            risks.append({'type': 'POSSIBLE_SENSITIVE_LOG', 'severity': 'high', 'line': number,
                          'message': '日志表达式可能包含 Token/密码/身份证/手机号等敏感字段'})
    open_apis = sorted(set(re.findall(r'(?:call_open_api|call_api)\s*\(\s*[\'\"]([^\'\"]+)', src)))
    models = sorted(set(re.findall(r'[\'\"]model[\'\"]\s*:\s*[\'\"]([^\'\"]+)', src)))
    if 'dynamic_log' in src and not has_diag:
        risks.append({'type': 'UNSTRUCTURED_DYNAMIC_LOG', 'severity': 'low', 'line': None,
                      'message': '写入 dynamic_log 但未发现统一诊断字段'})

    severity_order = {'high': 3, 'medium': 2, 'low': 1}
    risk_level = 'none'
    if risks:
        risk_level = max((r['severity'] for r in risks), key=lambda x: severity_order[x])
    # 噪声分层：标记每条风险属于 true_high（需人工优先）还是 noise（工具会自动补齐/低信号）
    for r in risks:
        r['category'] = 'true_high' if r['type'] in TRUE_HIGH_TYPES else 'noise'
    return {
        'path': str(path),
        'classes': [c[0] for c in classes],
        'has_execute': has_execute,
        'field_access_count': len(candidate_accesses),
        # 内部缓存：供 _rank 复用，audit JSON 输出前会剔除，保持原报告 schema 稳定。
        '_accesses': accesses,
        'open_apis': open_apis,
        'models': models,
        'signals': {
            'has_traceback': 'traceback.format_exc' in src,
            'has_dynamic_log': 'dynamic_log' in src,
            'has_self_log': bool(re.search(r'\bself\.log\s*\(', src)),
            'has_diag': has_diag,
        },
        'risk_level': risk_level,
        'risks': risks,
    }


def main():
    ap = argparse.ArgumentParser(description='HCM 云函数错误定位改造工具')
    ap.add_argument('mode', choices=['scan', 'audit', 'diff', 'apply'])
    ap.add_argument('--dir', action='append', default=[], required=True,
                    help='云函数目录，可重复指定')
    ap.add_argument('--meta', help='模型元数据 JSON（hcm.model.meta 导出），用于字段对照')
    ap.add_argument('--snippet', default=None, help='locate_snippet.py 路径（默认与本脚本同目录）')
    ap.add_argument('--json', action='store_true', help='以 JSON 输出（便于其它工具消费）')
    ap.add_argument('--top', type=int, default=0,
                    help='仅改造/预览优先级最高的前 N 个文件（按真高风险+字段访问面排序）')
    ap.add_argument('--only', action='append', default=[], metavar='RISK',
                    help='仅处理含指定风险类型的文件，可重复，如 --only UNSAFE_SUBSCRIPT_ACCESS')
    ap.add_argument('--min-score', type=int, default=0, help='仅处理优先级分 >= N 的文件')
    ap.add_argument('--queue', action='store_true',
                    help='audit 模式：额外输出按优先级排序的改造队列（score 降序）')
    ap.add_argument('--all', action='store_true',
                    help='audit 模式：显示噪声明细（默认仅突出 true_high，噪声折叠）')
    ap.add_argument('--redact-sensitive', action='store_true',
                    help='diff/apply：把「单行 log/print(敏感变量)」包成 _mask(...)，保守脱敏')
    ap.add_argument('--since', metavar='REV',
                    help='增量模式：仅处理 git REV 之后发生变化的文件；例如 --since HEAD。')
    args = ap.parse_args()

    snippet_path = Path(args.snippet) if args.snippet else \
        Path(__file__).parent / 'cf_locate_kit' / 'locate_snippet.py'
    if args.mode in ('diff', 'apply') and not snippet_path.exists():
        print(f"[错误] 找不到 snippet: {snippet_path}", file=sys.stderr)
        return 2

    meta_fields = _load_meta_fields(Path(args.meta)) if args.meta else None

    files = []
    for d in args.dir:
        p = Path(d)
        if not p.exists():
            print(f"[警告] 目录不存在，跳过: {p}", file=sys.stderr)
            continue
        files += sorted(p.rglob('*.py'))
    if args.since:
        files = _filter_changed_since(files, args.since)
    if not files:
        print("[提示] 未找到任何 .py 文件")
        return 0

    # diff/apply 的优先级过滤集（scan/audit 模式为 None，不限制）
    selected = None
    if args.mode in ('diff', 'apply'):
        ranked = _rank(files, args.only, args.min_score, args.top)
        selected = {f for f, _, _ in ranked}

    if args.mode == 'audit':
        # A1/A3：一次批量读取+解析；大目录自动并行，小目录保持串行。
        report = _audit_all(files)
        if args.json:
            public_report = {
                path: {k: v for k, v in info.items() if k != '_accesses'}
                for path, info in report.items()
            }
            print(json.dumps(public_report, ensure_ascii=False, indent=2))
        else:
            counts = {'high': 0, 'medium': 0, 'low': 0, 'none': 0}
            print('=' * 72)
            print('云函数诊断规范审计报告（true_high 优先，noise 已折叠）')
            print('=' * 72)
            for path, info in report.items():
                level = info.get('risk_level', 'high')
                counts[level] = counts.get(level, 0) + 1
                risks = info.get('risks') or []
                if not risks:
                    continue
                th = [r for r in risks if r['category'] == 'true_high']
                noise = [r for r in risks if r['category'] == 'noise']
                print(f"\\n### {path} [{level}]  true_high={len(th)} noise={len(noise)}")
                for risk in th:
                    line = f"L{risk['line']}" if risk.get('line') else '-'
                    print(f"    [TRUE] [{risk['severity']}] {risk['type']} {line}: {risk['message']}")
                if args.all:
                    for risk in noise:
                        line = f"L{risk['line']}" if risk.get('line') else '-'
                        print(f"    [noise] [{risk['severity']}] {risk['type']} {line}: {risk['message']}")
                elif noise:
                    print(f"    [noise] 另有 {len(noise)} 条噪声型风险（工具会自动补齐/低信号，加 --all 查看）")
            print('\\n' + '=' * 72)
            print('文件风险统计：' + ' / '.join(f'{k}={v}' for k, v in counts.items()))
            if args.queue:
                q = _rank(files, [], 0, args.top, infos=report)
                print('\\n' + '=' * 72)
                print('按优先级排序的改造队列（score 降序）')
                print('=' * 72)
                for i, (f, score, th) in enumerate(q, 1):
                    th_s = ','.join(sorted(th)) or '-'
                    print(f"  {i:<4} score={score:<5} {th_s:<42} {Path(f).name}")
        return 0

    snippet = _snippet_body(snippet_path) if args.mode in ('diff', 'apply') else ''
    report = {}
    changed = 0

    for f in files:
        accesses, has_execute, classes, err = scan_file(f)
        if err:
            print(f"[跳过] {f}: {err}", file=sys.stderr)
            continue
        # 优先级过滤：diff/apply 仅处理 selected 内的文件
        if selected is not None and f not in selected:
            continue

        cand = [a for a in accesses if _is_candidate(a[1], a[2])]
        report[str(f)] = {
            'classes': [c[0] for c in classes],
            'has_execute': has_execute,
            'accesses': [{'line': a[0], 'obj': a[1], 'field': a[2], 'kind': a[3]} for a in cand],
        }

        if args.mode == 'scan':
            continue

        src = f.read_text(encoding='utf-8')
        new = _transform(src, snippet, redact_sensitive=args.redact_sensitive)
        if new == src:
            continue
        changed += 1
        if args.mode == 'diff':
            diff = difflib.unified_diff(
                src.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f'a/{f}', tofile=f'b/{f}', n=3)
            sys.stdout.writelines(diff)
        else:  # apply
            bak = f.with_suffix(f.suffix + '.bak')
            if not bak.exists():
                bak.write_text(src, encoding='utf-8')
            f.write_text(new, encoding='utf-8')
            print(f"[已改造] {f}  (备份 {bak.name})")

    if args.mode == 'scan':
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("=" * 72)
            print("云函数字段访问扫描报告")
            print("=" * 72)
            total_missing = 0
            for path, info in report.items():
                if not info['accesses']:
                    continue
                print(f"\n### {path}")
                print(f"    类: {', '.join(info['classes']) or '-'}"
                      f"   有 execute: {info['has_execute']}")
                for a in info['accesses']:
                    flag = ''
                    if meta_fields is not None and a['field'] not in meta_fields:
                        flag = '   ⚠️ 元数据中不存在（疑似已删除/改名）'
                        total_missing += 1
                    print(f"    L{a['line']:<5} {a['obj']}.{a['field']}"
                          f"  [{a['kind']}]{flag}")
            if meta_fields is not None:
                print("\n" + "=" * 72)
                print(f"元数据对照：共 {total_missing} 处字段在元数据中不存在")
    elif args.mode == 'diff':
        print(f"\n[预览] 共 {changed} 个文件将被改造（未写盘）。确认无误后加 --apply。",
              file=sys.stderr)
    else:
        print(f"\n[完成] 共改造 {changed} 个文件（原文件已备份为 .bak）。"
              f"请回归测试后再部署。")

    return 0


if __name__ == '__main__':
    sys.exit(main())
