# -*- coding: utf-8 -*-
"""CF 云函数错误诊断 —— 聚合「解析 + 词典 + Wiki 路由 + Token 健康度 + 对象反查」。

本模块是 AI 诊断链路的**数据聚合层**：把散落在各处的诊断素材汇总成一个响应，
让 AI（或前端面板）一次调用即可拿到完整上下文，无需逐个文件读。

数据来源（单一事实来源原则）：
- 错误码词典：`store/downloads/895/docs/metadata/reference/errdict.json`
- 路由索引  ：`store/downloads/895/docs/metadata/reference/ERROR_ROUTE_INDEX.md`
- Wiki 正文 ：索引里引用的 .md 文档
- Token 缓存：`api.cf.cf_tokens._CF_TOKEN_CACHE`

设计要点：
1. 路由规则**不硬编码在 Python 里**，而是运行时解析 ERROR_ROUTE_INDEX.md，
   改 md 即改路由，保证索引文件始终是唯一事实来源。
2. Wiki 片段按需抽取（默认 1500 字/篇），避免 80+ 篇文档撑爆 AI context。
3. 所有对外的文件路径都相对项目根，便于 AI 直接用 Read 工具打开。
"""
from __future__ import annotations

import ast
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from api.common import logger, _PROJECT_ROOT
from api.cf.cf_tokens import _CF_TOKEN_CACHE, _cf_token_stale

# --------------------------------------------------------------------------- #
#  路径常量
# --------------------------------------------------------------------------- #

_REF_DIR = _PROJECT_ROOT / "store" / "downloads" / "895" / "docs" / "metadata" / "reference"
_ERRDICT_PATH = _REF_DIR / "errdict.json"
_INFERRED_DICT_PATH = _REF_DIR / "errdict_inferred.json"
_ROUTE_INDEX_PATH = _REF_DIR / "ERROR_ROUTE_INDEX.md"
_SOURCE_INDEX_PATH = _REF_DIR / "cf_source_index.json"
_CASE_DIR = _PROJECT_ROOT / "logs" / "cf_cases"
_FEEDBACK_PATH = _CASE_DIR / "diagnosis_feedback.jsonl"
_REFERENCE_ROOT = Path(os.environ.get(
    "HCM_REFERENCE_ROOT",
    "/Users/caozhaoqi/Downloads/other/hcm-cloud-vue/hcm-core",
)).expanduser()
_REFERENCE_FILES = {
    "error_wrapper": "core/service/handlers.py:476-555",
    "error_definitions": "errors.py:8-66, 143-158, 165-180",
    "token_ttl": "apps/idm/auth_util.py:66-68",
}

# Tornado secure cookie: 2|1:0|10:<unix_ts>|5:token|56:<b64>|<sig>
_TOKEN_TS_RE = re.compile(r'2\|1:0\|10:(\d+)\|5:token\|56:')
_HCM_TOKEN_TTL_HOURS = 2

# --------------------------------------------------------------------------- #
#  ① 错误文本解析
# --------------------------------------------------------------------------- #

_LOC_BLOCK_RE = re.compile(
    r"\[定位\]\s*(?P<body>[^\n]*?)\s*(?:\|\||$)"
)
_LOC_KV_RE = re.compile(r"\b(model|id|field|value|stage)=([^\s]+)")

_ERRCODE_PATTERNS = [
    re.compile(r"errcode[\"']?\s*[:=]\s*\"?(\d{4,6})", re.I),
    re.compile(r"错误码\s*[:：]?\s*(\d{4,6})"),
    re.compile(r"错误号\s*[:：]?\s*(\d{4,6})"),
    re.compile(r"\bcode[\"']?\s*[:=]\s*\"?(\d{4,6})", re.I),
]
_ERROR_CODE_RE = re.compile(r"(?:error_code|错误号)\D*(\d{10,})", re.I)


def _file_address(path: Path, line: Optional[int] = None) -> dict:
    """返回 AI 可直接使用的稳定文件地址。"""
    try:
        rel = str(path.relative_to(_PROJECT_ROOT))
    except ValueError:
        rel = str(path)
    absolute = str(path)
    address = {
        "relative_path": rel,
        "absolute_path": absolute,
        "uri": path.as_uri() if path.is_absolute() else "",
        "line": line,
        "read_hint": f"Read {absolute}" + (f" around line {line}" if line else ""),
    }
    return address


def _mask_value(v, max_len: int = 120) -> "str | None":
    """敏感值脱敏：长串中间打码，与 locate_snippet._mask 思路一致。"""
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if s == "":
        return "(空)"
    if len(s) > 12:
        s = s[:6] + "****" + s[-4:]
    return s[:max_len]


def cf_parse_error(text: str) -> "dict":
    """解析云函数错误文本，提取结构化定位信息。

    兼容三种输入形态：
    1. 带 ``[定位]`` 的完整文本（改造后的云函数抛出）
    2. 只有 error_code（毫秒时间戳，服务端日志索引）
    3. 散落的 ``model=`` / ``field=`` token 或纯 errcode
    """
    text = (text or "").strip()
    out = {
        "model": None, "object_id": None, "field": None,
        "value": None, "value_masked": None, "stage": None,
        "errcode": None,       # 业务错误码（4~6 位，查词典用）
        "error_code": None,    # 毫秒时间戳（10+ 位，服务端日志索引）
        "message": None,
        "log_type": None,
    }
    if not text:
        return out

    # --- [定位] 块：优先取块内的 kv，避免误匹配正文里散落的 token ---
    body = text
    m = _LOC_BLOCK_RE.search(text)
    if m:
        body = m.group("body")
        # 「|| 」之后到行尾是原因描述；允许错误文本前后带空白。
        reason_match = re.search(r"\|\|\s*(.+)$", text, re.S)
        if reason_match:
            out["message"] = reason_match.group(1).strip().splitlines()[0][:500]

    for key, val in _LOC_KV_RE.findall(body):
        if key == "id":
            out["object_id"] = val
        elif key == "value":
            out["value"] = val
            out["value_masked"] = _mask_value(val)
        else:
            out[key] = val

    # 若整段没写 [定位]，退化为全文找 kv（同样只取第一次出现）
    if not any([out["model"], out["object_id"], out["field"]]):
        for key, val in _LOC_KV_RE.findall(text):
            if key == "id" and not out["object_id"]:
                out["object_id"] = val
            elif key == "value" and not out["value"]:
                out["value"] = val
                out["value_masked"] = _mask_value(val)
            elif key == "model" and not out["model"]:
                out["model"] = val
            elif key == "field" and not out["field"]:
                out["field"] = val
            elif key == "stage" and not out["stage"]:
                out["stage"] = val

    # --- errcode（业务错误码）---
    for pat in _ERRCODE_PATTERNS:
        mm = pat.search(text)
        if mm:
            out["errcode"] = int(mm.group(1))
            break
    if out["errcode"] is None:
        # 兜底只接受 errdict.json 已知的业务码，避免把 error_code 的片段误认成 errcode。
        known_codes = (_load_errdict().get("errcodes") or {}).keys()
        for code in re.findall(r"\b(\d{4,6})\b", text):
            if code in known_codes:
                out["errcode"] = int(code)
                break

    # --- error_code（毫秒时间戳 / 服务端日志索引）---
    stripped = text.strip()
    if re.fullmatch(r"\d{10,}", stripped):
        out["error_code"] = stripped
    else:
        mm = _ERROR_CODE_RE.search(text)
        if mm:
            out["error_code"] = mm.group(1)

    # --- 原因兜底 ---
    if not out["message"]:
        mm = re.search(r"(?:错误原因|原因|message|errmsg|description)\s*[:：]\s*(.+)$",
                       text, re.I | re.S)
        if mm:
            out["message"] = mm.group(1).strip().splitlines()[0][:500]

    # --- log_type（云函数名）---
    mm = re.search(r"log_type[\"']?\s*[:=]\s*\"?([A-Za-z_][\w]*)", text)
    if mm:
        out["log_type"] = mm.group(1)

    return out


# --------------------------------------------------------------------------- #
#  ② 错误码词典
# --------------------------------------------------------------------------- #

_ERRDICT_CACHE = {"mtime": 0.0, "data": None}
_INFERRED_CACHE = {"mtime": 0.0, "data": None}


def _load_errdict() -> "dict":
    """加载 errdict.json（带 mtime 缓存，改文件立即生效）。"""
    try:
        mt = _ERRDICT_PATH.stat().st_mtime
    except OSError:
        return {}
    if _ERRDICT_CACHE["data"] is not None and _ERRDICT_CACHE["mtime"] == mt:
        return _ERRDICT_CACHE["data"]
    try:
        data = json.loads(_ERRDICT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CF-DIAG] 读取 errdict.json 失败: {e}")
        return {}
    _ERRDICT_CACHE.update(mtime=mt, data=data)
    return data


def _load_inferred_errdict() -> "dict":
    """加载 errdict_inferred.json（带 mtime 缓存）。

    该文件由 ``tools/cf_errdict_expand.py`` 从参考源码 errors.py 生成，
    把 errdict 覆盖的错误码从几十个扩到 200+。其中除人工维护的条目外，
    中文含义均由常量名推断（``verified=false``），只作兜底提示。
    """
    try:
        mt = _INFERRED_DICT_PATH.stat().st_mtime
    except OSError:
        return {}
    if _INFERRED_CACHE["data"] is not None and _INFERRED_CACHE["mtime"] == mt:
        return _INFERRED_CACHE["data"]
    try:
        data = json.loads(_INFERRED_DICT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CF-DIAG] 读取 errdict_inferred.json 失败: {e}")
        return {}
    _INFERRED_CACHE.update(mtime=mt, data=data)
    return data


def _lookup_errdict(errcode) -> "dict | None":
    """查错误码释义：人工维护的主词典优先，推断词典兜底。

    返回条目带 ``verified`` 标记，AI 应据此判断释义可信度：
    - ``verified=True``：人工维护，可直接引用；
    - ``verified=False``：由常量名推断，仅作方向提示，不可当官方文案。
    """
    if not errcode:
        return None
    key = str(errcode)
    main = (_load_errdict().get("errcodes") or {}).get(key)
    if main:
        return {**main, "verified": True, "source": "errdict.json"}
    inferred = (_load_inferred_errdict().get("errcodes") or {}).get(key)
    if inferred:
        return inferred
    return None


# --------------------------------------------------------------------------- #
#  ③ Token 健康度
# --------------------------------------------------------------------------- #

def cf_token_health(server_url: str = "", token: str = "") -> "dict":
    """诊断 Token 健康度。

    HCM token 是 Tornado secure cookie，第 3 段嵌了签发时间戳。
    ⚠️ 不能用缓存里的 ``ts`` 判断过期——那是「最后一次登录尝试时间」，
    登录失败时也会被更新但 token 保持旧值。
    """
    tok = (token or "").strip()
    if not tok and server_url:
        cached = _CF_TOKEN_CACHE.get(server_url.rstrip("/"))
        if isinstance(cached, dict):
            tok = (cached.get("token") or "").strip()
    res = {
        "token_provided": bool(tok),
        "issue_ts": None,
        "issue_time": None,
        "age_hours": None,
        "ttl_hours": _HCM_TOKEN_TTL_HOURS,
        "expired": None,
        "hint": "",
    }
    if not tok:
        res["hint"] = "未提供 token，无法判断健康度"
        return res
    m = _TOKEN_TS_RE.search(tok)
    if not m:
        res["hint"] = "token 不是 Tornado secure cookie 格式，无法解析签发时间"
        return res
    ts = int(m.group(1))
    age_h = (time.time() - ts) / 3600.0
    res["issue_ts"] = ts
    res["issue_time"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    res["age_hours"] = round(age_h, 2)
    res["expired"] = age_h > _HCM_TOKEN_TTL_HOURS
    res["hint"] = (
        f"token 已签发 {age_h:.1f} 小时，超过默认 {_HCM_TOKEN_TTL_HOURS} 小时 TTL，"
        f"高度疑似过期（17003 最常见真因）"
        if res["expired"] else
        f"token 签发 {age_h:.1f} 小时，仍在 {_HCM_TOKEN_TTL_HOURS} 小时 TTL 内"
    )
    return res


# --------------------------------------------------------------------------- #
#  ④ Wiki 路由（运行时解析 ERROR_ROUTE_INDEX.md，不硬编码规则）
# --------------------------------------------------------------------------- #

_ROUTE_CACHE = {"mtime": 0.0, "data": None}

# 错误模式 → 用于匹配的关键词（命中即路由到该模式下的文档）
# 注意：这里只放「关键词」，文档清单仍从 md 里解析，避免两处维护。
_PATTERN_KEYWORDS = {
    "token": ["token", "17003", "51006", "未登录", "登录过期", "登录态", "会话"],
    "field": ["字段", "400014", "keyerror", "nonetype", "field=", "字段缺失", "空值"],
    "list": ["list", "查询为空", "返回空", "filter_dict", "conditions", "page_index"],
    "relation": ["关联", "relation", "rec.get", "关联字段"],
    "metadata": ["元数据", "schema", "additionalproperties", "校验不通过", "meta_"],
    "type": ["类型错误", "typeerror", "类型转换", "转换失败"],
    "formula": ["common_formula", "social_formula", "公式", "云函数执行", "execute"],
    "locate": ["无法定位", "错误号", "1693", "traceback", "hide_error_msg"],
}


def _parse_route_index() -> "dict":
    """解析 ERROR_ROUTE_INDEX.md，抽出三种路由表。

    返回::

        {
          "errcode": {"17003": {"docs": [...], "name": "...", "meaning": "..."}},
          "patterns": [{"key": "token", "title": "...", "symptoms": "...", "docs": [...]}],
          "log_type": {"daily_overtime": {"docs": [...], "note": "..."}},
        }
    """
    try:
        mt = _ROUTE_INDEX_PATH.stat().st_mtime
    except OSError:
        return {"errcode": {}, "patterns": [], "log_type": {}}
    if _ROUTE_CACHE["data"] is not None and _ROUTE_CACHE["mtime"] == mt:
        return _ROUTE_CACHE["data"]

    try:
        raw = _ROUTE_INDEX_PATH.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[CF-DIAG] 读取 ERROR_ROUTE_INDEX.md 失败: {e}")
        return {"errcode": {}, "patterns": [], "log_type": {}}

    result = {"errcode": {}, "patterns": [], "log_type": {}}

    # --- 一、按 errcode 路由（表格：| errcode | 名称 | 含义 | 首选文档 | 补充文档 |）---
    for line in raw.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        code_raw = cells[0]
        if not re.fullmatch(r"\d{4,6}(?:~\d{4,6})?", code_raw):
            continue
        docs = [c.strip("` ").strip() for c in cells[3:] if c.strip() and c.strip() != "—"]
        for part in re.findall(r"\d{4,6}", code_raw):
            result["errcode"][part] = {
                "name": cells[1].strip("` "),
                "meaning": cells[2],
                "docs": docs,
            }

    # --- 二、按错误模式路由（### 2.X 标题 + 表格里的文档）---
    # 先截取「二、按错误模式路由」整节，避免最后一个小节把后面的「三、…」也吞进来。
    m2 = re.search(r"##\s*二、按错误模式路由(.*?)(?=\n##\s|\Z)", raw, re.S)
    sections = re.split(r"\n###\s+", m2.group(1) if m2 else "")
    for sec in sections[1:]:
        lines = sec.splitlines()
        title = lines[0].strip() if lines else ""
        symptoms = ""
        mm = re.search(r"\*\*典型表现\*\*[：:]\s*(.+)", sec)
        if mm:
            symptoms = mm.group(1).strip()
        # 模式 key 用「标题 + 典型表现」打分推断。
        # 只用这两处做 haystack —— 若把表格里的文档名也算进来，
        # 会引入 `04_list_param_penetration_v1.md` 这类噪声导致误判。
        hay = (title + " " + symptoms).lower()
        key, best = None, 0
        for k, kws in _PATTERN_KEYWORDS.items():
            score = sum(hay.count(kw.lower()) for kw in kws)
            if score > best:
                key, best = k, score
        docs = []
        for line in lines:
            if not line.startswith("|"):
                continue
            # 反引号包裹的 .md / .py 路径
            docs.extend(re.findall(r"`([^`]*\.(?:md|py|json))`", line))
        # 去重保序
        seen, uniq = set(), []
        for d in docs:
            if d not in seen:
                seen.add(d)
                uniq.append(d)
        if uniq:
            result["patterns"].append({
                "key": key, "title": title, "symptoms": symptoms, "docs": uniq,
            })

    # --- 三、按 log_type 路由（表格：| log_type | 可能涉及的 Wiki | 说明 |）---
    m3 = re.search(r"##\s*三、按云函数日志类型路由(.*?)(?=\n##\s|\Z)", raw, re.S)
    if m3:
        for line in m3.group(1).splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or not cells[0] or cells[0].startswith("---") or cells[0] == "log_type":
                continue
            docs = [c.strip("` ").strip() for c in cells[1].split("`") if c.strip().endswith(".md")]
            if not docs:
                docs = re.findall(r"`([^`]*\.md)`", cells[1])
            result["log_type"][cells[0]] = {
                "docs": docs,
                "note": cells[2] if len(cells) > 2 else "",
            }

    _ROUTE_CACHE.update(mtime=mt, data=result)
    return result


def _resolve_doc_path(ref: str) -> "Path | None":
    """把索引里的文档引用解析成真实路径。

    索引里的写法有三种：
    - 相对 reference 目录：`01_FIELD_SCHEMA.md`
    - 带子目录：`info_form_relations/01_SCHEMA_MATRIX.md`
    - 绝对路径/相对项目根：`store/downloads/895/skills/...`
    - 目录（以 / 结尾）：返回 None，目录不适合做片段抽取
    """
    ref = (ref or "").strip().strip("`").strip()
    if not ref or ref == "—":
        return None
    if ref.endswith("/"):
        return None
    if not ref.endswith((".md", ".py", ".json")):
        return None
    candidates = []
    p = Path(ref)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(_PROJECT_ROOT / ref)
        candidates.append(_REF_DIR / ref)
        # ERROR_ROUTE_INDEX 中的 `metadata/specs/...` 是相对
        # `store/downloads/895/docs`，而不是相对 reference 目录。
        candidates.append(_REF_DIR.parent / ref)
        candidates.append(_REF_DIR.parent.parent / ref)
        candidates.append(_REF_DIR / "wiki" / ref)
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _extract_snippet(path: "Path", keywords, max_chars: int = 1500) -> "dict":
    """从 Wiki 文档抽取与关键词最相关的片段。

    策略：按 markdown 标题切块 → 命中关键词的块优先 → 依次拼接直到 max_chars。
    没有任何命中的话，返回文档开头（通常是概述 + 参数表），也有诊断价值。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"file": str(path.relative_to(_PROJECT_ROOT)), "address": _file_address(path), "section": "",
                "content": "", "error": f"读取失败: {e}"}

    rel = str(path.relative_to(_PROJECT_ROOT))
    lines = raw.splitlines()
    blocks, cur_title, cur_buf = [], "(概述)", []
    for line in lines:
        m = _HEAD_RE.match(line)
        if m:
            if cur_buf:
                blocks.append((cur_title, "\n".join(cur_buf).strip()))
            cur_title, cur_buf = m.group(2).strip(), []
        else:
            cur_buf.append(line)
    if cur_buf:
        blocks.append((cur_title, "\n".join(cur_buf).strip()))

    kws = [k.lower() for k in (keywords or []) if k]
    scored = []
    for title, body in blocks:
        if not body:
            continue
        hay = (title + "\n" + body).lower()
        score = sum(hay.count(k) for k in kws) if kws else 0
        scored.append((score, title, body))
    scored.sort(key=lambda x: -x[0])

    picked, total = [], 0
    for score, title, body in scored:
        if total >= max_chars:
            break
        if score == 0 and picked:
            # 已有命中块时，低相关的块不再补
            continue
        room = max_chars - total
        chunk = body if len(body) <= room else body[:room] + "\n…(截断)"
        picked.append(f"### {title}\n{chunk}")
        total += len(chunk)
        if score == 0:
            break  # 无命中时只取开头一块

    section = picked[0].split("\n", 1)[0].lstrip("# ").strip() if picked else ""
    return {
        "file": rel,
        "address": _file_address(path),
        "section": section,
        "content": "\n\n".join(picked),
        "matched": bool(kws) and any(s > 0 for s, _, _ in scored),
        "read_hint": f"Read {path} and inspect the `{section}` section" if section else f"Read {path}",
    }


def _route_wiki(parsed: "dict", max_docs: int = 3, max_chars: int = 1500) -> "dict":
    """根据解析结果路由到最相关的 Wiki 文档并抽取片段。"""
    index = _parse_route_index()
    text = " ".join(str(v) for v in parsed.values() if v)
    low = text.lower()
    errcode = parsed.get("errcode")

    matched_patterns, doc_refs = [], []
    for pat in index["patterns"]:
        key = pat.get("key")
        kws = _PATTERN_KEYWORDS.get(key, [])
        hit = [k for k in kws if k.lower() in low]
        # 特殊：errcode 直接命中关键词表
        if errcode and str(errcode) in kws:
            hit.append(str(errcode))
        if hit:
            matched_patterns.append({"key": key, "title": pat.get("title", ""),
                                     "hit_keywords": hit})
            doc_refs.extend(pat.get("docs", []))

    # errcode 路由表补充
    if errcode:
        ec_entry = index["errcode"].get(str(errcode))
        if ec_entry:
            doc_refs.extend(ec_entry.get("docs", []))

    # log_type 路由表补充
    lt = parsed.get("log_type")
    if lt and lt in index["log_type"]:
        doc_refs.extend(index["log_type"][lt].get("docs", []))

    # 去重保序
    seen, uniq = set(), []
    for d in doc_refs:
        if d not in seen:
            seen.add(d)
            uniq.append(d)

    # 解析路径 + 抽取片段
    keywords = [parsed.get("field"), parsed.get("model"), parsed.get("stage"),
                parsed.get("log_type"), parsed.get("message")]
    keywords = [str(k) for k in keywords if k]
    if errcode:
        keywords.append(str(errcode))

    snippets, missing = [], []
    for ref in uniq:
        if len(snippets) >= max_docs:
            break
        p = _resolve_doc_path(ref)
        if p is None:
            missing.append(ref)
            continue
        snip = _extract_snippet(p, keywords, max_chars=max_chars)
        if snip.get("content"):
            snippets.append(snip)

    return {
        "matched_patterns": matched_patterns,
        "snippets": snippets,
        "referenced_but_missing": missing,
        "all_referenced": uniq,
    }


# --------------------------------------------------------------------------- #
#  ⑤ 日志结构化解析（供 cf_export_logs 复用）
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"\[([^\]]{1,80})\]")
_ERROR_HINTS = ("[定位]", "traceback", "errcode", "error", "失败", "异常", "错误", "✗", "failed")
_STAGE_KEYS = ("stage", "step", "phase")
_ID_KEYS = ("dept_id", "dept", "department_id", "object_id", "record_id", "employee_id", "id")
# cf_diag() 认可的级别标签（与 cf_diag_snippet._DIAG_LEVELS 对齐）
_DIAG_LEVELS = ("DEBUG", "INFO", "WARN", "WARNING", "ERROR")


def _safe_literal(s: str):
    """尝试把 Python repr 字符串解析成对象（ast.literal_eval 比 eval 安全）。"""
    s = s.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


# --- cf_diag() 标准化日志（见 tools/cf_locate_kit/cf_diag_snippet.py）---
# 格式：[DIAG][<LEVEL>][stage:xxx][model:X][id:Y][field:Z] 消息正文
# [DIAG] 是哨兵：命中即走精确解析，不再依赖正则猜测。
_DIAG_HEAD_RE = re.compile(r"^\[DIAG\]")
_DIAG_KV_MAP = {
    "stage": "stage", "model": "model", "id": "object_id",
    "field": "field", "rid": "rid", "dept": "dept_id",
}


def _parse_diag_content(raw: str, out: dict) -> bool:
    """解析 cf_diag() 输出的标准化日志；命中返回 True。

    标签形如 ``[stage:fetch]``，级别为裸标签 ``[INFO]`` / ``[ERROR]``。
    相比通用解析，这里字段语义是约定好的，无需猜测。
    """
    if not _DIAG_HEAD_RE.match(raw):
        return False
    out["schema"] = "cf-diag/v1"
    pos = raw.index("]") + 1
    # 逐个吃掉 [TOKEN]，直到遇到非标签内容
    while True:
        while pos < len(raw) and raw[pos] in " \t":
            pos += 1
        if pos >= len(raw) or raw[pos] != "[":
            break
        end = raw.find("]", pos)
        if end == -1:
            break
        token = raw[pos + 1:end]
        pos = end + 1
        if ":" in token:
            k, v = token.split(":", 1)
            key = _DIAG_KV_MAP.get(k.lower())
            if key:
                out[key] = v
                out["tags"][k] = v
            else:
                out["tags"][k] = v
        else:
            out["tags"][token] = ""
            if token in _DIAG_LEVELS:
                out["level"] = "WARNING" if token == "WARN" else token
    body = raw[pos:].strip()
    out["message"] = body[:500] if body else None
    # 正文末尾可能挂 JSON 扩展（traceback / errcode / 进度等）
    if body:
        mm = re.search(r"(\{.*\})\s*$", body, re.S)
        if mm:
            d = _safe_literal(mm.group(1))
            if isinstance(d, dict):
                out["data"] = d
                if isinstance(d.get("errcode"), int):
                    out["errcode"] = d["errcode"]
                if d.get("traceback"):
                    out["is_error"] = True
                # 正文去掉 JSON 尾巴，避免消息被大段 traceback 淹没
                out["message"] = body[:mm.start()].strip()[:500] or out["message"]
    out["is_error"] = out["is_error"] or out["level"] in ("ERROR", "WARNING") or "[定位]" in raw
    return True


def parse_cf_log_content(content, log_type: str = "") -> "dict":
    """把 dynamic_log 的 content 字段解析成结构化字段。

    content 实测有三种形态（这也是 G3「日志格式不统一」的来源）：
    1. ``[RID:129837] [POINTS_MODULE] [WRITE] ✓ edit 成功, record_id=23178667``（标签式）
    2. ``{'report': 'daily_overtime', 'stage': 'start', ...}``（Python repr 字典）
    3. 纯文本，中间可能夹着 ``kwargs-{...}`` 这类 repr 片段

    统一输出::

        {
          "tags": {"RID": "129837", "POINTS_MODULE": "", "WRITE": ""},
          "level": "INFO",
          "stage": "start",
          "dept_id": 16078507,
          "object_id": 23178667,
          "errcode": 2,
          "is_error": False,
          "message": "✓ edit 成功, record_id=23178667",
          "data": {...},          # 解析出的字典（若有）
          "locate": {...},        # 若含 [定位] 则解析出来
        }
    """
    out = {
        "tags": {}, "level": None, "stage": None, "dept_id": None,
        "object_id": None, "errcode": None, "is_error": False,
        "message": None, "data": None, "locate": None,
        "model": None, "field": None, "rid": None, "schema": None,
    }
    if content is None:
        return out
    raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    raw = raw.strip()
    if not raw:
        return out

    # --- 0. cf_diag() 标准化日志：命中即精确解析，不走猜测逻辑 ---
    if _parse_diag_content(raw, out):
        if "[定位]" in raw:
            out["locate"] = cf_parse_error(raw)
        return out

    # --- 1. 抽取开头的 [TAG] 组 ---
    pos = 0
    while True:
        m = _TAG_RE.match(raw, pos)
        if not m:
            break
        token = m.group(1)
        # 只有「全大写/数字/下划线/冒号」的短 token 才算标签，避免吃掉 [定位] 正文
        if len(token) <= 40 and re.fullmatch(r"[\w:.\-]+", token):
            if ":" in token:
                k, v = token.split(":", 1)
                out["tags"][k] = v
            else:
                out["tags"][token] = ""
            pos = m.end()
            # 标签之间只允许空白
            while pos < len(raw) and raw[pos] in " \t":
                pos += 1
        else:
            break
    body = raw[pos:].strip()

    # --- 2. 级别 ---
    for lv in ("DEBUG", "INFO", "WARN", "WARNING", "ERROR", "DIAG_ERR"):
        if lv in out["tags"]:
            out["level"] = "WARNING" if lv == "WARN" else lv
            break

    # --- 3. 尝试解析为 Python 字面量 ---
    data = _safe_literal(body)
    if isinstance(data, dict):
        out["data"] = data
        for k in _STAGE_KEYS:
            if isinstance(data.get(k), str):
                out["stage"] = data[k]
                break
        for k in _ID_KEYS:
            v = data.get(k)
            if isinstance(v, int):
                if k == "dept_id" or k == "dept":
                    out["dept_id"] = v
                else:
                    out["object_id"] = v
        ec = data.get("errcode")
        if isinstance(ec, int):
            out["errcode"] = ec
        out["message"] = body[:500]
    else:
        # 非字典：正文可能夹着 repr 片段，尽力抠出来
        mm = re.search(r"[=\-:]\s*(\{.*\}|\[.*\])\s*$", body, re.S)
        if mm:
            d2 = _safe_literal(mm.group(1))
            if isinstance(d2, dict):
                out["data"] = d2
                ec = d2.get("errcode")
                if isinstance(ec, int):
                    out["errcode"] = ec
                for k in _STAGE_KEYS:
                    if isinstance(d2.get(k), str):
                        out["stage"] = d2[k]
                        break
        out["message"] = body[:500]

    # --- 4. errcode 兜底（正文里散落的 errcode=N）---
    if out["errcode"] is None:
        mm = re.search(r"errcode[\"']?\s*[:=]\s*\"?(-?\d{1,6})", body, re.I)
        if mm:
            out["errcode"] = int(mm.group(1))

    # --- 5. [定位] ---
    if "[定位]" in raw:
        loc = cf_parse_error(raw)
        if any([loc["model"], loc["field"], loc["object_id"]]):
            out["locate"] = loc
            out["stage"] = out["stage"] or loc.get("stage")
            out["errcode"] = out["errcode"] if out["errcode"] is not None else loc.get("errcode")
            out["is_error"] = True

    # --- 6. 是否疑似错误 ---
    low = raw.lower()
    if not out["is_error"]:
        # DIAG_ERR / ERROR 标签本身就是错误信号；普通「成功」日志仍不算错误。
        tagged_error = any(k in out["tags"] for k in ("ERROR", "DIAG_ERR", "EXCEPTION", "FAILED"))
        if tagged_error or (out["errcode"] not in (None, 0)) or any(h in low for h in _ERROR_HINTS):
            # errcode=0 / "成功" 这类不算错误，除非明确打了 ERROR 标签
            if tagged_error or not any(k in raw for k in ("✓", "成功", "success")):
                out["is_error"] = True
                if not out["level"]:
                    out["level"] = "ERROR"

    if log_type and not out["stage"]:
        out["stage"] = None
    return out


def cf_parse_log_rows(rows: "list") -> "list":
    """批量把日志行的 content 字段解析成结构化字段（保留原字段，新增 parsed_content）。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        pc = parse_cf_log_content(row.get("content"), row.get("log_type", ""))
        out.append({**row, "parsed_content": pc})
    return out


# --------------------------------------------------------------------------- #
#  ⑥ 参考源码证据、错误码覆盖率、错误日志关联
# --------------------------------------------------------------------------- #

_REFERENCE_ERROR_CACHE = {"mtime": 0.0, "data": None}
_SOURCE_SKIP_PARTS = {".venv", "venv", "__pycache__", ".git", "site-packages", "node_modules"}
_SOURCE_DIRS = ("cloud_functions", "config/cf_backup")
_SOURCE_FIXED_FILES = ("errors.py", "core/service/handlers.py", "apps/idm/auth_util.py")

# 特定错误码 → 最权威的参考源码（命中即强加权）。
# 业务云函数里常有成百上千处引用同一错误码，靠词频排序必然把真正的
# 「定性文件」淹没掉。这里按错误码显式指定该优先看哪个框架文件。
_ERRCODE_KEY_SOURCES = {
    # 17003 = Token 过期/未登录：先看 handlers.py 的异常包装（hide_error_msg
    # 会吞掉挂载信息），再看 auth_util.py 的 Token 校验与 TTL。
    17003: {
        "core/service/handlers.py": 20,
        "apps/idm/auth_util.py": 16,
    },
}


def _redact_text(value: str, max_chars: int = 700) -> str:
    """对源码/日志摘要做轻量脱敏，避免诊断上下文带出凭据或长敏感串。"""
    s = str(value or "")
    # 先处理 key=value / key: value 形式的凭据
    s = re.sub(
        r"(?i)((?:password|passwd|secret|authorization|cookie|token)\s*[:=]\s*)([\"']?)[^,;\s\"']+",
        r"\1***",
        s,
    )
    # 手机号/身份证/长数字只保留首尾，避免日志上下文暴露个人信息
    def _mask_number(match):
        value = match.group(1)
        return value[:4] + "****" + value[-2:]

    s = re.sub(r"(?<![\w])(\d{8,})(?![\w])", _mask_number, s)
    return s[:max_chars]


def _load_reference_error_catalog() -> dict:
    """从参考源码 errors.py 提取 AppException 定义，作为 errdict 的覆盖率校验源。"""
    path = _REFERENCE_ROOT / "errors.py"
    try:
        mt = path.stat().st_mtime
    except OSError:
        return {"codes": {}, "available": False, "file": "errors.py"}
    if _REFERENCE_ERROR_CACHE["data"] is not None and _REFERENCE_ERROR_CACHE["mtime"] == mt:
        return _REFERENCE_ERROR_CACHE["data"]

    result = {"codes": {}, "available": True, "file": "errors.py"}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as e:
        result.update({"available": False, "error": str(e)})
        return result

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not isinstance(func, ast.Name) or func.id != "AppException" or len(node.value.args) < 3:
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names:
            continue
        try:
            status, code, message = (ast.literal_eval(arg) for arg in node.value.args[:3])
        except (ValueError, SyntaxError):
            continue
        if not isinstance(code, int):
            continue
        item = {"name": names[0], "status_code": status, "errmsg": message}
        result["codes"].setdefault(str(code), []).append(item)

    _REFERENCE_ERROR_CACHE.update(mtime=mt, data=result)
    return result


def _reference_error_info(errcode) -> dict:
    """返回参考源码错误码定义和当前 errdict 覆盖率。

    覆盖率区分两层：
    - verified_coverage_percent：仅人工维护的 errdict.json（verified=true）
    - inferred_coverage_percent：errdict.json + errdict_inferred.json（含由常量名推断的词条）
    """
    catalog = _load_reference_error_catalog()
    verified_codes = set((_load_errdict().get("errcodes") or {}).keys())
    inferred_codes = set((_load_inferred_errdict().get("errcodes") or {}).keys())
    source_codes = set(catalog.get("codes", {}).keys())
    code = str(errcode) if errcode is not None else ""
    verified_cov = round(len(verified_codes & source_codes) / len(source_codes) * 100, 1) if source_codes else None
    inferred_cov = round(len((verified_codes | inferred_codes) & source_codes) / len(source_codes) * 100, 1) if source_codes else None
    return {
        "available": bool(catalog.get("available")),
        "file": catalog.get("file", "errors.py"),
        "matched": catalog.get("codes", {}).get(code, []),
        "verified_errdict_count": len(verified_codes),
        "inferred_errdict_count": len(inferred_codes),
        "local_errdict_count": len(verified_codes | inferred_codes),
        "source_error_code_count": len(source_codes),
        "missing_from_local_errdict": sorted(source_codes - (verified_codes | inferred_codes)),
        "verified_coverage_percent": verified_cov,
        "inferred_coverage_percent": inferred_cov,
        "coverage_percent": inferred_cov,
    }


def _iter_reference_source_files():
    """迭代可用于证据检索的云函数源码，跳过依赖和缓存目录。"""
    seen = set()
    for rel_dir in _SOURCE_DIRS:
        root = _REFERENCE_ROOT / rel_dir
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SOURCE_SKIP_PARTS]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = Path(dirpath) / name
                try:
                    rel = str(path.relative_to(_REFERENCE_ROOT))
                except ValueError:
                    continue
                if rel not in seen:
                    seen.add(rel)
                    yield path, rel
    for rel in _SOURCE_FIXED_FILES:
        path = _REFERENCE_ROOT / rel
        if path.is_file() and rel not in seen:
            seen.add(rel)
            yield path, rel


def _source_search_terms(parsed: dict) -> list:
    """构造源码检索词，优先使用可准确定位的标识符。"""
    terms = []
    for key in ("log_type", "model", "field", "stage"):
        value = str(parsed.get(key) or "").strip()
        if len(value) >= 2 and value.lower() not in {t.lower() for t in terms}:
            terms.append(value)
    if parsed.get("errcode"):
        terms.append(str(parsed["errcode"]))
    # 原因文本只取带有 Python/接口语义的短 token，避免中文普通词命中过多文件。
    message = str(parsed.get("message") or "")
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", message):
        if token.lower() not in {t.lower() for t in terms}:
            terms.append(token)
    return terms[:8]


_SOURCE_INDEX_CACHE = {"mtime": 0.0, "data": None}
_SOURCE_INDEX_SCHEMA = "hcm-cf-source-index/v1"


def _source_index_file_record(path: Path, rel: str, raw: str, index: int) -> dict:
    """构造单个源码文件的轻量索引记录。"""
    lines = raw.splitlines()
    term_lines = {}
    for number, line in enumerate(lines, 1):
        # 重点索引字符串字面量（字段/model/log_type 通常在这里）、错误码、
        # 类/函数名和 HCM API 调用；不把普通局部变量全部写入索引，控制体积。
        tokens = set(re.findall(r"\b\d{4,6}\b", line))
        for literal in re.findall(r"['\"]([^'\"]{3,100})['\"]", line):
            tokens.update(re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", literal))
        if re.search(r"\b(class|def)\s+|call_open_api|call_api|self\.log|_diag|traceback|dynamic_log", line):
            tokens.update(re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", line))
        for token in tokens:
            key = token.lower()
            term_lines.setdefault(key, []).append(number)
    classes = re.findall(r"^\s*class\s+([A-Za-z_]\w*)", raw, re.M)
    functions = re.findall(r"^\s*def\s+([A-Za-z_]\w*)", raw, re.M)
    open_apis = sorted(set(re.findall(r"(?:call_open_api|call_api)\s*\(\s*['\"]([^'\"]+)", raw)))
    models = sorted(set(re.findall(r"['\"]model['\"]\s*:\s*['\"]([^'\"]+)", raw)))
    signals = {
        "has_execute": bool(re.search(r"^\s*def\s+execute\s*\(", raw, re.M)),
        "has_traceback": "traceback.format_exc" in raw,
        "has_dynamic_log": "dynamic_log" in raw,
        "has_self_log": bool(re.search(r"\bself\.log\s*\(", raw)),
        "has_diag": bool(re.search(r"(?:self\.)?_diag\s*\(", raw)),
        "has_location_marker": "[定位]" in raw or "locate_guard" in raw,
    }
    # term_lines 是用于定位行号的索引；过滤超短/噪声 token，控制 JSON 大小。
    term_lines = {k: v[:20] for k, v in term_lines.items() if len(k) >= 3}
    return {
        "index": index,
        "path": rel,
        "absolute_path": str(path),
        "size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "classes": classes[:30],
        "functions": functions[:100],
        "open_apis": open_apis[:50],
        "models": models[:100],
        "signals": signals,
        "term_lines": term_lines,
    }


def _build_source_index(persist: bool = True) -> dict:
    """扫描一次参考源码并生成可复用索引；默认写入项目 reference 目录。"""
    if not _REFERENCE_ROOT.is_dir():
        return {"schema": _SOURCE_INDEX_SCHEMA, "available": False, "files": [], "term_files": {}}
    records, term_files = [], {}
    for path, rel in _iter_reference_source_files():
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            record = _source_index_file_record(path, rel, raw, len(records))
        except (OSError, UnicodeError):
            continue
        records.append(record)
        for term in record["term_lines"]:
            term_files.setdefault(term, []).append(record["index"])
    data = {
        "schema": _SOURCE_INDEX_SCHEMA,
        "available": True,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_root": str(_REFERENCE_ROOT),
        "source_root_exists": True,
        "file_count": len(records),
        "files": records,
        "term_files": term_files,
    }
    _SOURCE_INDEX_CACHE.update(mtime=time.time(), data=data)
    if persist:
        try:
            _SOURCE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SOURCE_INDEX_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning(f"[CF-DIAG] 写入源码索引失败: {e}")
    return data


def _load_source_index(force: bool = False) -> dict:
    """加载持久化源码索引；不存在或 force 时才扫描源码。"""
    if not force and _SOURCE_INDEX_CACHE["data"] is not None:
        return _SOURCE_INDEX_CACHE["data"]
    if not force:
        try:
            data = json.loads(_SOURCE_INDEX_PATH.read_text(encoding="utf-8"))
            if data.get("schema") == _SOURCE_INDEX_SCHEMA and data.get("source_root") == str(_REFERENCE_ROOT):
                _SOURCE_INDEX_CACHE.update(mtime=_SOURCE_INDEX_PATH.stat().st_mtime, data=data)
                return data
        except (OSError, ValueError):
            pass
    return _build_source_index(persist=True)


def cf_rebuild_source_index() -> dict:
    """显式重建参考源码索引，供源码更新后调用。"""
    data = _build_source_index(persist=True)
    return {
        "ok": bool(data.get("available")),
        "path": str(_SOURCE_INDEX_PATH),
        "source_root": str(_REFERENCE_ROOT),
        "file_count": data.get("file_count", 0),
        "generated_at": data.get("generated_at"),
    }


def _find_source_evidence(parsed: dict, limit: int = 8) -> dict:
    """用持久化索引定位参考源码证据，仅读取命中的少量源码文件。"""
    if not _REFERENCE_ROOT.is_dir():
        return {"available": False, "terms": [], "hits": [], "scanned_files": 0, "index": {"available": False}}
    terms = _source_search_terms(parsed)
    index = _load_source_index()
    if not terms:
        return {"available": True, "terms": [], "hits": [], "scanned_files": 0,
                "index": {"path": str(_SOURCE_INDEX_PATH), "file_count": index.get("file_count", 0)}}

    records = index.get("files") or []
    term_files = index.get("term_files") or {}
    log_type = str(parsed.get("log_type") or "").lower()
    errcode = parsed.get("errcode")
    fixed_files = set(_SOURCE_FIXED_FILES)

    # ---- 阶段一：只用索引打分（不读源码），得到完整排序 ---------------------- #
    # ⚠️ 旧实现在这里踩过坑：先按 base_score 取前 limit*2 个候选、再读文件算 bonus。
    # 当某个错误码在 cloud_functions 里命中超过 16 个文件时（17003 有上百个），
    # 由于所有候选 base_score 都是 1，sorted 保持稳定顺序 = 索引下标顺序，
    # 而固定证据文件（errors.py / core/service/handlers.py / apps/idm/auth_util.py）
    # 是追加在索引末尾的（下标 403/404/405），于是**永远进不了候选集**，
    # 它的 +12 固定加权与 +20 错误码加权根本没机会生效 —— 最权威的证据被
    # 一堆只是恰好写了该错误码的业务云函数挤掉。
    # 正确做法：先把加权全部算完再排序，最后才按名次读文件。
    base_scores = {}
    for term in terms:
        for file_index in term_files.get(term.lower(), []):
            base_scores[file_index] = base_scores.get(file_index, 0) + 1
    for record in records:
        if log_type and log_type in Path(record.get("path", "")).stem.lower():
            base_scores[record.get("index")] = base_scores.get(record.get("index"), 0) + 8

    scored = []
    for file_index, base_score in base_scores.items():
        if not isinstance(file_index, int) or file_index >= len(records):
            continue
        record = records[file_index]
        rel = record.get("path", "")
        score = base_score
        if rel in fixed_files:
            score += 12
        for path_key, bonus in _ERRCODE_KEY_SOURCES.get(errcode, {}).items():
            if rel == path_key:
                score += bonus
        scored.append((score, rel, record))

    # 同分时按相对路径排序，保证结果稳定可复现（不依赖索引下标顺序）
    scored.sort(key=lambda item: (-item[0], item[1]))

    # ---- 阶段二：只读取进入前列的文件，构建带行号的证据 ---------------------- #
    candidates = []
    for score, rel, record in scored[:limit * 2]:
        path = Path(record.get("absolute_path", ""))
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        local_hits = []
        line_map = record.get("term_lines") or {}
        line_numbers = sorted({line for term in terms for line in line_map.get(term.lower(), [])})
        for number in line_numbers[:8]:
            start = max(0, number - 2)
            end = min(len(lines), number + 1)
            matched = [term for term in terms if number in line_map.get(term.lower(), [])]
            local_hits.append({
                "line": number,
                "matched": matched,
                "address": _file_address(path, number),
                "excerpt": _redact_text("\n".join(lines[start:end])),
            })
            if len(local_hits) >= 4:
                break
        if not local_hits:
            continue
        candidates.append({
            "file": rel,
            "address": _file_address(path),
            "score": score,
            "hits": local_hits,
            "signals": record.get("signals", {}),
            "index_record": {
                "classes": record.get("classes", []),
                "functions": record.get("functions", []),
                "open_apis": record.get("open_apis", []),
                "models": record.get("models", []),
            },
        })

    candidates.sort(key=lambda x: (-x["score"], x["file"]))
    return {
        "available": True,
        "terms": terms,
        "hits": candidates[:limit],
        "scanned_files": 0,
        "index": {
            "path": str(_SOURCE_INDEX_PATH),
            "address": _file_address(_SOURCE_INDEX_PATH),
            "file_count": index.get("file_count", len(records)),
            "generated_at": index.get("generated_at"),
            "used": True,
        },
    }


def _find_log_matches(parsed: dict, limit: int = 20) -> dict:
    """在本地已导出的 CF 日志中关联 error_code/errcode/定位字段。"""
    log_dir = _PROJECT_ROOT / "logs" / "cf_logs"
    if not log_dir.is_dir():
        return {"available": False, "matches": [], "scanned_files": 0}
    error_code = str(parsed.get("error_code") or "")
    errcode = parsed.get("errcode")
    model = str(parsed.get("model") or "").lower()
    field = str(parsed.get("field") or "").lower()
    log_type = str(parsed.get("log_type") or "").lower()
    if not any((error_code, errcode, model, field, log_type)):
        return {"available": True, "matches": [], "scanned_files": 0}

    matches, scanned = [], 0
    for path in sorted(log_dir.glob("*.json"), key=lambda p: -p.stat().st_mtime):
        scanned += 1
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in doc.get("logs", []) if isinstance(doc, dict) else []:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("content") or "")
            pc = row.get("parsed_content") if isinstance(row.get("parsed_content"), dict) else parse_cf_log_content(raw, row.get("log_type", ""))
            hay = (raw + " " + json.dumps(pc, ensure_ascii=False)).lower()
            score = 0
            if error_code and error_code in hay:
                score += 10
            if errcode is not None and re.search(rf"\berrcode\D*{re.escape(str(errcode))}\b", hay):
                score += 6
            if model and model in hay:
                score += 3
            if field and field in hay:
                score += 3
            if log_type and log_type in (str(row.get("log_type") or "").lower() + " " + hay):
                score += 2
            if score:
                matches.append({
                    "file": str(path.relative_to(_PROJECT_ROOT)),
                    "address": _file_address(path),
                    "id": row.get("id"),
                    "create_time": row.get("create_time"),
                    "log_type": row.get("log_type"),
                    "score": score,
                    "stage": pc.get("stage"),
                    "errcode": pc.get("errcode"),
                    "is_error": pc.get("is_error"),
                    "message": _redact_text(pc.get("message") or raw, 300),
                })
    matches.sort(key=lambda x: (-x["score"], x.get("create_time") or ""), reverse=False)
    return {"available": True, "matches": matches[:limit], "scanned_files": scanned}


def _build_evidence_bundle(parsed: dict, route: dict, reference_error: dict,
                           source_evidence: dict, log_matches: dict,
                           token_health: dict) -> dict:
    """构造紧凑的 AI 证据包：证据地址、命中原因、下一步读取提示。"""
    references = []
    for snippet in (route or {}).get("snippets", []):
        references.append({
            "kind": "wiki",
            "reason": "错误路由索引命中",
            "file": snippet.get("file"),
            "address": snippet.get("address"),
            "section": snippet.get("section"),
            "read_hint": snippet.get("read_hint"),
        })
    for hit in (source_evidence or {}).get("hits", []):
        for line_hit in (hit.get("hits") or [])[:3]:
            references.append({
                "kind": "source",
                "reason": f"源码索引命中: {', '.join(line_hit.get('matched') or [])}",
                "file": hit.get("file"),
                "address": line_hit.get("address") or hit.get("address"),
                "signals": hit.get("signals", {}),
                "read_hint": (line_hit.get("address") or hit.get("address") or {}).get("read_hint", ""),
            })
    for match in (log_matches or {}).get("matches", [])[:10]:
        references.append({
            "kind": "log",
            "reason": "error_code/errcode/定位字段关联",
            "file": match.get("file"),
            "address": match.get("address"),
            "id": match.get("id"),
            "create_time": match.get("create_time"),
            "read_hint": (match.get("address") or {}).get("read_hint", ""),
        })

    if reference_error.get("available"):
        error_path = _REFERENCE_ROOT / "errors.py"
        references.append({
            "kind": "source_definition",
            "reason": "参考源码 AppException 定义",
            "file": "errors.py",
            "address": _file_address(error_path),
            "read_hint": f"Read {error_path} around the AppException definition",
        })

    hints = []
    if token_health.get("expired") is True:
        hints.append("优先重新登录：Token 超过默认 TTL，17003/51006 可能是会话失效")
    if parsed.get("error_code"):
        hints.append("先按 error_code 在服务端日志或已导出 dynamic_log 中反查，再判断业务代码")
    if parsed.get("model") and parsed.get("field"):
        hints.append("对照当前对象字段值与源码字段访问位置，判断数据已修复还是代码仍会复现")
    if source_evidence.get("hits"):
        hints.append("优先 Read sourceEvidence 中带行号的源码地址，确认实际调用链")
    if route.get("snippets"):
        hints.append("再 Read Wiki 地址对应章节，核对字段/schema/conditions 约束")
    confidence = "high" if source_evidence.get("hits") and (log_matches.get("matches") or parsed.get("error_code")) else "medium"
    return {
        "schema": "hcm-cf-evidence-bundle/v1",
        "confidence": confidence,
        "references": references[:30],
        "hints": hints,
        "canonical_paths": list(dict.fromkeys(
            [r.get("address", {}).get("absolute_path") for r in references if r.get("address")]
        )),
    }


def _build_diagnosis_summary(parsed: dict, err_info: dict | None, token_health: dict,
                             current_data: dict | None, source_evidence: dict,
                             log_matches: dict) -> dict:
    """根据确定性信号给出保守的根因候选，不替代 AI 的最终判断。"""
    code = parsed.get("errcode")
    root_cause = "UNKNOWN"
    confidence = 0.35
    reasons = []
    checks = []

    if token_health.get("expired") is True:
        root_cause, confidence = "TOKEN_EXPIRED", 0.92
        reasons.append("Token 签发时间超过默认 TTL")
        checks.append("重新登录并使用当前网关签发的新 Token 重试")
    elif code == 51006:
        root_cause, confidence = "LOGIN_EXPIRED", 0.88
        reasons.append("errcode=51006 对应登录态失效")
        checks.append("重新登录后重试")
    elif code == 400011:
        root_cause, confidence = "FIELD_NOT_FOUND", 0.86
        reasons.append("errcode=400011 对应字段不存在")
        checks.append("读取 model 元数据，确认字段名和版本")
    elif code == 400012:
        root_cause, confidence = "OBJECT_NOT_FOUND", 0.86
        reasons.append("errcode=400012 对应对象不存在")
        checks.append("确认对象 ID、模型和租户范围")
    elif code == 400014:
        root_cause, confidence = "FIELD_DATA_INVALID", 0.78
        reasons.append("errcode=400014 对应字段数据不合法")
        checks.append("对比报错时字段值、当前对象值和字段 schema")
    elif code == 18003:
        root_cause, confidence = "PERMISSION_DENIED", 0.82
        reasons.append("errcode=18003 对应无权限访问")
        checks.append("确认 Token 所属账号的模型/字段权限")
    elif code == 17003:
        root_cause, confidence = "OPEN_API_EXECUTION_ERROR", 0.55
        reasons.append("errcode=17003 是 OpenAPI/云函数异常的框架兜底码")
        checks.append("先确认 Token；仍失败时读取 error_code 关联日志和源码证据")

    if current_data is not None:
        if current_data.get("present") is False:
            reasons.append("当前对象字段为空或不存在")
        elif current_data.get("present") is True:
            reasons.append("当前对象字段有值，可能是错误后已修复")
    if source_evidence.get("hits"):
        reasons.append(f"已命中 {len(source_evidence['hits'])} 个参考源码文件")
    if log_matches.get("matches"):
        reasons.append(f"已关联 {len(log_matches['matches'])} 条本地日志")

    return {
        "root_cause": root_cause,
        "confidence": confidence,
        "status": "need_verification" if confidence < 0.9 else "high_probability",
        "reasons": reasons,
        "checks_to_run": checks,
        "dictionary_name": (err_info or {}).get("name"),
    }


# --------------------------------------------------------------------------- #
#  ⑦ 诊断上下文聚合（主入口）
# --------------------------------------------------------------------------- #

def cf_diagnose_context(req) -> "dict":
    """聚合诊断上下文：解析 + 词典 + Wiki 路由 + Token 健康度 + 案例库匹配。

    一次调用拿到全部素材，AI 无需再逐个文件读。
    """
    text = (getattr(req, "text", "") or "").strip()
    if not text:
        raise ValueError("缺少待诊断的错误文本（text）")

    parsed = cf_parse_error(text)

    # 显式传入的字段覆盖解析结果
    for src, dst in (("model", "model"), ("object_id", "object_id"), ("field", "field")):
        v = getattr(req, src, "") or ""
        if v.strip():
            parsed[dst] = v.strip()

    err_info = _lookup_errdict(parsed.get("errcode"))
    route = _route_wiki(
        parsed,
        max_docs=int(getattr(req, "max_docs", 3) or 3),
        max_chars=int(getattr(req, "max_chars", 1500) or 1500),
    )
    token_health = cf_token_health(
        server_url=getattr(req, "server_url", "") or "",
        token=getattr(req, "token", "") or "",
    )
    reference_error = _reference_error_info(parsed.get("errcode"))
    source_evidence = _find_source_evidence(parsed, limit=8)
    log_matches = _find_log_matches(parsed, limit=20)

    # value 只在本地解析阶段保留原始 token；对外响应和 AI prompt 只返回脱敏值。
    parsed_public = dict(parsed)
    parsed_public["value"] = parsed.get("value_masked")
    current_present = getattr(req, "current_present", None)
    current_data = None
    if current_present is not None or getattr(req, "current_value", ""):
        current_data = {
            "value": getattr(req, "current_value", "") or "(空)",
            "present": current_present,
        }

    # 历史相似案例
    cases = _find_similar_cases(parsed, limit=int(getattr(req, "case_limit", 5) or 5))
    evidence_bundle = _build_evidence_bundle(
        parsed, route, reference_error, source_evidence, log_matches, token_health,
    )
    summary = _build_diagnosis_summary(
        parsed, err_info, token_health, current_data, source_evidence, log_matches,
    )

    # 组装「AI 可直接消费」的诊断提示词
    ai_prompt = _build_diagnose_prompt(
        text, parsed, err_info, route, token_health, current_data,
        reference_error=reference_error, source_evidence=source_evidence, log_matches=log_matches,
        evidence_bundle=evidence_bundle, summary=summary,
    )

    return {
        "ok": True,
        "summary": summary,
        "parsed": parsed_public,
        "errDict": err_info,
        "wiki": route,
        "tokenHealth": token_health,
        "currentData": current_data,
        "similarCases": cases,
        "sourceReferences": {
            "available": _REFERENCE_ROOT.is_dir(),
            "root": str(_REFERENCE_ROOT) if _REFERENCE_ROOT.is_dir() else "",
            "files": _REFERENCE_FILES,
        },
        "referenceError": reference_error,
        "sourceEvidence": source_evidence,
        "logMatches": log_matches,
        "evidenceBundle": evidence_bundle,
        "aiPrompt": ai_prompt,
        "diagnosed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _build_diagnose_prompt(
    text, parsed, err_info, route, token_health, current_data=None,
    reference_error=None, source_evidence=None, log_matches=None, evidence_bundle=None,
    summary=None,
) -> str:
    """生成一段可直接喂给任意 AI 的诊断提示词（含错误原文 + 词典 + Wiki 片段）。"""
    L = ["# HCM 云函数执行错误诊断请求", "",
         "请基于以下已聚合的诊断上下文，定位根因并给出修复建议。", ""]
    L.append("## 错误原文")
    L.append("```")
    L.append(text.strip()[:4000])
    L.append("```")
    summary = summary or {}
    L.append("## 机器初步判断（需要结合证据验证）")
    L.append(f"- 候选根因: {summary.get('root_cause', 'UNKNOWN')}")
    L.append(f"- 置信度: {summary.get('confidence', 0)}")
    L.append(f"- 状态: {summary.get('status', 'need_verification')}")
    for reason in summary.get("reasons", [])[:6]:
        L.append(f"- 依据: {reason}")
    L.append("")
    L.append("## 已解析信息")
    for k, label in (("model", "模型(model)"), ("object_id", "对象ID"), ("field", "字段(field)"),
                     ("stage", "阶段(stage)"), ("errcode", "业务错误码"),
                     ("error_code", "错误号(error_code, 服务端日志索引)"),
                     ("log_type", "云函数(log_type)")):
        v = parsed.get(k)
        if v not in (None, ""):
            L.append(f"- {label}: {v}")
    if parsed.get("value_masked"):
        L.append(f"- 字段值(报错时,已脱敏): {parsed['value_masked']}")
    if parsed.get("message"):
        L.append(f"- 原因: {parsed['message']}")
    if current_data is not None:
        L.append(f"- 当前字段值: {current_data.get('value', '(空)')}（{ '有值' if current_data.get('present') else '空/不存在' }）")
    L.append("")

    if err_info:
        L.append("## 错误码词典")
        L.append(f"- 名称: {err_info.get('name')}")
        L.append(f"- 含义: {err_info.get('meaning')}")
        L.append(f"- 修复建议: {err_info.get('fix')}")
        L.append("")

    ref = reference_error or {}
    matched_defs = ref.get("matched") or []
    if matched_defs:
        L.append("## 参考源码错误定义")
        for item in matched_defs[:3]:
            L.append(f"- {item.get('name')} (HTTP {item.get('status_code')}): {item.get('errmsg')}")
        L.append(f"- 当前本地 errdict 覆盖率: 已校验 {ref.get('verified_coverage_percent')}%（errdict.json），含推断 {ref.get('inferred_coverage_percent')}%（源码唯一错误码 {ref.get('source_error_code_count')} 个）")
        L.append("")

    th = token_health or {}
    if th.get("age_hours") is not None:
        L.append("## Token 健康度")
        L.append(f"- 签发时间: {th.get('issue_time')}（年龄 {th.get('age_hours')} 小时）")
        L.append(f"- 默认 TTL: {th.get('ttl_hours')} 小时，是否疑似过期: {th.get('expired')}")
        L.append(f"- 结论: {th.get('hint')}")
        L.append("")

    source_hits = (source_evidence or {}).get("hits") or []
    if source_hits:
        L.append("## 参考云函数源码证据")
        L.append(f"- 检索词: {', '.join((source_evidence or {}).get('terms') or [])}")
        for item in source_hits[:5]:
            L.append(f"### `{item.get('file')}`")
            for hit in (item.get("hits") or [])[:2]:
                L.append(f"- L{hit.get('line')} 命中 {', '.join(hit.get('matched') or [])}")
                L.append("```python")
                L.append(hit.get("excerpt", ""))
                L.append("```")
        L.append("")

    log_hits = (log_matches or {}).get("matches") or []
    if log_hits:
        L.append("## 本地已导出日志关联")
        for item in log_hits[:8]:
            L.append(
                f"- `{item.get('file')}` id={item.get('id')} time={item.get('create_time')} "
                f"stage={item.get('stage')} score={item.get('score')}: {item.get('message', '')}"
            )
        L.append("")

    snips = (route or {}).get("snippets") or []
    if snips:
        L.append("## 相关 Wiki 规范片段（按需抽取）")
        for s in snips:
            L.append(f"### 来源: `{s.get('file')}`")
            address = s.get("address") or {}
            if address.get("absolute_path"):
                L.append(f"- 文档地址: `{address['absolute_path']}`")
            if s.get("section"):
                L.append(f"- 建议阅读章节: `{s['section']}`")
            L.append(s.get("content", ""))
            L.append("")

    bundle = evidence_bundle or {}
    refs = bundle.get("references") or []
    if refs:
        L.append("## 可直接提供给 AI 的证据地址")
        L.append(f"- 证据置信度: {bundle.get('confidence', 'unknown')}")
        for ref in refs[:20]:
            address = ref.get("address") or {}
            path = address.get("absolute_path") or ref.get("file") or ""
            line = address.get("line")
            suffix = f":{line}" if line else ""
            L.append(f"- [{ref.get('kind', 'evidence')}] `{path}{suffix}` — {ref.get('reason', '')}")
        L.append("")

    L.append("## 请回答")
    L.append("1. 根因是什么（字段缺失 / Token 过期 / 类型错误 / 权限不足 / 数据异常 / 网关不可达）？")
    L.append("2. 最小修复方案（代码改动 或 数据修复 SQL）。")
    L.append("3. 违反了哪条 Wiki 规范（若有）。")
    L.append("4. 预防建议（是否应补充到云函数编写规范）。")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
#  ⑦ 案例库
# --------------------------------------------------------------------------- #

def _find_similar_cases(parsed, limit: int = 5) -> "list":
    """在 logs/cf_cases/ 里找历史相似案例（按 errcode / model+field 打分）。"""
    if not _CASE_DIR.is_dir():
        return []
    errcode = parsed.get("errcode")
    model = (parsed.get("model") or "").lower()
    field = (parsed.get("field") or "").lower()
    log_type = (parsed.get("log_type") or "").lower()

    scored = []
    for p in sorted(_CASE_DIR.glob("*.md"), key=lambda x: -x.stat().st_mtime):
        if p.name == "README.md":
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except OSError:
            continue
        low = body.lower()
        score = 0
        if errcode and re.search(rf"errcode\s*[:：]?\s*{errcode}\b", low):
            score += 5
        if model and model in low:
            score += 3
        if field and field in low:
            score += 3
        if log_type and log_type in low:
            score += 2
        if score:
            title = ""
            for line in body.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("# ").strip()
                    break
            scored.append({"file": f"logs/cf_cases/{p.name}", "title": title,
                           "score": score, "mtime": p.stat().st_mtime})
    scored.sort(key=lambda x: (-x["score"], -x["mtime"]))
    return scored[:limit]


def cf_save_case(req) -> "dict":
    """保存诊断案例到 logs/cf_cases/，命名 cf_case_<errcode>_<log_type>_<ts>.md。"""
    content = (getattr(req, "content", "") or "").strip()
    if not content:
        raise ValueError("案例内容为空")
    _CASE_DIR.mkdir(parents=True, exist_ok=True)

    errcode = (getattr(req, "errcode", "") or "").strip() or "unknown"
    log_type = (getattr(req, "log_type", "") or "").strip() or "unknown"
    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40]
    ts = datetime.now()
    fname = f"cf_case_{safe(errcode)}_{safe(log_type)}_{ts.strftime('%Y%m%d_%H%M%S')}.md"
    fpath = _CASE_DIR / fname

    meta = [
        "---",
        f"errcode: {errcode}",
        f"log_type: {log_type}",
        f"saved_at: {ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"source: {getattr(req, 'source', 'manual') or 'manual'}",
        "---",
        "",
    ]
    try:
        fpath.write_text("\n".join(meta) + content, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[CF-DIAG] 案例写入失败: {e}")
        raise RuntimeError(f"写入文件失败: {e}")
    logger.info(f"[CF-DIAG] 案例已保存: {fpath}")
    return {"ok": True, "path": str(fpath), "filename": fname, "size": len(content)}


_FEEDBACK_RESULTS = {"correct", "partially_correct", "wrong", "unknown"}


def _resolve_case_file(case_file: str) -> Path:
    """只允许访问案例库内的 Markdown 文件，阻断路径穿越。"""
    raw = (case_file or "").strip()
    if raw.startswith("logs/cf_cases/"):
        raw = raw[len("logs/cf_cases/"):]
    candidate = (_CASE_DIR / raw).resolve()
    root = _CASE_DIR.resolve()
    if candidate.parent != root or candidate.suffix.lower() != ".md" or not candidate.is_file():
        raise ValueError("case_file 必须是 logs/cf_cases/ 下已存在的 Markdown 文件")
    return candidate


def cf_save_feedback(req) -> dict:
    """记录人工对 AI 诊断结果的确认，使用 JSONL 追加写入，不修改原案例。"""
    result = (getattr(req, "result", "") or "").strip().lower()
    if result not in _FEEDBACK_RESULTS:
        raise ValueError("result 必须是 correct / partially_correct / wrong / unknown")
    case_path = _resolve_case_file(getattr(req, "case_file", ""))
    _CASE_DIR.mkdir(parents=True, exist_ok=True)
    event = {
        "feedback_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "case_file": f"logs/cf_cases/{case_path.name}",
        "result": result,
        "actual_root_cause": (getattr(req, "actual_root_cause", "") or "").strip(),
        "fix_applied": getattr(req, "fix_applied", None),
        "notes": _redact_text(getattr(req, "notes", "") or "", 1000),
        "source": (getattr(req, "source", "manual") or "manual").strip(),
    }
    try:
        with _FEEDBACK_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.exception(f"[CF-DIAG] 反馈写入失败: {e}")
        raise RuntimeError(f"写入反馈失败: {e}")
    return {"ok": True, "feedback": event, "path": str(_FEEDBACK_PATH)}


def cf_feedback_metrics() -> dict:
    """统计诊断反馈质量，供迭代看板和后续规则优化使用。"""
    events = []
    if _FEEDBACK_PATH.is_file():
        for line in _FEEDBACK_PATH.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
    counts = {key: 0 for key in _FEEDBACK_RESULTS}
    for item in events:
        if item.get("result") in counts:
            counts[item["result"]] += 1
    judged = counts["correct"] + counts["partially_correct"] + counts["wrong"]
    accuracy = round((counts["correct"] + 0.5 * counts["partially_correct"]) / judged, 4) if judged else None
    return {
        "total": len(events),
        "counts": counts,
        "judged": judged,
        "accuracy": accuracy,
        "feedback_file": str(_FEEDBACK_PATH),
    }


# --------------------------------------------------------------------------- #
#  ⑧ 诊断→规范闭环（根据人工反馈反哺词典与路由索引）
# --------------------------------------------------------------------------- #

# 反馈中 result 字段表示「AI 诊断结论与真实情况的偏差」，只有 wrong / partially_correct
# 且带 actual_root_cause 或 notes 的样本才有反哺价值。
_FEEDBACK_LEARNABLE = {"wrong", "partially_correct"}

# ERROR_ROUTE_INDEX.md 末尾自动补充小节的锚点标题（其内部的 | 行仍会被全局扫描命中）。
_ROUTE_FB_ANCHOR = "## 四、反馈闭环自动补充（AI 诊断修正）"


def _extract_case_signals(case_body: str) -> "dict":
    """从案例正文里抽取 errcode / model / field 信号。"""
    err = None
    m = re.search(r"errcode\s*[:：]?\s*(\d{4,6})", case_body, re.I)
    if m:
        err = m.group(1)
    model = field = None
    mm = re.search(r"model\s*[=：]\s*([A-Za-z_][\w]*)", case_body)
    if mm:
        model = mm.group(1)
    fm = re.search(r"field\s*[=：]\s*([A-Za-z_][\w]*)", case_body)
    if fm:
        field = fm.group(1)
    return {"errcode": err, "model": model, "field": field}


def _collect_feedback_learnings(max_proposals: int = 100) -> "list":
    """扫描 feedback JSONL，聚合成按 errcode 归并的可学习条目。

    返回每个条目的结构::
        {
          "errcode": "17003",
          "model": "employee", "field": "id_card",
          "root_causes": ["TOKEN_EXPIRED", ...],   # 去重后的真实根因
          "notes": ["重新登录后恢复", ...],          # 去重后的修正备注
          "doc_suggestions": ["01_FIELD_SCHEMA.md"], # 从备注反引号引用或路由推断
          "sources": ["logs/cf_cases/cf_case_xxx.md"],
        }
    """
    events = []
    if _FEEDBACK_PATH.is_file():
        for line in _FEEDBACK_PATH.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)

    by_code = {}
    for ev in events:
        if ev.get("result") not in _FEEDBACK_LEARNABLE:
            continue
        rc = (ev.get("actual_root_cause") or "").strip()
        note = _redact_text(ev.get("notes") or "", 1000).strip()
        if not rc and not note:
            continue
        case_file = ev.get("case_file") or ""
        signals = {"errcode": None, "model": None, "field": None}
        if case_file:
            base = Path(case_file)
            # case_file 可能写成 logs/cf_cases/xxx.md 或裸名，统一在 _CASE_DIR 解析。
            cand = _CASE_DIR / base.name
            try:
                body = cand.read_text(encoding="utf-8")
                signals = _extract_case_signals(body)
            except OSError:
                pass
        code = signals.get("errcode")
        if not code:
            # 备注里若直接带了 errcode 也能学。
            em = re.search(r"errcode\s*[:：]?\s*(\d{4,6})", (rc + " " + note), re.I)
            if em:
                code = em.group(1)
        if not code:
            continue

        doc_sugs = re.findall(r"`([^`]*\.md)`", note)
        entry = by_code.setdefault(code, {
            "errcode": code, "model": signals.get("model"), "field": signals.get("field"),
            "root_causes": [], "notes": [], "doc_suggestions": [], "sources": [],
        })
        if rc and rc not in entry["root_causes"]:
            entry["root_causes"].append(rc)
        if note and note not in entry["notes"]:
            entry["notes"].append(note)
        for d in doc_sugs:
            if d not in entry["doc_suggestions"]:
                entry["doc_suggestions"].append(d)
        if case_file and case_file not in entry["sources"]:
            entry["sources"].append(case_file)

    consolidated = list(by_code.values())[:max_proposals]
    return consolidated


def cf_apply_feedback_learnings(apply: bool = False, max_proposals: int = 100) -> "dict":
    """扫描诊断反馈，生成「反馈闭环」修正提案；``apply=True`` 时回写词典与路由索引。

    安全策略：
    - 仅以 ``result=wrong / partially_correct`` 且带 ``actual_root_cause`` 或 ``notes`` 的反馈为样本；
    - ``apply=False`` 只产出提案 Markdown，不触碰 errdict.json / ERROR_ROUTE_INDEX.md；
    - ``apply=True`` 先备份再 merge：新 errcode 路由行追加到 ERROR_ROUTE_INDEX.md 末尾
      「四、反馈闭环自动补充」小节，该小节的 ``|`` 行会被 ``_parse_route_index`` 全局扫描命中。
    """
    learnings = _collect_feedback_learnings(max_proposals=max_proposals)
    main = _load_errdict()
    errcodes = main.get("errcodes") or {}
    proposals = []
    applied = {"errdict_new": [], "errdict_updated": [], "route_new": []}

    for item in learnings:
        code = item["errcode"]
        root_cause = "；".join(item["root_causes"]) or "（反馈未标注明确根因）"
        fix = "；".join(item["notes"]) or "（反馈未提供修复说明）"
        preferred = item["doc_suggestions"][0] if item["doc_suggestions"] else "errdict.json"
        supp = "；".join(item["doc_suggestions"][1:]) if len(item["doc_suggestions"]) > 1 else "—"
        existing = errcodes.get(code)
        entry = {
            "name": existing.get("name") if existing else f"FEEDBACK_{code}",
            "meaning": root_cause,
            "fix": fix,
            "verified": True,
            "source": "feedback",
            "feedback_updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if existing:
            # 追加反馈字段而非覆盖人工释义，避免丢掉既有 verified 文案。
            merged = dict(existing)
            merged.setdefault("feedback", [])
            merged["feedback"].append({
                "root_cause": root_cause,
                "fix": fix,
                "at": entry["feedback_updated_at"],
            })
            proposals.append({
                "errcode": code, "action": "update_errdict",
                "name": entry["name"], "meaning": root_cause, "fix": fix,
                "sources": item["sources"],
            })
            if apply:
                errcodes[code] = merged
                applied["errdict_updated"].append(code)
        else:
            proposals.append({
                "errcode": code, "action": "new_errdict",
                "name": entry["name"], "meaning": root_cause, "fix": fix,
                "sources": item["sources"],
            })
            if apply:
                errcodes[code] = entry
                applied["errdict_new"].append(code)
        # 路由行提案（无论是否新建 errdict，都保证路由表能命中该 errcode）。
        proposals.append({
            "errcode": code, "action": "new_route",
            "name": entry["name"], "meaning": root_cause,
            "preferred_doc": preferred, "supp_doc": supp,
        })
        if apply:
            applied["route_new"].append(code)

    # 生成提案 Markdown
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_lines = [
        f"# 反馈闭环修正提案（{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）",
        "",
        f"- 样本数（可学习反馈）: {len(learnings)}",
        f"- 模式: {'已回写（apply=True）' if apply else '仅预览（apply=False）'}",
        "",
        "## 一、errdict.json 修正",
    ]
    for p in proposals:
        if p["action"] in ("new_errdict", "update_errdict"):
            md_lines.append(
                f"- `[{p['action']}]` `{p['errcode']}` {p['name']}: "
                f"含义={p['meaning']}；修复={p['fix']}"
            )
    md_lines.append("")
    md_lines.append("## 二、ERROR_ROUTE_INDEX.md 新增路由行")
    for p in proposals:
        if p["action"] == "new_route":
            md_lines.append(
                f"- `| {p['errcode']} | {p['name']} | {p['meaning']} | {p['preferred_doc']} | {p['supp_doc']} |`"
            )
    md_lines.append("")
    md_lines.append("## 三、云函数编写规范建议（人工跟进）")
    md_lines.append("- 对命中率高的根因，建议在 `store/downloads/895/skills/cloud-function-writing/SKILL.md` 补充对应防御性编码规则（如：涉及 Token 的调用统一先做健康度校验）。")
    fields_hit = {p["errcode"] for p in learnings if p.get("field")}
    if fields_hit:
        md_lines.append(
            f"- errcode {', '.join(sorted(fields_hit))} 出现字段级错误，"
            f"可补充「字段变更需同步更新 FIELD_SCHEMA 文档」规则。"
        )

    proposal_text = "\n".join(md_lines)
    proposal_path = None
    if apply or learnings:
        _CASE_DIR.mkdir(parents=True, exist_ok=True)
        proposal_path = _CASE_DIR / f"feedback_learnings_proposal_{ts}.md"
        proposal_path.write_text(proposal_text, encoding="utf-8")

    if apply and proposals:
        # 备份 + 回写 errdict.json
        _backup_and_write_json(_REF_DIR / "errdict.json", {"_meta": main.get("_meta", {}), "errcodes": errcodes})
        # 备份 + 追加路由小节
        _append_route_feedback_section(applied["route_new"], proposals, learnings)

    return {
        "ok": True,
        "applied": apply,
        "sample_count": len(learnings),
        "proposals": proposals,
        "applied_changes": applied,
        "proposal_path": str(proposal_path) if proposal_path else None,
    }


def _backup_and_write_json(path: "Path", data: "dict"):
    """备份 JSON 后覆盖写入。"""
    if path.is_file():
        bak = path.with_suffix(path.suffix + f".bak.{int(datetime.now().timestamp())}")
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_route_feedback_section(codes: "list", proposals: "list", learnings: "list"):
    """把新路由行追加到 ERROR_ROUTE_INDEX.md 末尾的自动补充小节。"""
    if not codes:
        return
    lookup = {p["errcode"]: p for p in proposals if p["action"] == "new_route"}
    lines = [""]
    if _ROUTE_INDEX_PATH.is_file():
        existing = _ROUTE_INDEX_PATH.read_text(encoding="utf-8")
        if _ROUTE_FB_ANCHOR not in existing:
            lines.append("")
            lines.append(_ROUTE_FB_ANCHOR)
            lines.append("")
            lines.append("> 以下行由诊断反馈闭环自动追加（`cf_apply_feedback_learnings(apply=True)`）。")
            lines.append("")
            lines.append("| errcode | 名称 | 含义 | 首选文档 | 补充文档 |")
            lines.append("|---------|------|------|----------|----------|")
        else:
            lines = []
    else:
        lines.append("# AI 诊断云函数错误路由索引")
        lines.append("")
        lines.append(_ROUTE_FB_ANCHOR)
        lines.append("")
        lines.append("| errcode | 名称 | 含义 | 首选文档 | 补充文档 |")
        lines.append("|---------|------|------|----------|----------|")
    for code in codes:
        p = lookup.get(code)
        if not p:
            continue
        lines.append(f"| {code} | {p['name']} | {p['meaning']} | {p['preferred_doc']} | {p['supp_doc']} |")
    # 仅追加（_ROUTE_FB_ANCHOR 已存在时不清空旧内容）。
    with _ROUTE_INDEX_PATH.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    # 失效路由缓存，强制下次重新解析。
    _ROUTE_CACHE.update(mtime=0.0, data=None)


def cf_list_cases(keyword: str = "", limit: int = 50) -> "dict":
    """列出案例库条目（可按关键词过滤）。"""
    if not _CASE_DIR.is_dir():
        return {"cases": [], "total": 0}
    items = []
    for p in sorted(_CASE_DIR.glob("*.md"), key=lambda x: -x.stat().st_mtime):
        if p.name == "README.md":
            continue
        try:
            body = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if keyword and keyword.lower() not in body.lower() and keyword.lower() not in p.name.lower():
            continue
        title = ""
        errcode = ""
        for line in body.splitlines():
            if not title and line.startswith("#"):
                title = line.lstrip("# ").strip()
            if not errcode:
                mm = re.match(r"errcode:\s*(\S+)", line.strip())
                if mm:
                    errcode = mm.group(1)
        items.append({
            "file": f"logs/cf_cases/{p.name}",
            "filename": p.name,
            "title": title,
            "errcode": errcode,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size": p.stat().st_size,
        })
    return {"cases": items[:limit], "total": len(items)}
