"""tree_panel 单测：路径解析、过期回调安全丢弃、基础增删。

需要 QApplication（用 offscreen 头less 平台，避免弹窗）。
整个进程只允许一个 QApplication，故在导入 PyQt 之前先设好环境变量。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
import unittest  # noqa: E402

_app = QApplication.instance() or QApplication([])

from gui.tree_panel import TreePanel  # noqa: E402


class _Entry:
    """模拟 core.models.FileEntry 的最小替身，避免为单测引入整个 core。"""

    def __init__(self, name, path, type_, size=None):
        self.name = name
        self.path = path
        self.type = type_  # 'dir' / 'file'
        self.size = size


class TestFindItemByPath(unittest.TestCase):
    def setUp(self):
        self.panel = TreePanel()

    def _seed(self):
        # 根：dirA (path=/a) -> 含 dirB (path=/a/b)；以及文件 /c
        self.panel.set_root_entries([
            _Entry("a", "/a", "dir"),
            _Entry("c", "/c", "file", 1024),
        ])
        root = self.panel.tree.invisibleRootItem()
        item_a = root.child(0)
        item_a.takeChildren()
        item_a.addChild(self.panel._make_item(_Entry("b", "/a/b", "dir")))

    def test_find_nested(self):
        self._seed()
        it = self.panel.find_item_by_path("/a/b")
        self.assertIsNotNone(it)
        d = it.data(0, Qt.ItemDataRole.UserRole)
        self.assertEqual(d["path"], "/a/b")

    def test_find_top_level(self):
        self._seed()
        self.assertIsNotNone(self.panel.find_item_by_path("/c"))

    def test_find_missing_returns_none(self):
        self._seed()
        self.assertIsNone(self.panel.find_item_by_path("/nope"))

    def test_find_after_clear_returns_none(self):
        # 模拟「切换仓库 / 重新加载根目录」后 tree.clear() 销毁所有节点
        self._seed()
        self.panel.clear()
        self.assertIsNone(self.panel.find_item_by_path("/a/b"))


class TestStaleCallback(unittest.TestCase):
    """回归：请求在途时树被重建，回调按 path 解析找不到节点应安全丢弃（不崩）。

    这正是 main_window._set_children 的防御逻辑所依赖的底层保证：
    find_item_by_path 在节点被销毁后返回 None，于是回调直接 return，
    不再触碰已销毁的 QTreeWidgetItem（否则会抛
    "wrapped C/C++ object of type QTreeWidgetItem has been deleted"）。
    """

    def test_path_resolves_to_none_after_clear(self):
        panel = TreePanel()
        panel.set_root_entries([_Entry("a", "/a", "dir")])
        panel.clear()  # 树已重建，旧 path 的节点已销毁
        self.assertIsNone(panel.find_item_by_path("/a"))


class TestSetChildren(unittest.TestCase):
    def setUp(self):
        self.panel = TreePanel()
        self.panel.set_root_entries([_Entry("a", "/a", "dir")])
        self.item_a = self.panel.tree.invisibleRootItem().child(0)

    def test_populates_and_marks_loaded(self):
        entries = [_Entry("x", "/a/x", "file", 10), _Entry("y", "/a/y", "dir")]
        self.panel.set_children(self.item_a, entries)
        self.assertEqual(self.item_a.childCount(), 2)
        d = self.item_a.data(0, Qt.ItemDataRole.UserRole)
        self.assertTrue(d["loaded"])

    def test_empty_collapses(self):
        self.panel.set_children(self.item_a, [])
        self.assertFalse(self.item_a.isExpanded())


if __name__ == "__main__":
    unittest.main()
