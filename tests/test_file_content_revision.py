"""回归测试：Cookie 模式文件内容预览必须带上 revision 才能命中 GIJViewGitFileContent。

背景（2026-08-30）：
  浏览器里「查看文件」用的是 GIJViewGitFileContent.jspa?revision=<commitSHA>&repoId=&path=，
  该实例上 GIJFileBrowser.jspa.json 返回 404。旧 _cookie_file_content 把调用方传来的
  ref（分支 HEAD / 某次提交 SHA）直接丢弃，只打 GIJFileBrowser 且不带 revision → 永远取不到内容。

  修复：_cookie_file_content 现优先打 GIJViewGitFileContent.jspa?revision=<ref>，命中后从
  <script id="git-file-content-json"> 的 rawFile 或 <pre>/<code> 渲染块提取正文；revision 端点
  不可用时再回退 GIJFileBrowser。

  另修一个潜在崩溃：_looks_like_error_envelope 用 bytes 字面量去 in 一个 str，page 为 str 时
  TypeError；改为 str 字面量。
"""
import sys
import types
import unittest

sys.path.insert(0, ".")

from core.client.browse import BrowseMixin
from core.client.files import FilesMixin

SHA = "a1d450c761232d585b6deaf5f16987db27d16620"


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class _FakeClient(FilesMixin, BrowseMixin):
    def __init__(self, page_for_view):
        self.config = types.SimpleNamespace(
            jira_url="https://jira.hcmcloud.cn", cookie="sess=abc", mode="cookie")
        self.repo_id = "895"
        self.branch = ""
        self._branch_cache = {}
        self._head_cache = {}
        self.hits = []
        self._page_for_view = page_for_view  # callable(url) -> _Resp

    def http_get(self, url, headers=None, timeout=30):
        self.hits.append(url)
        if "GIJViewGitFileContent.jspa" in url:
            return self._page_for_view(url)
        if "GIJFileBrowser.jspa" in url:
            return _Resp(404, "not found")  # 该实例旧端点已死
        return _Resp(404, "nf")

    def cookie_headers(self):
        return {"Cookie": self.config.cookie}

    def _resolve_branch(self, rid, b):
        return "release"

    def _resolve_head(self, rid, b):
        return SHA


def _view_pre_page(url):
    return _Resp(200, '<html><body><pre class="code">print("hello")\nx = 1\n</pre></body></html>')


def _view_json_page(url):
    return _Resp(200, '<html><body><script id="git-file-content-json">'
                      '{"rawFile":"line1\\nline2\\nline3","contentType":"text/x-python"}'
                      '</script></body></html>')


class TestFileContentRevision(unittest.TestCase):
    def test_get_file_uses_revision_endpoint_and_extracts_pre(self):
        c = _FakeClient(_view_pre_page)
        content, err = c.get_file("migrate_flex_document.py")
        self.assertIsNone(err, err)
        self.assertIn('print("hello")', content)
        self.assertIn("x = 1", content)
        self.assertTrue(
            any(f"GIJViewGitFileContent.jspa?revision={SHA}" in h for h in c.hits),
            c.hits)

    def test_get_file_at_commit_uses_revision_and_extracts_json(self):
        c = _FakeClient(_view_json_page)
        content, err = c.get_file_at_commit("895", SHA, "migrate_flex_document.py")
        self.assertIsNone(err, err)
        self.assertEqual(content, "line1\nline2\nline3")

    def test_both_endpoints_dead_gives_clear_error(self):
        c = _FakeClient(lambda url: _Resp(404, "nf"))
        content, err = c.get_file("x.py")
        self.assertIsNone(content)
        self.assertIn("404", err)

    def test_error_envelope_no_crash_on_str_page(self):
        # _looks_like_error_envelope 现在对 str 页面不再抛 TypeError
        c = _FakeClient(lambda url: _Resp(200, '{"error":"forbidden"}'))
        content, err = c.get_file("x.py")
        self.assertIsNone(content)
        self.assertIn("错误包", err)

    def test_viewer_page_with_image_token_still_previews(self):
        """回归：GIJViewGitFileContent 的渲染页常带『image/*』MIME 选择/图片预览图标，
        诊断正则 _FILE_BROWSE_ERROR_RE 含宽泛词 'image' 会误中。修复后提取逻辑先于诊断正则，
        只要能从 <pre>/<code> 或 <script> JSON 取出正文就直接成功，根本不会跑到诊断正则。
        这里构造一个同时含 'image' 字样的 viewer 页，断言仍能正常预览、不报『文件过大或为二进制』。"""

        def _view_page_with_image(url):
            return _Resp(
                200,
                '<html><body>'
                '<div class="file-toolbar">'
                '<span class="mime">Content-Type: text/x-python; charset=utf-8</span>'
                '<select class="preview-mode"><option>text</option>'
                '<option>image</option></select>'
                '</div>'
                '<pre class="code">def main():\n    return 42\n</pre>'
                '</body></html>')

        c = _FakeClient(_view_page_with_image)
        content, err = c.get_file("utils.py")
        self.assertIsNone(err, err)
        self.assertIn("def main()", content)
        self.assertNotIn("文件过大或为二进制", content or "")


if __name__ == "__main__":
    unittest.main()
