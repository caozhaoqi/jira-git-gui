# -*- coding: utf-8 -*-
r"""从参考源码 errors.py 提取错误码定义，生成 errDict 扩充候选。

为什么单独生成、不直接合并进 errdict.json
------------------------------------------
参考 errors.py 里 215/216 个错误码的 errmsg 都是 i18n key（形如
``system_errors_msg_00000030``），真实中文文案存在数据库的
``HCMErrorsI18NData`` 表中（见 util.py:2353 hcm_errors_i18n），源码里拿不到。

因此本工具**不编造中文文案**，而是：
1. 从 errors.py AST 提取权威事实：errcode、HTTP status、常量名、i18n key；
2. 按常量名的英文词表**推断**中文含义与分类，并标记 ``verified=false``；
3. 输出独立的 ``errdict_inferred.json``，与人工维护的 errdict.json 物理隔离。

消费侧（cf_diagnose.py）按「已校验优先于推断」合并，推断条目只作兜底提示，
不会覆盖人工维护的释义。

用法
----
    python tools/cf_errdict_expand.py                 # 生成到默认路径
    python tools/cf_errdict_expand.py --source X      # 指定 errors.py
    python tools/cf_errdict_expand.py --out Y         # 指定输出
    python tools/cf_errdict_expand.py --stats         # 只看统计不写文件
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SRC = Path("/Users/caozhaoqi/Downloads/other/hcm-cloud-vue/hcm-core/errors.py")
_DEFAULT_OUT = (
    _PROJECT_ROOT / "store" / "downloads" / "895" / "docs" / "metadata"
    / "reference" / "errdict_inferred.json"
)
_MAIN_DICT = (
    _PROJECT_ROOT / "store" / "downloads" / "895" / "docs" / "metadata"
    / "reference" / "errdict.json"
)

# --------------------------------------------------------------------------- #
#  英文 token → 中文（按最长匹配优先，用于把常量名翻译成可读含义）
# --------------------------------------------------------------------------- #
_TOKEN_CN = {
    "AUTH": "认证", "PASSWORD": "密码", "VALID_CODE": "验证码", "IMG_CODE": "图片验证码",
    "PHONE_CODE": "短信验证码", "TOKEN": "令牌", "LOGIN": "登录", "LOGOUT": "登出",
    "SSO": "单点登录", "SAML": "SAML 认证", "BINDING": "绑定", "UNBIND": "解绑",
    "USER": "用户", "EMPLOYEE": "员工", "EMP": "员工", "COMPANY": "公司",
    "DEPT": "部门", "ORG": "组织", "POSITION": "岗位", "JOB": "任职",
    "PERMISSION": "权限", "ROLE": "角色", "ACCESS": "访问", "FORBIDDEN": "禁止",
    "NO_PERMISSION": "无权限", "NOT_ALLOW": "不允许", "RESTRICT": "受限",
    "WORKFLOW": "工作流", "ACTION": "动作", "TASK": "任务", "REPORT": "报表",
    "FIELD": "字段", "OBJECT": "对象", "META": "元数据", "MODEL": "模型",
    "PARAMS": "参数", "PARAM": "参数", "DATA": "数据", "CONFIG": "配置",
    "NOT_EXIST": "不存在", "NOT_FOUND": "未找到", "IS_EMPTY": "为空", "EMPTY": "为空",
    "EXIST": "已存在", "ALREADY": "已", "DUPLICATE": "重复", "REPEAT": "重复",
    "INVALID": "无效", "NOT_VALID": "不合法", "ERROR": "错误", "FAILED": "失败",
    "FAIL": "失败", "EXPIRE": "已过期", "EXPIRED": "已过期", "TIMEOUT": "超时",
    "MAX": "超过上限", "LIMIT": "受限", "UPLOAD": "上传", "DOWNLOAD": "下载",
    "FILE": "文件", "TEMPLATE": "模板", "PRINT": "打印", "EXPORT": "导出",
    "IMPORT": "导入", "SEND": "发送", "MOBILE": "短信", "MSG": "消息",
    "SMS": "短信", "MAIL": "邮件", "NOTIFY": "通知", "SYNC": "同步", "SYN": "同步",
    "MODIFY": "修改", "DELETE": "删除", "ADD": "新增", "CREATE": "创建",
    "UPDATE": "更新", "SAVE": "保存", "EDIT": "编辑", "VIEW": "查看",
    "RULE": "规则", "CHECK": "校验", "VERIFY": "验证", "SIGNATURE": "签名",
    "SOURCE": "来源", "THIRD": "第三方", "OPEN": "开放", "API": "接口",
    "SCRIPT": "脚本", "REDIS": "缓存", "SYSTEM": "系统", "SYS": "系统",
    "SERVICE": "服务", "REMOTE": "远程", "DOMAIN": "域名", "BROWSER": "浏览器",
    "SECURITY": "安全", "BLACK_LIST": "黑名单", "WHITE_LIST": "白名单",
    "PRIMARY": "主", "PART": "兼职", "PARTTIME": "兼职", "FUTURE": "未来",
    "PRE": "预", "TEMP": "临时", "MANAGER": "负责人", "PARENT": "上级",
    "NAME": "名称", "NUMBER": "编号", "COUNT": "数量", "TARGET": "目标",
    "EXECUTE": "执行", "FETCH": "获取", "RESULT": "结果", "STATUS": "状态",
    "RECRUIT": "招聘", "RESUME": "简历", "ATTEND": "考勤", "GEO": "地理位置",
    "DICTIONARY": "字典", "FLEX": "弹性", "CHANNEL": "渠道", "ARTICLE": "文章",
    "ACCOUNT": "账号", "OFFLINE": "离线", "JOIN": "关联", "MAP": "映射",
    "LANXIN": "蓝信", "WE_CHAT": "微信", "WE": "企微", "APP": "应用",
    "OUTER": "外部", "SHORT": "短", "CURRENT": "当前", "NEED": "需要",
    "HAVE": "存在", "HAS": "存在", "NOT": "未", "NO": "无", "IS": "",
    "TO": "", "THE": "", "OF": "", "AND": "",
    "SUB": "下级", "INFO": "信息", "TYPE": "类型", "KEY": "键值",
    "CODE": "编码", "TIME": "时间", "DATE": "日期", "VALUE": "值",
    # 组合词：翻译时按 3→2→1 词最长匹配，故组合会优先于上面的单词命中。
    # HAVE_NO_PERMISSION 必须在 HAVE=存在 之前生效，否则会译成病句「存在无权限」。
    "HAVE_NO_PERMISSION": "无权限", "NOT_PERMISSION": "无权限",
    "NOT_NULL": "不能为空", "IS_NULL": "为空", "ALREADY_EXIST": "已存在",
}

# 按常量名前缀归类，用于给 AI 提示「该往哪个方向查」
_CATEGORY_RULES = [
    (("AUTH", "LOGIN", "TOKEN", "SSO", "SAML", "PASSWORD", "VALID_CODE", "IMG_CODE"),
     "auth", "认证/登录态问题：优先查 Token 是否过期（默认 2 小时）"),
    (("PERMISSION", "ROLE", "FORBIDDEN", "NO_PERMISSION", "ACCESS"),
     "permission", "权限问题：查账号对该模型/字段的角色权限"),
    (("FIELD", "META", "MODEL", "OBJECT", "FLEX", "DICTIONARY"),
     "metadata", "元数据/字段问题：查字段名、模型版本与 schema 约束"),
    (("WORKFLOW", "ACTION", "TASK"),
     "workflow", "流程/动作问题：查流程配置、节点条件与动作目标"),
    (("PARAMS", "PARAM", "DATA", "VALID", "INVALID", "CHECK", "RULE"),
     "validation", "参数校验问题：按接口 schema 检查类型/必填/枚举"),
    (("NOT_EXIST", "NOT_FOUND", "IS_EMPTY", "EXIST", "DUPLICATE", "REPEAT",
      "EMPLOYEE", "EMP", "USER", "DEPT", "ORG", "POSITION", "JOB", "COMPANY"),
     "resource", "资源问题：查对象/记录是否存在、是否被删除或落在权限范围外"),
    (("FILE", "UPLOAD", "DOWNLOAD", "TEMPLATE", "PRINT", "EXPORT", "IMPORT"),
     "file", "文件/模板问题：查格式、大小、扩展名与模板配置"),
    (("MOBILE", "SMS", "SEND", "MSG", "MAIL", "NOTIFY"),
     "notify", "通知发送问题：查通道配置、模板变量与接收人"),
    (("SYNC", "SYN", "REMOTE", "THIRD", "OPEN_API", "EXECUTE"),
     "integration", "外部集成问题：查第三方接口凭据、网络与返回体"),
    (("REDIS", "SYSTEM", "SYS", "SERVICE"),
     "infrastructure", "基础设施问题：查缓存/服务可用性与集群状态"),
]
_DEFAULT_CATEGORY = ("other", "按常量名与 HTTP 状态码判断排查方向")


def _tokenize(name: str) -> list[str]:
    """把 ERROR_NAME 切成 ['ERROR', 'NAME']。"""
    return [t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t]


def _translate_name(name: str) -> str:
    """按最长匹配把常量名逐词翻译成中文短语。"""
    tokens = _tokenize(name)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        # 优先尝试 3 词、2 词组合，命中则跳过相应词数
        for span in (3, 2, 1):
            if i + span > len(tokens):
                continue
            key = "_".join(tokens[i:i + span])
            if key in _TOKEN_CN:
                cn = _TOKEN_CN[key]
                if cn:
                    out.append(cn)
                i += span
                break
        else:
            out.append(tokens[i].lower())
            i += 1
    # 去重相邻重复词（如「错误 错误」）
    dedup: list[str] = []
    for w in out:
        if not dedup or dedup[-1] != w:
            dedup.append(w)
    return "".join(dedup) if all(len(w) <= 3 for w in dedup) else " ".join(dedup)


def _categorize(name: str) -> tuple[str, str]:
    up = name.upper()
    for keys, cat, hint in _CATEGORY_RULES:
        if any(k in up for k in keys):
            return cat, hint
    return _DEFAULT_CATEGORY


def _status_hint(status: int) -> str:
    return {
        400: "请求参数问题（客户端侧）",
        401: "认证失败：Token 缺失/无效/过期",
        403: "鉴权通过但无权限",
        404: "资源不存在",
        500: "服务端内部异常",
        501: "服务端未实现/业务不满足前置条件",
    }.get(status, "")


def extract_errors(src: Path) -> list[dict]:
    """AST 解析 errors.py，返回去重后的错误码定义列表。"""
    tree = ast.parse(src.read_text(encoding="utf-8"))
    found: dict[int, dict] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        if not (isinstance(fn, ast.Name) and fn.id == "AppException"):
            continue
        args = node.value.args
        if len(args) < 3:
            continue
        status = getattr(args[0], "value", None)
        code = getattr(args[1], "value", None)
        msg = getattr(args[2], "value", None)
        if not isinstance(code, int):
            continue
        name = node.targets[0].id if isinstance(node.targets[0], ast.Name) else "UNNAMED"
        if code in found:
            continue
        found[code] = {
            "errcode": code,
            "status_code": status if isinstance(status, int) else None,
            "name": name,
            "i18n_key": msg if isinstance(msg, str) else "",
        }
    return sorted(found.values(), key=lambda r: r["errcode"])


def build_inferred(rows: list[dict], verified: dict) -> dict:
    """生成推断词典：已校验的照抄，其余按规则推断。"""
    errcodes: dict[str, dict] = {}
    inferred_count = 0
    for r in rows:
        key = str(r["errcode"])
        cat, hint = _categorize(r["name"])
        entry = {
            "name": r["name"],
            "status_code": r["status_code"],
            "category": cat,
            "hint": hint,
        }
        if key in verified:
            v = verified[key]
            entry.update({
                "meaning": v.get("meaning", ""),
                "fix": v.get("fix", ""),
                "verified": True,
                "source": "manual",
            })
        else:
            entry.update({
                "meaning": _translate_name(r["name"]),
                "verified": False,
                "source": "inferred_from_constant_name",
                "i18n_key": r["i18n_key"],
                "caveat": "含义由常量名推断，未经人工校验；真实文案存于数据库 HCMErrorsI18NData",
            })
            inferred_count += 1
        if r["status_code"]:
            entry["status_hint"] = _status_hint(r["status_code"])
        errcodes[key] = entry

    cats: dict[str, int] = {}
    for e in errcodes.values():
        cats[e["category"]] = cats.get(e["category"], 0) + 1

    return {
        "_meta": {
            "schema": "hcm-errdict-inferred/v1",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generator": "tools/cf_errdict_expand.py",
            "note": (
                "错误码定义（errcode/status_code/常量名/i18n_key）来自参考源码 errors.py，属权威事实；"
                "中文含义除 verified=true 的条目外均由常量名推断，不可当作官方文案。"
            ),
            "merge_rule": "消费侧按 verified=true 优先；推断条目仅作兜底提示，不覆盖人工释义。",
        },
        "stats": {
            "total": len(errcodes),
            "verified": sum(1 for e in errcodes.values() if e.get("verified")),
            "inferred": inferred_count,
            "by_category": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        },
        "errcodes": errcodes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从参考源码 errors.py 生成 errDict 扩充候选")
    ap.add_argument("--source", default=str(_DEFAULT_SRC), help="errors.py 路径")
    ap.add_argument("--out", default=str(_DEFAULT_OUT), help="输出 JSON 路径")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args(argv)

    src = Path(args.source)
    if not src.is_file():
        print(f"[ERR] 找不到参考源码 errors.py: {src}", file=sys.stderr)
        print("      可用 HCM_REFERENCE_ROOT 指向其它目录后重试。", file=sys.stderr)
        return 2

    verified: dict = {}
    if _MAIN_DICT.is_file():
        try:
            verified = json.loads(_MAIN_DICT.read_text(encoding="utf-8")).get("errcodes", {})
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 读取主词典失败（将全部按推断处理）: {e}", file=sys.stderr)

    rows = extract_errors(src)
    data = build_inferred(rows, verified)
    st = data["stats"]

    print(f"源码        : {src}")
    print(f"错误码总数  : {st['total']}")
    print(f"  已校验    : {st['verified']}（来自人工维护的 errdict.json）")
    print(f"  推断      : {st['inferred']}（由常量名推断，待人工确认）")
    print("分类分布    : " + ", ".join(f"{k}={v}" for k, v in st["by_category"].items()))

    if args.stats:
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入      : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
