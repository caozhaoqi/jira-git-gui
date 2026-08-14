"""代码预览面板。"""
import json
import re

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from .highlighter import CodeHighlighter

# 超过该字符数 / 行数则截断预览，避免把超大文件塞入编辑器导致 UI 卡死。
_MAX_PREVIEW_CHARS = 1_500_000
_MAX_PREVIEW_LINES = 8000

# 按扩展名映射高亮模式（非代码/JSON 的文件不强制高亮，避免误染）。
_CODE_LIKE = {
    "py", "js", "jsx", "ts", "tsx", "java", "go", "c", "cpp", "h", "hpp", "rs",
    "sh", "php", "rb", "kt", "scala", "lua", "r", "pl", "m", "css", "scss", "less",
    "html", "vue", "yaml", "yml", "toml", "ini", "conf", "cfg", "sql", "gradle",
    "bash", "zsh",
}


class PreviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.title = QLabel("预览")
        self.title.setObjectName("section-title")
        layout.addWidget(self.title)

        # 大文件提示条（默认隐藏）
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet(
            "color: #92400e; background: #fef3c7; border: 1px solid #fde68a; "
            "border-radius: 6px; padding: 4px 8px; font-size: 12px;")
        self.warn.setVisible(False)
        layout.addWidget(self.warn)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        # 注意：QFont 第一个参数是「单个字体族名」，不能传 CSS 逗号列表，
        # 否则会解析成一个不存在的字体族并在渲染时崩溃。正确做法用 setFamilies 列表回退。
        mono = QFont()
        mono.setFamilies(["Menlo", "Monaco", "Courier New", "monospace"])
        mono.setPointSize(12)
        self.text.setFont(mono)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.text)

        # 语法高亮器（零依赖，主题感知）
        self._highlighter = CodeHighlighter(self.text.document())

    def set_content(self, text: str, path: str = "") -> None:
        formatted, is_json = self._try_format_json(path, text or "")
        # 大文件保护：截断到前若干行，仅预览，避免界面卡死
        lines = formatted.split("\n")
        nlines = len(lines)
        nchars = len(formatted)
        if nchars > _MAX_PREVIEW_CHARS or nlines > _MAX_PREVIEW_LINES:
            shown = "\n".join(lines[:_MAX_PREVIEW_LINES])
            self.warn.setText(
                f"⚠ 文件较大（约 {nchars // 1024} KB / {nlines} 行），仅预览前 "
                f"{min(nlines, _MAX_PREVIEW_LINES)} 行以避免界面卡顿。"
                f"请用文件树勾选后「下载选中」获取完整内容。")
            self.warn.setVisible(True)
            self._render(shown, path, is_json)
            title = f"预览 · {path}（已截断）" if path else "预览（已截断）"
            self.title.setText(title)
            return

        self.warn.setVisible(False)
        self._render(formatted, path, is_json)
        if is_json:
            self.title.setText(f"预览 · {path}  (JSON 已格式化)" if path else "预览 (JSON 已格式化)")
        else:
            self.title.setText(f"预览 · {path}" if path else "预览")

    def _render(self, content: str, path: str, is_json: bool) -> None:
        """设置文本并应用对应高亮模式（失败则回退为纯文本）。"""
        mode = "json" if is_json else ("code" if self._is_code_like(path) else None)
        try:
            if mode is None:
                # 非代码文件：关闭高亮，纯文本展示
                self._highlighter.setDocument(None)
            else:
                self._highlighter.mode = mode
                if self._highlighter.document() is not self.text.document():
                    self._highlighter.setDocument(self.text.document())
            self.text.setPlainText(content)
        except Exception:
            # 任何异常都回退到纯文本，绝不 crash
            try:
                self._highlighter.setDocument(None)
                self.text.setPlainText(content)
            except Exception:
                pass

    @staticmethod
    def _is_code_like(path: str) -> bool:
        if not path:
            return False
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return ext in _CODE_LIKE

    @staticmethod
    def _try_format_json(path: str, content: str) -> tuple[str, bool]:
        """如果是 JSON 文件或内容像 JSON，尝试格式化。返回 (formatted, is_json)。"""
        if not content:
            return content, False
        is_json_file = bool(re.search(r"\.json$", path or "", re.IGNORECASE))
        looks_like_json = content.strip().startswith(("{", "["))
        if is_json_file or looks_like_json:
            try:
                parsed = json.loads(content)
                return json.dumps(parsed, indent=2, ensure_ascii=False), True
            except (json.JSONDecodeError, ValueError):
                pass
        return content, False

    def set_loading(self, path: str = "") -> None:
        self.warn.setVisible(False)
        self.title.setText(f"加载中 · {path}" if path else "加载中…")
        self.text.setPlainText("")

    def set_error(self, msg: str) -> None:
        self.warn.setVisible(False)
        self.title.setText("错误")
        self.text.setPlainText(msg)

    @staticmethod
    def size_fmt(n) -> str:
        if n is None:
            return ""
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} K"
        return f"{n / 1024 / 1024:.1f} M"
