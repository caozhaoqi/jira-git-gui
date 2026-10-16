# -*- coding: utf-8 -*-
"""冻结后端必须能 import：模拟 Windows 环境挡住 Unix 专属模块后完整导入后端链。

背景（2026-10-16 CI 现场）：GitHub CI 打出的 Windows 安装包后端起不来，
连续两轮「启动冒烟」依次抓到：
  ① ModuleNotFoundError: No module named 'yaml'      —— requirements.txt 漏声明 PyYAML
  ② ModuleNotFoundError: No module named 'fcntl'     —— core/k8s/exec_pty.py 裸 import Unix 专属模块
两者都只在 CI 产物上暴露（本机 venv 用着自然存在 / Unix 上 fcntl 一直存在），
本地怎么跑都发现不了。本测试把这两类问题变成可离线跑的回归：

  ① 由 CI 的启动冒烟（release.yml）保证；
  ② 由本文件保证：挡住 Unix 专属 stdlib 后 import 整个后端链。

AST 扫描那条保证「将来没人再往模块顶层裸写 fcntl/pty/termios」。
"""
import ast
import os
import subprocess
import sys
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Unix / POSIX 专属、Windows 上不存在的 stdlib 模块
UNIX_ONLY_MODULES = {
    "fcntl", "pty", "termios", "tty", "resource", "grp", "pwd",
    "syslog", "curses", "ossaudiodev", "spwd", "nis", "readline",
}

#: 冻结后端入口会拉进来的包目录（不含 tests/ 与 venv/）
SCAN_DIRS = ("api", "core")


def test_no_unguarded_unix_only_top_level_imports():
    """api/ 与 core/ 里不允许出现模块顶层裸 import 的 Unix 专属模块。

    合法姿势只有两种：整个包不 import（放到 try/except 里降级），
    或像 core/k8s/exec_pty.py 那样 try/except ImportError 后置
    ``PTY_SUPPORTED = False`` 并在真正使用时给出可读错误。
    """
    offenders = []
    for pkg in SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, pkg)):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "tests")]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, REPO_ROOT)
                tree = ast.parse(open(path, encoding="utf-8").read(), rel)
                # try/except 里的 import 视为已降级（安全）；其余按模块顶层判定
                guarded = set()
                for stmt in ast.walk(tree):
                    if isinstance(stmt, ast.Try):
                        for sub in ast.walk(stmt):
                            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                                guarded.add(id(sub))
                for node in ast.walk(tree):
                    if id(node) in guarded or not _is_module_level(tree, node):
                        continue
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                        for n in names:
                            if n in UNIX_ONLY_MODULES:
                                offenders.append("%s:%d import %s" % (rel, node.lineno, n))
                    elif isinstance(node, ast.ImportFrom):
                        if not node.module:
                            continue
                        top = node.module.split(".")[0]
                        if top in UNIX_ONLY_MODULES:
                            offenders.append(
                                "%s:%d from %s import ..." % (rel, node.lineno, node.module))
    assert not offenders, (
        "以下位置裸 import 了 Unix 专属模块，Windows 冻结产物一启动就会"
        " ModuleNotFoundError（套 try/except ImportError 降级即可）：\n  "
        + "\n  ".join(offenders))


def _is_module_level(tree, node):
    """判断 import 语句是否位于模块顶层（函数/类之外）。"""
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(sub is node for sub in ast.walk(child)):
                return False
    return True


def test_backend_import_chain_survives_without_unix_modules():
    """挡住 Unix 专属模块后 import api.server 必须成功（等价于 Windows 环境）。"""
    script = textwrap.dedent(
        """
        import sys

        BLOCKED = %r

        class _Blocker:
            def find_module(self, name, path=None):
                return self if name.split('.')[0] in BLOCKED else None

            def find_spec(self, name, path=None, target=None):
                if name.split('.')[0] in BLOCKED:
                    raise ImportError('blocked for windows-import test: ' + name)
                return None

        sys.meta_path.insert(0, _Blocker())
        for name in list(sys.modules):
            if name.split('.')[0] in BLOCKED:
                del sys.modules[name]

        import api.server  # noqa: F401
        from core.k8s import exec_pty
        assert exec_pty.PTY_SUPPORTED is False, 'hook 未生效？'
        try:
            exec_pty.PtySession(['true']).start()
        except RuntimeError as ex:
            assert 'Unix' in str(ex)
        else:
            raise AssertionError('PTY_SUPPORTED=False 时 start() 必须抛 RuntimeError')
        print('WINDOWS_IMPORT_OK')
        """
    ) % (sorted(UNIX_ONLY_MODULES),)

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "后端 import 链在无 Unix 专属模块时失败（= Windows 上启动即崩）：\n"
        + proc.stdout[-3000:] + "\n" + proc.stderr[-3000:])
    assert "WINDOWS_IMPORT_OK" in proc.stdout
