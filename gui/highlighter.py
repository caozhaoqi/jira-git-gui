# -*- coding: utf-8 -*-
"""零依赖语法高亮器（基于 QSyntaxHighlighter）。

支持通用代码（注释 / 字符串 / 数字 / 关键字）与 JSON（键 / 值 / 数字 / 布尔）。
颜色随应用主题（QApplication 的 "theme" 属性）自适应，浅色 / 深色两套配色。
不引入任何第三方依赖，离线可用。
"""
from PyQt6.QtCore import QRegularExpression
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import QApplication

# 主题配色：comment / string / number / keyword / json_key
_PALETTES = {
    "light": {
        "comment": "#6a9955",
        "string": "#a31515",
        "number": "#098658",
        "keyword": "#0000ff",
        "json_key": "#001080",
    },
    "dark": {
        "comment": "#6a9955",
        "string": "#ce9178",
        "number": "#b5cea8",
        "keyword": "#569cd6",
        "json_key": "#9cdcfe",
    },
}

_KEYWORDS = (
    r"if|else|elif|for|while|do|def|class|function|return|import|from|as|var|let|"
    r"const|public|private|protected|static|new|await|async|try|catch|except|finally|"
    r"throw|raise|switch|case|break|continue|null|true|false|None|True|False|self|this|"
    r"package|interface|struct|enum|type|func|go|select|from|where|and|or|not|in|of|with|"
    r"yield|lambda|print|echo|using|namespace|extends|implements|abstract|final|void|"
    r"int|float|string|bool|double|char|long|unsigned|signed|then|end|begin|foreach|"
    r"public|protected|virtual|override|get|set|fn|mut|pub|impl|trait|match"
)


def _theme_palette() -> dict:
    app = QApplication.instance()
    theme = (app.property("theme") if app else None) or "light"
    return _PALETTES.get(theme, _PALETTES["light"])


class CodeHighlighter(QSyntaxHighlighter):
    def __init__(self, parent: QTextDocument | None = None, mode: str = "code"):
        super().__init__(parent)
        self.mode = mode  # "code" | "json"
        pal = _theme_palette()
        self._comment_fmt = self._fmt(pal["comment"])
        self._string_fmt = self._fmt(pal["string"])
        self._number_fmt = self._fmt(pal["number"])
        self._keyword_fmt = self._fmt(pal["keyword"])
        self._json_key_fmt = self._fmt(pal["json_key"])

        # 单行规则（顺序：先 number/keyword，再 string，再行注释 —— 让字符串覆盖注释/关键字，
        # 避免字符串内的 // 被误判为注释；块注释在后处理时整体覆盖）。
        self._rules = [
            (QRegularExpression(r"\b\d+(?:\.\d+)?\b"), self._number_fmt),
            (QRegularExpression(r"\b(?:" + _KEYWORDS + r")\b"), self._keyword_fmt),
            (QRegularExpression(r"(?://|#|--).*$"), self._comment_fmt),
            (QRegularExpression(r"\"(?:\\.|[^\"\\])*\""), self._string_fmt),
            (QRegularExpression(r"'(?:\\.|[^'\\])*'"), self._string_fmt),
            (QRegularExpression(r"`(?:\\.|[^`\\])*`"), self._string_fmt),
        ]
        # JSON 规则：数字 / 布尔空 / 字符串值 / JSON 键（在冒号前的字符串）
        self._json_rules = [
            (QRegularExpression(r"\b\d+(?:\.\d+)?\b"), self._number_fmt),
            (QRegularExpression(r"\b(?:true|false|null|True|False|None)\b"), self._keyword_fmt),
            (QRegularExpression(r"\"(?:\\.|[^\"\\])*\""), self._string_fmt),
            (QRegularExpression(r"\"(?:\\.|[^\"\\])*\"(?=\s*:)"), self._json_key_fmt),
        ]
        self._comment_start = QRegularExpression(r"/\*")
        self._comment_end = QRegularExpression(r"\*/")

    @staticmethod
    def _fmt(color: str) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        return f

    def set_theme(self, theme: str) -> None:
        """切换主题并立即重绘。"""
        pal = _PALETTES.get(theme, _PALETTES["light"])
        self._comment_fmt = self._fmt(pal["comment"])
        self._string_fmt = self._fmt(pal["string"])
        self._number_fmt = self._fmt(pal["number"])
        self._keyword_fmt = self._fmt(pal["keyword"])
        self._json_key_fmt = self._fmt(pal["json_key"])
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if self.mode == "json":
            for pat, fmt in self._json_rules:
                self._apply(text, pat, fmt)
            return
        for pat, fmt in self._rules:
            self._apply(text, pat, fmt)
        self._highlight_block_comments(text)

    def _apply(self, text: str, pattern: QRegularExpression, fmt: QTextCharFormat) -> None:
        it = pattern.globalMatch(text)
        while it.hasNext():
            m = it.next()
            self.setFormat(m.capturedStart(), m.capturedLength(), fmt)

    def _highlight_block_comments(self, text: str) -> None:
        # 1 = 处于块注释中
        state = 1 if self.previousBlockState() == 1 else 0
        i = 0
        if state == 1:
            em = self._comment_end.match(text)
            if em.hasMatch():
                end = em.capturedEnd()
                self.setFormat(0, end, self._comment_fmt)
                i = end
                state = 0
            else:
                self.setFormat(0, len(text), self._comment_fmt)
                self.setCurrentBlockState(1)
                return
        n = len(text)
        while i < n:
            sm = self._comment_start.match(text, i)
            if not sm.hasMatch():
                break
            s = sm.capturedStart()
            em = self._comment_end.match(text, s + 1)
            if em.hasMatch():
                e = em.capturedEnd()
                self.setFormat(s, e - s, self._comment_fmt)
                i = e
            else:
                self.setFormat(s, n - s, self._comment_fmt)
                self.setCurrentBlockState(1)
                i = n
        if self.currentBlockState() != 1:
            self.setCurrentBlockState(0)
