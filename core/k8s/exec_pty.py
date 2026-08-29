# -*- coding: utf-8 -*-
"""K8s Pod 内交互式终端：本地 PTY + ``kubectl exec -it`` 常驻会话。

背景（为什么要有这个模块）
--------------------------
``core.k8s.exec_cmd.exec_command`` 走的是**一次性、无终端**通道::

    kubectl exec <pod> -n <ns> -c <c> -- sh -c "<script>"

既不分配 TTY（无 ``-t``），也不转发 stdin（``subprocess.run`` 直接继承服务端 stdin）。
因此 ``vim`` / ``top`` / ``htop`` / ``less`` 这类全屏（full-screen）程序会：

* 拿不到 tty → vim 报 ``Vim: Warning: Output is not to a terminal``，
  top 报 ``top: failed tty get``；
* 或继承到服务端 stdin 后一直阻塞，直到 60s 超时被 ``TimeoutExpired`` 打断，
  输出被丢弃 → 前端表现就是「输入命令没有任何反馈」。

本模块用**本地 PTY + ``kubectl exec -it``** 提供真正的常驻会话：

* ``pty.fork()`` 建一对主从 pty，子进程 setsid 后把从端设为控制终端；
* 子进程 ``execvp`` ``kubectl exec -it ...``——kubectl 因自身 stdin 是 tty 而分配远端
  TTY，并在收到 SIGWINCH 时转发窗口大小；
* 父进程持有主端 fd：写 = 转发键盘输入，读 = 转发程序输出，``TIOCSWINSZ`` = 改窗口大小。

与一次性通道的分工
------------------
* 一次性（``exec_command``）：脚本化、可断言退出码，用于文件读写 / 诊断取数；
* 交互式（本模块）：有 TTY，用于人工操作与全屏程序。
  ``interactive_command_hint`` 负责把「跑错通道」的命令拦下来并给出可读提示。
"""
import asyncio
import errno
import fcntl
import logging
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import termios
import threading
import time

from .exec_cmd import (
    _exec_base_args,
    _kubectl_subprocess_env,
    _resolve_kubectl_binary,
)

logger = logging.getLogger(__name__)

DEFAULT_COLS = 80
DEFAULT_ROWS = 24
DEFAULT_TERM = "xterm-256color"

# 需要真实终端才能工作的程序（全屏 / 持续刷新 / 分页器）
INTERACTIVE_COMMANDS = (
    "vim", "vi", "nvim", "nano", "pico", "emacs",
    "top", "htop", "atop", "btm",
    "less", "more", "man",
    "watch", "tail -f", "tailf", "multitail",
    "tmux", "screen", "mosh",
    "python", "python3", "node",       # 无 -c 的裸解释器 = REPL
    "mysql", "psql", "redis-cli", "mongo",
)

# 交互式命令在复合命令中的切分符（只看第一段，避免 `ls | less` 被误判为纯 ls）
_CMD_SPLIT_RE = re.compile(r'\|\||&&|;|\||\n')


def interactive_command_hint(command, names=INTERACTIVE_COMMANDS):
    """交互式/全屏命令给出可读提示，普通命令返回 ``''``（表示允许执行）。

    仅判断**第一段命令**：``ls | less`` 的首段是 ``ls``，不拦截（一次性通道里
    ``less`` 拿到管道会直接输出完）；``vim /etc/hosts`` 的首段是 ``vim``，拦截。
    """
    raw = (command or "").strip()
    if not raw:
        return ""
    head = _CMD_SPLIT_RE.split(raw)[0].strip()
    if not head:
        return ""
    low = head.lower()
    for name in names:
        n = name.lower()
        if low == n or low.startswith(n + " "):
            return (
                "「%s」是全屏/交互式程序，一次性执行通道没有终端（no TTY）无法渲染，"
                "也不会转发键盘输入。请在 K8s Shell 终端页执行。"
                % head
            )
    return ""


def build_pty_argv(env, pod, container, namespace, cwd=None,
                   cols=DEFAULT_COLS, rows=DEFAULT_ROWS, shell=""):
    """构造 ``kubectl exec -it ... -- sh -c <脚本>`` 的 argv。

    与 ``_exec_base_args`` 的差别：在 ``exec`` 后插入 ``-it``（分配 TTY + 转发 stdin），
    并用启动脚本把 TERM / PS1 / 窗口大小 / 工作目录准备妥当后 exec 一个交互式 shell。
    """
    base, _ns = _exec_base_args(env, pod, container, namespace)
    try:
        idx = base.index("exec")
    except ValueError:  # pragma: no cover - 防御：_exec_base_args 结构变更
        raise ValueError("_exec_base_args 返回参数中缺少 'exec' 子命令")
    argv = base[:idx] + ["exec", "-it"] + base[idx + 1:]
    argv += ["--", "sh", "-c", build_pty_script(cwd, cols, rows, shell)]
    return argv


#: 会话就绪标记：脚本打印 ``__K8S_PTY_READY__:<cwd>``，后端据此
#: （a）区分「连上了」与「kubectl 报错退出」，避免黑屏无提示；
#: （b）拿到真实起始工作目录。该行会被后端吃掉，不会显示到终端。
READY_MARKER = "__K8S_PTY_READY__"

READY_MARKER_RE = re.compile(re.escape(READY_MARKER) + r":([^\r\n]*)[\r\n]*")


def build_pty_script(cwd=None, cols=DEFAULT_COLS, rows=DEFAULT_ROWS, shell="",
                     ready_marker=True):
    """PTY 会话的启动脚本：设定工作目录 / TERM / 窗口大小，再 exec 交互式 shell。

    * ``cd`` 失败时回退到 ``/``，避免 shell 起不来导致终端黑屏；
    * ``ready_marker``：打印 ``__K8S_PTY_READY__:<cwd>`` 供后端判定会话是否真的建立；
    * ``stty rows/cols`` 直接设定**远端** pty 尺寸，不依赖 kubectl 的 resize 转发，
      让 vim / top 首屏就是正确布局（失败也不致命）；
    * 优先 ``bash -i``（支持 ``\\u@\\h:\\w`` 提示符），没有则回退 ``sh``。

    为什么用 ``exec``：替换掉中间 shell，让 kubectl 的 stdin/stdout 直接连到会话进程，
    Ctrl-C / 退出时信号语义与真实登录一致。
    """
    lines = []
    if cwd:
        lines.append("cd %s 2>/dev/null || cd /" % shlex.quote(cwd))
    if ready_marker:
        # printf 而非 echo：不依赖 echo 对反斜杠的实现差异
        lines.append("printf '\\n%s:%%s\\n' \"$(pwd)\"" % READY_MARKER)
    lines.append("export TERM=%s" % DEFAULT_TERM)
    lines.append("stty rows %d cols %d 2>/dev/null || true" % (int(rows), int(cols)))
    shell = (shell or "").strip()
    if shell:
        lines.append("exec %s" % shell)
    else:
        lines.append(
            "if command -v bash >/dev/null 2>&1; then "
            "PS1='\\u@\\h:\\w\\$ ' exec bash --noprofile --norc -i; fi"
        )
        lines.append("exec sh")
    return "\n".join(lines)


def kubectl_available():
    """kubectl 是否真实存在。

    ``_resolve_kubectl_binary()`` 在 PATH 里找不到时会返回兜底字符串 ``"kubectl"``，
    直接用它 fork 会得到「终端一黑、什么都没有」，因此启动前先做一次真实存在性校验。
    """
    resolved = _resolve_kubectl_binary()
    if not resolved:
        return False
    if os.path.isabs(resolved):
        return os.path.exists(resolved)
    return shutil.which(resolved) is not None


def _set_winsize(fd, rows, cols):
    """设置 pty 窗口大小（主从端共享同一 winsize，设主端即生效）。"""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", int(rows), int(cols), 0, 0))
        return True
    except Exception as ex:
        logger.debug("TIOCSWINSZ 失败 fd=%s: %s", fd, ex)
        return False


class PtySession:
    """一个常驻 PTY 会话：本地 pty 主端 ↔ 子进程（``kubectl exec -it``）。

    用法（async）::

        sess = PtySession(argv, cols=120, rows=30)
        sess.start()
        sess.write(b"ls\\r")
        sess.resize(120, 30)
        data = await sess.read()      # None 表示 EOF
        sess.close()

    线程模型：一个 daemon 读线程阻塞在 ``os.read(master)``，把字节放入
    ``asyncio.Queue``；``read()`` 在事件循环侧 await。写 / resize 直接系统调用，
    不经过事件循环。
    """

    _READ_SIZE = 65536
    _QUEUE_MAX = 4096   # 输出积压上限，超出丢弃最旧帧，避免 `yes` 类命令打爆内存

    def __init__(self, argv, env=None, cols=DEFAULT_COLS, rows=DEFAULT_ROWS,
                 loop=None):
        self._argv = list(argv)
        self._env = env
        self.cols = max(1, int(cols or DEFAULT_COLS))
        self.rows = max(1, int(rows or DEFAULT_ROWS))
        self._loop = loop
        self._queue = None
        self._master = None
        self._pid = None
        self._thread = None
        self._closed = True
        self._eof = False
        self.exit_code = None

    # ---- 生命周期 ------------------------------------------------------- #
    @property
    def alive(self):
        return self._pid is not None and not self._closed

    def start(self):
        """fork 出 PTY 子进程并启动读线程。失败会抛异常，调用方需自行兜底。"""
        if self._master is not None:
            return self
        if self._loop is None:
            # 优先取「正在运行」的循环：本方法常在 async 处理器里直接调用。
            # 若从线程池调用，get_running_loop 会抛 RuntimeError，再退回到
            # get_event_loop；都拿不到时 _loop=None，输出会被静默丢弃（终端黑屏），
            # 因此调用方在 asyncio 场景应显式传 loop=asyncio.get_running_loop()。
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                try:
                    self._loop = asyncio.get_event_loop()
                except RuntimeError:  # pragma: no cover
                    self._loop = None
        self._queue = asyncio.Queue()

        pid, master = pty.fork()
        if pid == 0:
            # ---- 子进程：只做 exec，不跑任何 Python 业务逻辑 ---- #
            try:
                if self._env:
                    os.environ.clear()
                    os.environ.update(self._env)
                os.execvp(self._argv[0], self._argv)
            except BaseException:
                os._exit(127)
            os._exit(127)  # pragma: no cover

        self._pid = pid
        self._master = master
        self._closed = False
        self._eof = False
        # fork 后立刻同步一次窗口大小：kubectl 启动需要 ~百毫秒，
        # 正常不会与其初始尺寸上报形成竞态；且首屏还有远端 stty 兜底
        _set_winsize(master, self.rows, self.cols)
        self._thread = threading.Thread(target=self._read_loop,
                                        name="k8s-pty-reader", daemon=True)
        self._thread.start()
        return self

    def close(self):
        """终止子进程、关闭 pty、回收线程。可重复调用。"""
        if self._closed and self._master is None:
            return
        self._closed = True
        master, pid, thread = self._master, self._pid, self._thread
        self._master = self._pid = self._thread = None

        if pid:
            self._terminate(pid)
        if master is not None:
            # 关主端会让子进程的 stdin 见 EOF；配合 killpg 足以唤醒阻塞的 read()
            try:
                os.close(master)
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)   # 唤醒阻塞中的 read()
            except Exception:
                pass

    def _terminate(self, pid):
        """异步收尾：SIGHUP 给远端 shell 一个体面退出的机会，超时再 SIGKILL。

        放在后台线程是为了不阻塞事件循环（``waitpid`` 会挂住调用方）。
        """
        def _kill():
            # pty.fork 已 setsid，子进程自成一进程组，整组一起收
            for sig, grace in ((signal.SIGHUP, 0.5), (signal.SIGKILL, 1.0)):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    return
                except OSError:
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        return
                if self._wait_exit(pid, grace):
                    return

        threading.Thread(target=_kill, name="k8s-pty-reaper", daemon=True).start()

    @staticmethod
    def _wait_exit(pid, timeout):
        """非阻塞轮询等待子进程退出；返回 True 表示已退出并被回收。"""
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            try:
                wpid, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True
            except OSError:  # pragma: no cover - EINTR 等
                return True
            if wpid == pid:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.05)

    # ---- IO -------------------------------------------------------------- #
    def write(self, data):
        """把键盘输入写进 pty（bytes 或 str）。"""
        if self._master is None or self._closed:
            return False
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            os.write(self._master, data)
            return True
        except OSError as ex:
            logger.debug("PTY 写入失败: %s", ex)
            self._closed = True
            return False

    def resize(self, cols, rows):
        """调整窗口大小：改本地 pty 尺寸 → kubectl 收到 SIGWINCH → 转发远端。"""
        if self._master is None or self._closed:
            return False
        self.cols = max(1, int(cols or self.cols))
        self.rows = max(1, int(rows or self.rows))
        return _set_winsize(self._master, self.rows, self.cols)

    async def read(self, timeout=None):
        """读取一段输出；返回 ``None`` 表示 EOF（子进程退出）。"""
        if self._queue is None:
            return None
        if self._eof:
            return None
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return b""        # 超时不是 EOF，返回空串让调用方继续循环
        except asyncio.CancelledError:
            return None
        if item is None:
            self._eof = True
            return None
        return item

    # ---- 读线程 ----------------------------------------------------------- #
    def _read_loop(self):
        # 先把 fd 抓成局部变量：close() 会把 self._master 置 None
        master = self._master
        if master is None:  # pragma: no cover
            return
        while not self._closed:
            try:
                data = os.read(master, self._READ_SIZE)
            except OSError as ex:
                # Linux 上 slave 全关后 read 返回 EIO = EOF；macOS 同理
                if ex.errno not in (errno.EIO, errno.EBADF):
                    logger.debug("PTY 读取异常: %s", ex)
                data = b""
            except ValueError:
                data = b""    # fd 已被 close
            if not data:
                break
            self._enqueue(data)
        self._enqueue(None)

    def _enqueue(self, item):
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if item is not None and self._queue.qsize() >= self._QUEUE_MAX:
                try:
                    self._queue.get_nowait()   # 丢弃最旧的一帧
                except Exception:
                    pass
            loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            pass      # 事件循环已关闭
        except Exception as ex:  # pragma: no cover
            logger.debug("PTY 输出入队失败: %s", ex)


def spawn_kubectl_pty(env, pod, container=None, namespace=None, cwd=None,
                      cols=DEFAULT_COLS, rows=DEFAULT_ROWS, shell="", loop=None):
    """构造并启动一个 ``kubectl exec -it`` PTY 会话。

    ``loop``：显式指定读线程回调所用的事件循环。**在 asyncio 场景务必传**
    ``asyncio.get_running_loop()``。若本函数被丢进线程池执行，
    ``PtySession.start()`` 里的 ``get_running_loop()`` 取不到循环会退化成
    ``None``，``_enqueue()`` 直接 return —— 表现为**终端连上了但永远黑屏**。
    """
    argv = build_pty_argv(env, pod, container, namespace, cwd=cwd,
                          cols=cols, rows=rows, shell=shell)
    return PtySession(argv, env=_kubectl_subprocess_env(),
                      cols=cols, rows=rows, loop=loop).start()
