# -*- coding: utf-8 -*-
"""Mock subprocess 测试：验证 list_dir 解析 与 exec_command cwd 追踪逻辑。

不依赖真实 kubectl / 集群。通过 monkeypatch `_run_kubectl_bytes` 注入伪输出，
覆盖：
  * _parse_ls：目录/文件分类、size/mode/modtime 字段提取、`.`/`..`/符号链接剔除
  * exec_command：__PWD__ 标记解析、clean output、cd 后新 cwd 追踪、失败抛 UserError
"""
import sys
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.k8s as _m
import core.k8s.exec_cmd as _cmd
import core.k8s.exec_fs as _fs
from core.errors import UserError


# ---- 注入伪 subprocess 输出 ------------------------------------------------- #
_CALLS = []


def _fake_run(argv, timeout=60, sub_env=None):
    """替身：记录 argv，按脚本内容返回伪造 (stdout, rc, stderr)。"""
    _CALLS.append(list(argv))
    script = ""
    if "--" in argv:
        script = argv[argv.index("--") + 3]  # ... sh -c <script>
    # list_dir: ls -la <path>
    if script.startswith("ls -la"):
        out = (
            "total 12\n"
            "drwxr-xr-x 1 root root 4096 Jan 02 10:00 .\n"
            "drwxr-xr-x 1 root root 4096 Jan 02 10:00 ..\n"
            "-rw-r--r-- 1 root root  128 Jan 02 10:01 app.log\n"
            "lrwxrwxrwx 1 root root   10 Jan 02 10:00 link -> /etc/hosts\n"
            "drwxr-xr-x 3 root root 4096 Jan 02 10:02 conf\n"
        ).encode("utf-8")
        return out, 0, b""
    # read_file: head -c <n> <path>  —— 这里不测，返回空
    if script.startswith("head -c"):
        return b"", 0, b""
    # exec_command: 带 __PWD__ 标记
    if "__PWD__" in script:
        # 模拟 `cd /etc && pwd` 之后输出标记
        out = (">>> hello\n"
               "\n"
               "__PWD__\n"
               "/etc\n").encode("utf-8")
        return out, 0, b""
    return b"", 0, b""


# ---- 测试 ------------------------------------------------------------------- #
def test_list_dir():
    _cmd._run_kubectl_bytes = _fake_run
    _fs._run_kubectl_bytes = _fake_run
    entries = _m.list_dir("dev", "mypod", None, None, "/tmp", timeout=10)
    # 期望剔除 total / . / .. ；符号链接(link) 保留为 file 且显示名去掉 → 目标
    # （契约：d 开头为 dir，- 开头为 file，其余归为 file，符合 Xftp 简化模型）
    names = {e["name"] for e in entries}
    assert names == {"app.log", "conf", "link"}, "条目应为 app.log + conf + link，实际 %r" % names
    app = next(e for e in entries if e["name"] == "app.log")
    assert app["type"] == "file", "app.log 应为 file"
    assert app["size"] == 128, "app.log size 应为 128"
    assert app["mode"].startswith("-"), "app.log mode 应为 - 开头"
    assert app["modtime"] == "Jan 02 10:01", "modtime 解析不符"
    conf = next(e for e in entries if e["name"] == "conf")
    assert conf["type"] == "dir", "conf 应为 dir"
    link = next(e for e in entries if e["name"] == "link")
    assert link["type"] == "file", "符号链接归为 file（双击 cat 目标，符合契约简化模型）"
    assert link["name"] == "link", "符号链接显示名应去掉 -> 目标部分"
    print("[OK] test_list_dir ->", entries)


def test_exec_command_cwd_tracking():
    _cmd._run_kubectl_bytes = _fake_run
    _fs._run_kubectl_bytes = _fake_run
    clean, new_cwd = _m.exec_command(
        "dev", "mypod", None, None, "echo hello", cwd="/tmp", timeout=10)
    assert "__PWD__" not in clean, "输出应剔除 __PWD__ 标记"
    assert "hello" in clean, "stdout 应包含 hello"
    assert new_cwd == "/etc", "cd /tmp && ... 后应追踪到 /etc，实际 %r" % new_cwd
    print("[OK] test_exec_command_cwd_tracking -> clean=%r new_cwd=%r" % (clean, new_cwd))


def test_exec_command_failure_raises_user_error():
    def _fail(argv, timeout=60, sub_env=None):
        return b"", 1, b"Error from server (NotFound): pods \"x\" not found"
    _cmd._run_kubectl_bytes = _fail
    _fs._run_kubectl_bytes = _fail
    caught = None
    try:
        _m.exec_command("dev", "mypod", None, None, "ls", cwd="/", timeout=10)
        assert False, "应当抛出 UserError"
    except UserError as ex:
        caught = ex
        assert "NotFound" in str(ex) or "执行命令失败" in str(ex), "错误信息不符: %s" % ex
    print("[OK] test_exec_command_failure_raises_user_error ->", str(caught))


def test_write_file_binary_pipes_base64_text():
    """回归测试：二进制写入必须把 base64 文本（而非解码后的原始字节）喂给容器内 base64 -d。

    若路由错误地先 b64decode 再传给 base64 -d，会导致「双重解码」损坏文件。
    """
    captured = {}

    def _fake_run(argv, timeout=60, sub_env=None):
        captured["argv"] = list(argv)
        captured["input"] = None
        # 捕获 stdin：真实 subprocess.run 的 input 参数在 _run_kubectl_bytes 之外，
        # 因此这里单独 monkeypatch write_file 依赖的 subprocess.run。
        return b"", 0, b""

    import subprocess as _sp

    def _fake_sp_run(argv, input=None, capture_output=False, timeout=60, env=None):
        captured["argv"] = list(argv)
        captured["input"] = input
        return _sp.CompletedProcess(argv, 0, b"", b"")

    _orig_sp_run = _fs._subprocess.run
    _fs._subprocess.run = _fake_sp_run
    try:
        # 以 base64 文本作为 content 调用（与路由修复后透传的行为一致）
        b64 = "aGVsbG8="  # "hello"
        _m.write_file("dev", "mypod", None, None, "/tmp/x.bin", b64, binary=True)
    finally:
        _fs._subprocess.run = _orig_sp_run

    script = captured["argv"][captured["argv"].index("--") + 3]
    assert script.startswith("base64 -d >"), "二进制写入脚本应为 base64 -d，实际: %s" % script
    # 关键断言：喂给 stdin 的是 base64 文本，而不是解码后的 b'hello'
    assert captured["input"] == b"aGVsbG8=", "stdin 应为 base64 文本，避免双重解码"
    assert captured["input"] != b"hello", "stdin 不应是已解码的原始字节"
    print("[OK] test_write_file_binary_pipes_base64_text -> script=%r stdin=%r"
          % (script, captured["input"]))


def test_write_file_text_pipes_raw():
    """文本写入直接 cat > path，stdin 为原文 UTF-8 字节。"""
    captured = {}
    import subprocess as _sp

    def _fake_sp_run(argv, input=None, capture_output=False, timeout=60, env=None):
        captured["argv"] = list(argv)
        captured["input"] = input
        return _sp.CompletedProcess(argv, 0, b"", b"")

    _orig_sp_run = _fs._subprocess.run
    _fs._subprocess.run = _fake_sp_run
    try:
        _m.write_file("dev", "mypod", None, None, "/tmp/x.txt", "你好", binary=False)
    finally:
        _fs._subprocess.run = _orig_sp_run
    script = captured["argv"][captured["argv"].index("--") + 3]
    assert script.startswith("cat >"), "文本写入脚本应为 cat，实际: %s" % script
    assert captured["input"] == "你好".encode("utf-8"), "stdin 应为原文 UTF-8 字节"
    print("[OK] test_write_file_text_pipes_raw -> script=%r" % script)


if __name__ == "__main__":
    test_list_dir()
    test_exec_command_cwd_tracking()
    test_exec_command_failure_raises_user_error()
    test_write_file_binary_pipes_base64_text()
    test_write_file_text_pipes_raw()
    print("\nALL MOCK TESTS PASSED")
