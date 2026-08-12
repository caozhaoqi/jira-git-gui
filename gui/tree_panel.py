"""文件树面板：懒加载的 QTreeWidget。

职责（纯视图）：
- requestRoot         : 请求根目录条目
- requestChildren     : 展开目录时请求其子项
- fileActivated       : 点击文件节点时请求正文
数据获取由 MainWindow 通过 Worker 完成，再把结果回调到这里。
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget


class TreePanel(QWidget):
    requestRoot = pyqtSignal()
    requestChildren = pyqtSignal(object, str)  # (QTreeWidgetItem, path)
    fileActivated = pyqtSignal(str)            # path

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称", "大小", "选择"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 90)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemClicked.connect(self._on_clicked)
        layout.addWidget(self.tree)

    # ----------------------------------------------------------- 对外接口
    def clear(self) -> None:
        self.tree.clear()

    def set_root_entries(self, entries) -> None:
        self.tree.clear()
        for e in entries:
            self.tree.addTopLevelItem(self._make_item(e))

    def set_children(self, parent_item, entries) -> None:
        parent_item.takeChildren()
        d = parent_item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(d, dict):
            d["loaded"] = True
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
        if entry.type == "dir":
            # 放一个占位子项，使目录显示展开箭头（懒加载）
            it.addChild(QTreeWidgetItem(["（加载中…", "", ""]))
        else:
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(2, Qt.CheckState.Unchecked)
        return it

    def _on_expanded(self, item):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(d, dict) or d.get("type") != "dir":
            return
        if d.get("loaded"):
            return
        item.takeChildren()
        item.addChild(QTreeWidgetItem(["（加载中…", "", ""]))
        self.requestChildren.emit(item, d["path"])

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
