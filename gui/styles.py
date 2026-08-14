# -*- coding: utf-8 -*-
"""全局 QSS 样式表 —— 统一视觉风格。

设计参考：VS Code / GitHub Desktop / Sourcetree
配色方案：浅灰底 + 蓝色强调色，扁平化设计
"""
from PyQt6.QtGui import QFont, QPalette, QColor
from PyQt6.QtWidgets import QApplication

# —— 配色常量 ——
COLOR_BG          = "#f5f6f8"   # 主背景
COLOR_SURFACE     = "#ffffff"   # 卡片/面板背景
COLOR_BORDER      = "#dfe1e6"   # 分隔线/边框
COLOR_TEXT        = "#172b4d"   # 主文字
COLOR_TEXT_SEC    = "#6b7280"   # 次要文字
COLOR_TEXT_HINT   = "#9ca3af"   # 占位符
COLOR_PRIMARY     = "#2563eb"   # 强调色（按钮、选中）
COLOR_PRIMARY_HOV = "#1d4ed8"   # 强调色悬停
COLOR_PRIMARY_BG  = "#eff6ff"   # 强调色浅底
COLOR_DANGER      = "#dc2626"   # 危险/错误
COLOR_SUCCESS     = "#16a34a"   # 成功
COLOR_WARNING     = "#d97706"   # 警告

QSS = f"""
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


def apply_global_style(app: QApplication) -> None:
    """应用全局 QSS 样式并设置默认字体。"""
    app.setStyleSheet(QSS)

    # 默认字体
    font = QFont()
    font.setFamilies(["SF Pro Text", "Helvetica Neue",
                      "Segoe UI", "Microsoft YaHei", "PingFang SC", "sans-serif"])
    font.setPointSize(13)
    app.setFont(font)
