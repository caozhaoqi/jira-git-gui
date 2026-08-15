"""TreePanel path->item 索引与勾选收集的无头测试（offscreen）。

验证 ADR-002：
- find_item_by_path 走 O(1) 索引，且能命中懒加载后注入的子节点
- set_children 在 takeChildren 前用 _prune_subtree 清理旧子树索引（无悬空引用）
- collect_checked 只收集已勾选的【文件】节点，与递归语义一致
- clear 重置索引
"""
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.models import TreeEntry
from gui.tree_panel import TreePanel


_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication(sys.argv)
    return _APP


def _entry(name, path, etype, size=None):
    return TreeEntry(name=name, path=path, type=etype, size=size)


class TestTreePanelIndex(unittest.TestCase):
    def setUp(self):
        _app()

    def test_find_item_by_path_root_and_child(self):
        p = TreePanel()
        p.set_root_entries([
            _entry("src", "src", "dir"),
            _entry("readme.md", "readme.md", "file", 123),
        ])
        f = p.find_item_by_path("readme.md")
        self.assertIsNotNone(f)
        self.assertEqual(f.data(0, Qt.ItemDataRole.UserRole)["path"], "readme.md")
        d = p.find_item_by_path("src")
        self.assertIsNotNone(d)
        self.assertEqual(d.data(0, Qt.ItemDataRole.UserRole)["type"], "dir")
        self.assertIsNone(p.find_item_by_path("nope"))

    def test_find_item_after_set_children(self):
        p = TreePanel()
        p.set_root_entries([_entry("src", "src", "dir")])
        parent = p.find_item_by_path("src")
        p.set_children(parent, [
            _entry("a.py", "src/a.py", "file", 10),
            _entry("sub", "src/sub", "dir"),
        ])
        a = p.find_item_by_path("src/a.py")
        self.assertIsNotNone(a)
        self.assertEqual(a.data(0, Qt.ItemDataRole.UserRole)["path"], "src/a.py")
        self.assertIsNotNone(p.find_item_by_path("src/sub"))

    def test_set_children_prunes_old_index(self):
        p = TreePanel()
        p.set_root_entries([_entry("src", "src", "dir")])
        parent = p.find_item_by_path("src")
        p.set_children(parent, [_entry("old.py", "src/old.py", "file", 1)])
        self.assertIsNotNone(p.find_item_by_path("src/old.py"))
        p.set_children(parent, [_entry("new.py", "src/new.py", "file", 2)])
        self.assertIsNone(p.find_item_by_path("src/old.py"))
        self.assertIsNotNone(p.find_item_by_path("src/new.py"))

    def test_collect_checked_only_checked_files(self):
        p = TreePanel()
        p.set_root_entries([
            _entry("a.py", "a.py", "file", 10),
            _entry("b.py", "b.py", "file", 20),
            _entry("dir", "dir", "dir"),
        ])
        p.find_item_by_path("a.py").setCheckState(2, Qt.CheckState.Checked)
        self.assertEqual(p.collect_checked(), ["a.py"])
        p.find_item_by_path("b.py").setCheckState(2, Qt.CheckState.Checked)
        self.assertEqual(p.collect_checked(), ["a.py", "b.py"])

    def test_clear_resets_index(self):
        p = TreePanel()
        p.set_root_entries([_entry("x.py", "x.py", "file", 1)])
        self.assertIsNotNone(p.find_item_by_path("x.py"))
        p.clear()
        self.assertIsNone(p.find_item_by_path("x.py"))
        self.assertEqual(p.collect_checked(), [])

    def test_placeholder_child_not_indexed(self):
        """目录默认带「加载中…」占位子项，它无 path，不应进索引。"""
        p = TreePanel()
        p.set_root_entries([_entry("src", "src", "dir")])
        parent = p.find_item_by_path("src")
        self.assertEqual(list(p._items_by_path.keys()), ["src"])
        p.set_children(parent, [])
        self.assertEqual(list(p._items_by_path.keys()), ["src"])


if __name__ == "__main__":
    unittest.main()
