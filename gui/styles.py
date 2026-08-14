# -*- coding: utf-8 -*-
"""全局 QSS 样式表 —— 统一视觉风格，支持浅色 / 深色双主题。

设计参考：VS Code / GitHub Desktop / Sourcetree
浅色：灰底 + 蓝色强调色，扁平化；深色：VS Code Dark 风格。
"""
from PyQt6.QtGui import QFont, QPalette, QColor
from PyQt6.QtWidgets import QApplication

# —— 配色方案（浅色 = 原版；深色 = VS Code Dark 风格）——
PALETTES = {
    "light": {
        "COLOR_BG": "#f5f6f8",
        "COLOR_SURFACE": "#ffffff",
        "COLOR_BORDER": "#dfe1e6",
        "COLOR_TEXT": "#172b4d",
        "COLOR_TEXT_SEC": "#6b7280",
        "COLOR_TEXT_HINT": "#9ca3af",
        "COLOR_PRIMARY": "#2563eb",
        "COLOR_PRIMARY_HOV": "#1d4ed8",
        "COLOR_PRIMARY_BG": "#eff6ff",
        "COLOR_DANGER": "#dc2626",
        "COLOR_SUCCESS": "#16a34a",
        "COLOR_WARNING": "#d97706",
    },
    "dark": {
        "COLOR_BG": "#1e1e2e",
        "COLOR_SURFACE": "#262637",
        "COLOR_BORDER": "#3a3a4d",
        "COLOR_TEXT": "#e4e4e7",
        "COLOR_TEXT_SEC": "#a1a1aa",
        "COLOR_TEXT_HINT": "#71717a",
        "COLOR_PRIMARY": "#4f8cff",
        "COLOR_PRIMARY_HOV": "#3b7ce0",
        "COLOR_PRIMARY_BG": "#16314f",
        "COLOR_DANGER": "#f87171",
        "COLOR_SUCCESS": "#4ade80",
        "COLOR_WARNING": "#fbbf24",
    },
}


def _build_qss(p: dict) -> str:
    COLOR_BG = p["COLOR_BG"]
    COLOR_SURFACE = p["COLOR_SURFACE"]
    COLOR_BORDER = p["COLOR_BORDER"]
    COLOR_TEXT = p["COLOR_TEXT"]
    COLOR_TEXT_SEC = p["COLOR_TEXT_SEC"]
    COLOR_TEXT_HINT = p["COLOR_TEXT_HINT"]
    COLOR_PRIMARY = p["COLOR_PRIMARY"]
    COLOR_PRIMARY_HOV = p["COLOR_PRIMARY_HOV"]
    COLOR_PRIMARY_BG = p["COLOR_PRIMARY_BG"]
    COLOR_DANGER = p["COLOR_DANGER"]
    COLOR_SUCCESS = p["COLOR_SUCCESS"]
    COLOR_WARNING = p["COLOR_WARNING"]

    return f"""
/* ===== 全局 ===== */
QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-size: 13px;
}}

QMainWindow {{
    background-color: {COLOR_BG};
}}

/* ===== 工具栏 ===== */
QToolBar {{
    background-color: {COLOR_SURFACE};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 4px 8px;
    spacing: 4px;
}}
QToolBar::separator {{
    width: 1px;
    background-color: {COLOR_BORDER};
    margin: 4px 6px;
}}
QToolBar QLabel {{
    color: {COLOR_TEXT_SEC};
    font-size: 12px;
    padding: 0 2px;
}}

/* ===== 按钮 ===== */
QPushButton {{
    background-color: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 5px 14px;
    color: {COLOR_TEXT};
    font-size: 13px;
}}
QPushButton:hover {{
    border-color: {COLOR_PRIMARY};
    background-color: {COLOR_PRIMARY_BG};
}}
QPushButton:pressed {{
    background-color: {COLOR_BORDER};
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_HINT};
    border-color: {COLOR_BORDER};
    background-color: {COLOR_BG};
}}
QPushButton#primary {{
    background-color: {COLOR_PRIMARY};
    border-color: {COLOR_PRIMARY};
    color: #ffffff;
    font-weight: 500;
}}
QPushButton#primary:hover {{
    background-color: {COLOR_PRIMARY_HOV};
    border-color: {COLOR_PRIMARY_HOV};
}}
QPushButton#primary:pressed {{
    background-color: {COLOR_PRIMARY_HOV};
}}

/* ===== 输入框 ===== */
QLineEdit, QSpinBox, QComboBox {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    background-color: {COLOR_SURFACE};
    selection-background-color: {COLOR_PRIMARY};
    selection-color: #ffffff;
    min-height: 20px;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {COLOR_PRIMARY};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT_HINT};
}}

/* QSpinBox 去掉箭头按钮区域多余间距 */
QSpinBox::up-button, QSpinBox::down-button {{
    width: 18px;
    border: none;
    background: transparent;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {COLOR_PRIMARY_BG};
    border-radius: 3px;
}}

/* ComboBox 下拉 */
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    width: 10px;
    height: 10px;
}}
QComboBox QAbstractItemView {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    background-color: {COLOR_SURFACE};
    selection-background-color: {COLOR_PRIMARY};
    selection-color: #ffffff;
    outline: none;
    padding: 2px;
}}

/* ===== 文本编辑区 ===== */
QPlainTextEdit, QTextEdit {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    background-color: {COLOR_SURFACE};
    selection-background-color: {COLOR_PRIMARY};
    selection-color: #ffffff;
}}
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {COLOR_PRIMARY};
}}

/* ===== 列表 ===== */
QListWidget {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    background-color: {COLOR_SURFACE};
    outline: none;
    padding: 2px;
}}
QListWidget::item {{
    border-radius: 4px;
    padding: 3px 6px;
}}
QListWidget::item:hover {{
    background-color: {COLOR_PRIMARY_BG};
}}
QListWidget::item:selected {{
    background-color: {COLOR_PRIMARY};
    color: #ffffff;
}}

/* ===== 树 ===== */
QTreeWidget {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    background-color: {COLOR_SURFACE};
    outline: none;
    padding: 2px;
}}
QTreeWidget::item {{
    padding: 2px 0;
    min-height: 20px;
}}
QTreeWidget::item:hover {{
    background-color: {COLOR_PRIMARY_BG};
}}
QTreeWidget::item:selected {{
    background-color: {COLOR_PRIMARY};
    color: #ffffff;
}}
QTreeWidget::branch:has-children:!has-siblings:closed,
QTreeView::branch:has-children:!has-siblings:closed {{
    image: none;
    border-image: none;
}}

/* ===== 分隔器手柄 ===== */
QSplitter::handle {{
    background-color: {COLOR_BORDER};
}}
QSplitter::handle:horizontal {{
    width: 2px;
    margin: 0 1px;
}}
QSplitter::handle:vertical {{
    height: 2px;
    margin: 1px 0;
}}

/* ===== 标签页 ===== */
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    background-color: {COLOR_SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background-color: transparent;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 16px;
    margin-right: 2px;
    color: {COLOR_TEXT_SEC};
}}
QTabBar::tab:hover {{
    background-color: {COLOR_PRIMARY_BG};
    color: {COLOR_TEXT};
}}
QTabBar::tab:selected {{
    background-color: {COLOR_SURFACE};
    border-color: {COLOR_BORDER};
    color: {COLOR_PRIMARY};
    font-weight: 500;
}}

/* ===== 分组框 ===== */
QGroupBox {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 10px;
    background-color: {COLOR_SURFACE};
    font-weight: 500;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: {COLOR_TEXT_SEC};
}}

/* ===== 进度条 ===== */
QProgressBar {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    background-color: {COLOR_BG};
    text-align: center;
    font-size: 11px;
    color: {COLOR_TEXT_SEC};
    min-height: 18px;
}}
QProgressBar::chunk {{
    background-color: {COLOR_PRIMARY};
    border-radius: 3px;
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    border: none;
    background-color: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {COLOR_BORDER};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {COLOR_TEXT_HINT};
}}
QScrollBar:horizontal {{
    border: none;
    background-color: transparent;
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: {COLOR_BORDER};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {COLOR_TEXT_HINT};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

/* ===== 状态栏 ===== */
QStatusBar {{
    background-color: {COLOR_SURFACE};
    border-top: 1px solid {COLOR_BORDER};
    color: {COLOR_TEXT_SEC};
    font-size: 12px;
    padding: 2px 8px;
}}

/* ===== 对话框 ===== */
QDialog {{
    background-color: {COLOR_BG};
}}

/* ===== 工具提示 ===== */
QToolTip {{
    background-color: {COLOR_TEXT};
    color: {COLOR_SURFACE};
    border: none;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}}

/* ===== 表单标签 ===== */
QLabel#section-title {{
    font-size: 14px;
    font-weight: 600;
    color: {COLOR_TEXT};
    padding: 2px 0;
}}
QLabel#hint {{
    color: {COLOR_TEXT_SEC};
    font-size: 12px;
}}
"""


QSS_LIGHT = _build_qss(PALETTES["light"])
QSS_DARK = _build_qss(PALETTES["dark"])
THEMES = {"light": QSS_LIGHT, "dark": QSS_DARK}


def apply_global_style(app: QApplication, theme: str = "light") -> None:
    """应用全局 QSS 样式并设置默认字体与当前主题标记。

    ``theme`` 写入 ``app`` 的 dynamic property（"theme"），供树面板配色、
    代码高亮器等读取，实现主题感知。
    """
    app.setStyleSheet(THEMES.get(theme, QSS_LIGHT))
    app.setProperty("theme", theme)

    # 默认字体
    font = QFont()
    font.setFamilies(["SF Pro Text", "Helvetica Neue",
                      "Segoe UI", "Microsoft YaHei", "PingFang SC", "sans-serif"])
    font.setPointSize(13)
    app.setFont(font)
