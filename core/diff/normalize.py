# -*- coding: utf-8 -*-
"""差异规范化工具（自 ``core/diff_diff`` 拆分）。

提供：
- ``_strip_jsonc_comments``：剥离 ``//`` 与 ``/* */`` 注释（字符串内不误删）
- ``canonical_text``：对 JSON/JSONC/XML 做规范格式化，使「空白/注释差异」不计入实质改动
- ``_JSON_EXTENSIONS`` / ``_XML_EXTENSIONS``：受规范化处理的扩展名集合

注意：原单体实现中引用了未定义的 ``_json`` / ``_minidom`` / ``_name_ext``，导致
JSONC/XML 路径在调用时 NameError；此处补全依赖，使规范化真正可用。
"""
import json as _json
import xml.dom.minidom as _minidom

from .models import _log

_JSON_EXTENSIONS = (
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml",
    ".ini", ".conf", ".cfg", ".properties",
)
_XML_EXTENSIONS = (
    ".xml", ".plist", ".svg", ".wsdl", ".xsd", ".html", ".htm",
)


def _strip_jsonc_comments(text: str) -> str:
    """剥离 JSONC 的 ``//`` 行注释与 ``/* */`` 块注释（字符串内容内的注释符不误删）。"""
    out = []
    in_str = False
    quote = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                in_str = False
            i += 1
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j == -1:
                j = n
            i = j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                j = n
            else:
                j += 2
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def canonical_text(name: str, content: str) -> str:
    """对 JSON/JSONC/XML 做规范化格式化，便于空白/注释无关的差异比较。

    非受支持扩展名或解析失败时回退原文（不抛异常）。
    """
    low = name.lower()
    for ext in _JSON_EXTENSIONS:
        if low.endswith(ext):
            try:
                if ext == ".jsonc":
                    return _json.dumps(
                        _json.loads(_strip_jsonc_comments(content)),
                        indent=2, ensure_ascii=False)
                return _json.dumps(_json.loads(content), indent=2, ensure_ascii=False)
            except Exception:
                _log.warning("JSON 规范化失败，回退原文: %s", name)
                return content
    for ext in _XML_EXTENSIONS:
        if low.endswith(ext):
            try:
                dom = _minidom.parseString(content.encode("utf-8"))
                return dom.toprettyxml(indent="  ")
            except Exception:
                _log.warning("XML 规范化失败，回退原文: %s", name)
                return content
    return content
