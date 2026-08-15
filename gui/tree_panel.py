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
        # 性能：path -> 活节点索引，O(1) 查找，替代整树递归遍历（万级节点显著加速）
        self._items_by_path: dict = {}
        # 缓存系统图标，避免每个节点都向 QApplication.style() 查询
        self._dir_icon = None
        self._file_icon = None

    # ----------------------------------------------------------- 对外接口
    def clear(self) -> None:
        self.tree.clear()
        self._items_by_path.clear()

    def _prune_subtree(self, item) -> None:
        """从索引中移除 item 及其所有后代（在 takeChildren 之前调用）。"""
        for i in range(item.childCount()):
            c = item.child(i)
            d = c.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and "path" in d:
                self._items_by_path.pop(d["path"], None)
            self._prune_subtree(c)

    def find_item_by_path(self, path):
        """按 path 查找节点，返回活的 QTreeWidgetItem；找不到返回 None。

        维护 ``_items_by_path`` 索引，O(1) 查找（替代整树递归遍历）。
        用于在异步回调（目录子项加载完成后）里「重新解析」节点引用，而非持有
        一个可能在请求期间被 tree.clear() 销毁的 QTreeWidgetItem。
        """
        return self._items_by_path.get(path)

    def set_root_entries(self, entries) -> None:
        self.tree.clear()
        self._items_by_path.clear()
        for e in entries:
            self.tree.addTopLevelItem(self._make_item(e))

    def set_children(self, parent_item, entries) -> None:
        self._prune_subtree(parent_item)
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
        """收集所有被勾选的【文件】路径（走索引，仅遍历已加载的文件节点）。"""
        paths = []
        for it in self._items_by_path.values():
            d = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and d.get("type") == "file":
                if it.checkState(2) == Qt.CheckState.Checked:
                    paths.append(d["path"])
        return paths

    # ----------------------------------------------------------- 内部
    def _make_item(self, entry):
        size = "" if entry.size is None else self._fmt_size(entry.size)
        it = QTreeWidgetItem([entry.name, size, ""])
        it.setData(0, Qt.ItemDataRole.UserRole,
                   {"path": entry.path, "type": entry.type, "loaded": False})
        # 图标：目录 / 文件用系统风格图标（懒缓存，避免每个节点重复查询 style()）
        if entry.type == "dir":
            if self._dir_icon is None:
                self._dir_icon = QApplication.style().standardIcon(
                    QStyle.StandardPixmap.SP_DirClosedIcon)
            it.setIcon(0, self._dir_icon)
        else:
            if self._file_icon is None:
                self._file_icon = QApplication.style().standardIcon(
                    QStyle.StandardPixmap.SP_FileIcon)
            it.setIcon(0, self._file_icon)
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
        # 登记到 path 索引，供 find_item_by_path / collect_checked O(1) 使用
        self._items_by_path[entry.path] = it
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
