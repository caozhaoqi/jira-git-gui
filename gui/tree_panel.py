"""文件树面板：懒加载的 QTreeWidget。

职责（纯视图）：
- requestRoot         : 请求根目录条目
- requestChildren     : 展开目录时请求其子项
- fileActivated       : 点击文件节点时请求正文
数据获取由 MainWindow 通过 Worker 完成，再把结果回调到这里。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QLabel, QApplication, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


# 文件类型配色（浅色 / 深色），用于文件名着色，便于快速区分。
_CATEGORY_COLORS = {
    "light": {
        "code": "#2563eb", "config": "#d97706", "docs": "#16a34a",
        "data": "#7c3aed", "other": None,
    },
    "dark": {
        "code": "#4f8cff", "config": "#fbbf24", "docs": "#4ade80",
        "data": "#c084fc", "other": None,
    },
}

_CODE_EXT = {"py", "js", "jsx", "ts", "tsx", "java", "go", "c", "cpp", "h", "hpp",
             "rs", "sh", "php", "rb", "kt", "scala", "lua", "r", "pl", "m", "css"}
_CONFIG_EXT = {"json", "yaml", "yml", "toml", "ini", "env", "xml", "conf", "cfg",
               "properties", "lock", "gitignore"}
_DOCS_EXT = {"md", "txt", "rst", "doc", "docx", "pdf", "rtf"}
_DATA_EXT = {"csv", "tsv", "sql", "xlsx", "parquet", "db", "sqlite", "xls"}


class TreePanel(QWidget):
    requestRoot = pyqtSignal()
    # 只传 path（稳定字符串），绝不把 QTreeWidgetItem 跨异步边界传递：
    # 节点可能在请求未返回时被 tree.clear() 销毁，闭包持有已销毁对象会抛
    # "wrapped C/C++ object of type QTreeWidgetItem has been deleted"。
    requestChildren = pyqtSignal(str)  # path
    fileActivated = pyqtSignal(str)    # path

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        title = QLabel("文件浏览器")
        title.setObjectName("section-title")
        layout.addWidget(title)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称", "大小", "选择"])
        self.tree.setColumnWidth(0, 280)
        self.tree.setColumnWidth(1, 80)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(False)
        self.tree.setMinimumHeight(200)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemClicked.connect(self._on_clicked)
        layout.addWidget(self.tree)

    # ----------------------------------------------------------- 对外接口
    def clear(self) -> None:
        self.tree.clear()

    def find_item_by_path(self, path):
        """按 path 在整棵树中查找节点，返回活的 QTreeWidgetItem；找不到返回 None。

        用于在异步回调（目录子项加载完成后）里「重新解析」节点引用，而非持有
        一个可能在请求期间被 tree.clear() 销毁的 QTreeWidgetItem。
        """

        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                d = c.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(d, dict) and d.get("path") == path:
                    return c
                found = walk(c)
                if found is not None:
                    return found
            return None

        return walk(self.tree.invisibleRootItem())

    def set_root_entries(self, entries) -> None:
        self.tree.clear()
        for e in entries:
            self.tree.addTopLevelItem(self._make_item(e))

    def set_children(self, parent_item, entries) -> None:
        parent_item.takeChildren()
        d = parent_item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(d, dict):
            d["loaded"] = True
            # 注意：PyQt6 的 item.data() 返回 UserRole 对象的「副本」，
            # 直接 d["loaded"]=True 不会写回，必须再 setData 一次，否则 loaded 标记永不生效，
            # 导致每次展开都重新发起请求（既浪费，又会放大异步竞态）。
            parent_item.setData(0, Qt.ItemDataRole.UserRole, d)
        if not entries:
            parent_item.setExpanded(False)
            return
        for e in entries:
            parent_item.addChild(self._make_item(e))

    def collect_checked(self) -> list:
        """收集所有被勾选的【文件】路径。"""
        paths = []
        root = self.tree.invisibleRootItem()

        def walk(item):
            for i in range(item.childCount()):
                c = item.child(i)
                d = c.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(d, dict):
                    if d.get("type") == "file":
                        if c.checkState(2) == Qt.CheckState.Checked:
                            paths.append(d["path"])
                    walk(c)

        walk(root)
        return paths

    # ----------------------------------------------------------- 内部
    def _make_item(self, entry):
        size = "" if entry.size is None else self._fmt_size(entry.size)
        it = QTreeWidgetItem([entry.name, size, ""])
        it.setData(0, Qt.ItemDataRole.UserRole,
                   {"path": entry.path, "type": entry.type, "loaded": False})
        # 图标：目录 / 文件用系统风格图标，跨平台一致
        style = QApplication.style()
        if style is not None:
            if entry.type == "dir":
                icon = style.standardIcon(QStyle.StandardPixmap.SP_DirClosedIcon)
            else:
                icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
            it.setIcon(0, icon)
        # 文件名按类型着色（主题感知）
        color = self._color_for(self._category(entry.name))
        if color is not None:
            it.setForeground(0, color)
        if entry.type == "dir":
            # 放一个占位子项，使目录显示展开箭头（懒加载）
            it.addChild(QTreeWidgetItem(["（加载中…", "", ""]))
        else:
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(2, Qt.CheckState.Unchecked)
        return it

    @staticmethod
    def _category(name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in _CODE_EXT:
            return "code"
        if ext in _CONFIG_EXT:
            return "config"
        if ext in _DOCS_EXT:
            return "docs"
        if ext in _DATA_EXT:
            return "data"
        return "other"

    @staticmethod
    def _color_for(category: str):
        app = QApplication.instance()
        theme = (app.property("theme") if app else None) or "light"
        hexv = _CATEGORY_COLORS.get(theme, _CATEGORY_COLORS["light"]).get(category)
        return QColor(hexv) if hexv else None

    def _on_expanded(self, item):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(d, dict) or d.get("type") != "dir":
            return
        if d.get("loaded"):
            return
        item.takeChildren()
        item.addChild(QTreeWidgetItem(["（加载中…", "", ""]))
        self.requestChildren.emit(d["path"])

    def _on_clicked(self, item, column):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(d, dict) and d.get("type") == "file":
            self.fileActivated.emit(d["path"])

    @staticmethod
    def _fmt_size(n) -> str:
        if n is None:
            return ""
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} K"
        return f"{n / 1024 / 1024:.1f} M"
