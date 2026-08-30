# -*- coding: utf-8 -*-
"""K8s Shell 交互式终端端到端测试（真 PTY，不需要 k8s 集群）。

用 ``tests/_mock_kubectl.py`` 替代 kubectl：它把 ``kubectl exec -it pod -- sh -c S``
等价为在本地 pty 上 ``exec sh -c S``，因此 **PTY 主从 / TERM / 窗口大小 /
行规程 / 信号** 整条链路与真实场景一致，可以真实验证全屏程序的渲染与交互。

覆盖场景：
  1. ready 帧带 ``tty=true``（确认走的是 PTY 分支而非行缓冲降级）
  2. 普通命令（echo）有回显与输出
  3. ``vim``：进入全屏（alternate screen）、可编辑、``:q!`` 能退出并恢复屏幕
  4. ``top``：持续刷新（多帧输出），``q`` 能退出
  5. ``less``：进入全屏分页，``q`` 能退出
  6. 窗口 resize 能影响远端（``stty size`` / ``tput cols`` 变化）
  7. 行缓冲降级模式（tty=0）下执行全屏命令要有明确提示，而不是静默无反馈

运行：``./venv/bin/python tests/test_k8s_shell_pty_e2e.py``
"""
import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# 替身 kubectl 注入
# --------------------------------------------------------------------------- #
def _install_mock_kubectl():
    """把 mock kubectl 放到临时目录并前置到 PATH，返回该临时目录。"""
    tmp = tempfile.mkdtemp(prefix="k8s-pty-mock-")
    src = ROOT / "tests" / "_mock_kubectl.py"
    dst = Path(tmp) / "kubectl"
    shutil.copyfile(src, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PATH"] = tmp + os.pathsep + os.environ.get("PATH", "")
    return tmp


def _patch_base_args():
    """让 build_pty_argv 不需要真实环境配置（get_env 依赖本地配置文件）。"""
    import core.k8s.exec_pty as _ep

    def fake_base_args(env_name, pod, container, namespace):
        args = ["kubectl", "exec", pod]
        ns = namespace or "default"
        args += ["-n", ns]
        if container:
            args += ["-c", container]
        return args, ns

    _ep._exec_base_args = fake_base_args


# --------------------------------------------------------------------------- #
# 假 WebSocket：前端替身
# --------------------------------------------------------------------------- #
class FakeWS:
    """实现 routes_k8s_exec 用到的 WebSocket 子集，并支持按脚本驱动。"""

    def __init__(self, tty="1"):
        self.query_params = {"tty": tty}
        self._inbox = asyncio.Queue()
        self.outbox = []
        self._event = asyncio.Event()
        self.accepted = False
        self.closed = False

    # --- 后端调用的接口 --- #
    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return await self._inbox.get()

    async def send_json(self, obj):
        self.outbox.append(obj)
        self._event.set()

    async def close(self):
        self.closed = True

    # --- 测试驱动辅助 --- #
    async def push(self, payload: dict):
        await self._inbox.put(json.dumps(payload))

    def output_text(self):
        return "".join(m.get("data", "") for m in self.outbox
                       if m.get("type") == "output")

    def errors(self):
        return [m.get("msg", "") for m in self.outbox if m.get("type") == "error"]

    async def _wait_new(self, remain):
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), remain)
        except asyncio.TimeoutError:
            pass

    async def wait_for(self, type_, timeout=20.0, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for m in self.outbox:
                if m.get("type") == type_ and (predicate is None or predicate(m)):
                    return m
            remain = deadline - loop.time()
            if remain <= 0:
                types = [x.get("type") for x in self.outbox]
                raise AssertionError(
                    "等待 %s 帧超时(%.0fs)；已收到帧类型=%r；错误帧=%r"
                    % (type_, timeout, types, self.errors()))
            await self._wait_new(remain)

    async def collect_until(self, needle, timeout=20.0):
        """累积输出直到包含 needle（用于等全屏程序首屏绘制）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            text = self.output_text()
            if needle in text:
                return text
            remain = deadline - loop.time()
            if remain <= 0:
                raise AssertionError(
                    "等待输出包含 %r 超时(%.0fs)；当前输出尾部=%r；错误帧=%r"
                    % (needle, timeout, text[-300:], self.errors()))
            await self._wait_new(remain)

    async def quiet(self, seconds=1.0):
        """等待输出静默 seconds 秒（用于判定 top 这类持续刷新程序已停止）。"""
        await asyncio.sleep(seconds)


async def _open_session(tty="1", pod="test-pod", cols=None, rows=None):
    """建立一条终端会话，返回 (FakeWS, 后台任务)。

    ``cols`` / ``rows`` 模拟前端建连时上报的首屏窗口尺寸（``?cols=&rows=``）。
    """
    import api.k8s.routes_k8s_exec as _rt

    ws = FakeWS(tty=tty)
    if cols:
        ws.query_params["cols"] = str(cols)
    if rows:
        ws.query_params["rows"] = str(rows)
    task = asyncio.ensure_future(
        _rt._ws_k8s_exec_tty(ws, "dev", "default", pod, None, {}))
    return ws, task


async def _close_session(ws, task):
    """结束会话并回报后台任务异常（否则终端崩溃原因会被静默吞掉）。"""
    if not task.done():
        try:
            await ws.push({"type": "disconnect"})
        except Exception:
            pass
    try:
        await asyncio.wait_for(task, timeout=10)
    except asyncio.TimeoutError:
        task.cancel()
    except Exception:
        pass
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc:
            print("      [会话任务异常] %s: %s" % (type(exc).__name__, exc))


# --------------------------------------------------------------------------- #
# 场景 1：ready 帧必须带 tty=true
# --------------------------------------------------------------------------- #
async def scenario_ready_is_tty():
    ws, task = await _open_session()
    try:
        ready = await ws.wait_for("ready", timeout=25)
        assert ready.get("tty") is True, \
            "ready 帧缺少 tty=true，说明降级到了行缓冲模式（全屏程序必然无反馈）"
        assert ready.get("cwd"), "ready 应带远端起始工作目录"
        print("[OK] scenario_ready_is_tty -> ready=%r" % ready)
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 2：普通命令有回显与输出
# --------------------------------------------------------------------------- #
async def scenario_plain_command():
    ws, task = await _open_session()
    try:
        await ws.wait_for("ready", timeout=25)
        await ws.push({"type": "input", "data": "echo HELLO_PTY\r"})
        await ws.collect_until("HELLO_PTY", timeout=15)
        assert "echo HELLO_PTY" in ws.output_text(), "应有远端回显（pty echo）"
        print("[OK] scenario_plain_command -> 回显与输出正常")
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 3：vim 全屏
# --------------------------------------------------------------------------- #
async def scenario_vim():
    ws, task = await _open_session()
    try:
        await ws.wait_for("ready", timeout=25)
        await ws.push({"type": "resize", "cols": 100, "rows": 30})
        path = "/tmp/_k8s_pty_e2e_vim.txt"
        await ws.push({"type": "input", "data": "vim %s\r" % path})

        # vim 进入 alternate screen 才算真的全屏渲染成功
        await ws.collect_until("\x1b[?1049h", timeout=15)

        # 进入插入模式写入内容，再 Esc + :wq 退出
        await ws.push({"type": "input", "data": "i"})
        await ws.push({"type": "input", "data": "written-by-pty-test"})
        await ws.push({"type": "input", "data": "\x1b"})       # ESC
        await ws.push({"type": "input", "data": ":wq\r"})

        # 退出 alternate screen，回到主屏幕
        await ws.collect_until("\x1b[?1049l", timeout=15)

        # 文件内容应真的被写入
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "written-by-pty-test" in content, \
            "vim 编辑内容未落盘，实际=%r" % content
        os.remove(path)
        print("[OK] scenario_vim -> 进入/编辑/保存/退出全链路正常")
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 4：top 持续刷新
# --------------------------------------------------------------------------- #
async def scenario_top():
    ws, task = await _open_session()
    try:
        await ws.wait_for("ready", timeout=25)
        await ws.push({"type": "resize", "cols": 100, "rows": 30})
        before = len(ws.output_text())
        await ws.push({"type": "input", "data": "top\r"})

        # top 首屏
        await ws.collect_until("PID", timeout=15)
        # 持续刷新：再等 2.5s，输出应继续增长（top 默认 1s 刷新一次）
        await asyncio.sleep(2.5)
        after = len(ws.output_text())
        assert after > before + 200, \
            "top 未持续刷新（%d -> %d），可能被当作一次性命令执行" % (before, after)

        await ws.push({"type": "input", "data": "q"})
        await asyncio.sleep(1.5)
        len_before_quiet = len(ws.output_text())
        await ws.quiet(2.0)
        assert len(ws.output_text()) == len_before_quiet, \
            "发送 q 后 top 仍在输出，未正常退出"
        print("[OK] scenario_top -> 持续刷新 %d 字符且 q 可退出" % (after - before))
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 5：less 分页
# --------------------------------------------------------------------------- #
async def scenario_less():
    ws, task = await _open_session()
    try:
        await ws.wait_for("ready", timeout=25)
        await ws.push({"type": "resize", "cols": 100, "rows": 30})
        # 内容必须超过一屏，否则 less 直接输出完就退出，不会进全屏分页模式
        await ws.push({"type": "input",
                       "data": "seq 1 300 > /tmp/_k8s_pty_e2e_less.txt\r"})
        await asyncio.sleep(1.2)
        await ws.push({"type": "input", "data": "less /tmp/_k8s_pty_e2e_less.txt\r"})
        # less 在 tty 下会切到 alternate screen 全屏渲染
        await ws.collect_until("\x1b[?1049h", timeout=10)
        await ws.push({"type": "input", "data": "q"})
        await ws.collect_until("\x1b[?1049l", timeout=10)
        if os.path.exists("/tmp/_k8s_pty_e2e_less.txt"):
            os.remove("/tmp/_k8s_pty_e2e_less.txt")
        print("[OK] scenario_less -> 全屏分页与 q 退出正常")
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 6：resize 同步到远端
# --------------------------------------------------------------------------- #
async def scenario_resize():
    ws, task = await _open_session()
    try:
        await ws.wait_for("ready", timeout=25)
        await ws.push({"type": "resize", "cols": 132, "rows": 40})
        await asyncio.sleep(0.8)
        await ws.push({"type": "input", "data": "stty size\r"})
        await ws.collect_until("40 132", timeout=10)
        assert "40 132" in ws.output_text(), \
            "resize 未同步到远端 pty，实际输出=%r" % ws.output_text()[-200:]
        print("[OK] scenario_resize -> 窗口尺寸同步正常 (40 行 132 列)")
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 7：建连时上报的首屏尺寸应直接生效（不等 resize 追赶）
# --------------------------------------------------------------------------- #
async def scenario_initial_size():
    """前端连上时就把 xterm 的 cols/rows 带上，首屏即应为该尺寸。

    否则远端会先按默认 80x24 起来，首屏错位，且要等一次 resize 才纠正。
    """
    ws, task = await _open_session(cols=110, rows=33)
    try:
        await ws.wait_for("ready", timeout=25)
        # 刻意**不发**任何 resize 帧
        await ws.push({"type": "input", "data": "stty size\r"})
        await ws.collect_until("33 110", timeout=15)
        assert "33 110" in ws.output_text(), \
            "首屏尺寸未生效，实际=%r" % ws.output_text()[-200:]
        print("[OK] scenario_initial_size -> 首屏即为 33 行 110 列（未发 resize）")
    finally:
        await _close_session(ws, task)


# --------------------------------------------------------------------------- #
# 场景 8：行缓冲降级模式下全屏命令要有明确提示
# --------------------------------------------------------------------------- #
async def scenario_degraded_hint():
    ws, task = await _open_session(tty="0")
    try:
        ready = await ws.wait_for("ready", timeout=15)
        assert not ready.get("tty"), "tty=0 时应为降级模式"
        await ws.push({"type": "input", "data": "vim /tmp/x\r"})
        # 注意：连上时已回过一条「降级原因」error 帧，这里要等的是 vim 那条
        err = await ws.wait_for(
            "error", timeout=25,
            predicate=lambda m: "vim" in (m.get("msg") or ""))
        print("[OK] scenario_degraded_hint -> %r" % (err.get("msg") or "")[:70])
    finally:
        await _close_session(ws, task)


SCENARIOS = [
    ("ready 帧带 tty=true", scenario_ready_is_tty),
    ("普通命令回显输出", scenario_plain_command),
    ("vim 全屏编辑", scenario_vim),
    ("top 持续刷新", scenario_top),
    ("less 全屏分页", scenario_less),
    ("resize 同步", scenario_resize),
    ("首屏尺寸", scenario_initial_size),
    ("降级模式提示", scenario_degraded_hint),
]


async def _main(only=None):
    failed = []
    for name, fn in SCENARIOS:
        if only and only not in name:
            continue
        try:
            await fn()
        except Exception as ex:
            failed.append((name, ex))
            print("[FAIL] %-20s %s: %s" % (name, type(ex).__name__, ex))
    return failed


if __name__ == "__main__":
    _install_mock_kubectl()
    _patch_base_args()
    only = sys.argv[1] if len(sys.argv) > 1 else None
    failed = asyncio.run(_main(only))
    print("")
    if failed:
        print("FAILED %d/%d" % (len(failed), len(SCENARIOS)))
        for name, ex in failed:
            print("  - %s: %s" % (name, ex))
        sys.exit(1)
    print("ALL PTY E2E SCENARIOS PASSED")
