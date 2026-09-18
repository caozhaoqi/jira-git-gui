# -*- coding: utf-8 -*-
"""SSH 远程 kubectl 执行（账密登录远程服务器，在其上执行 kubectl）。

场景：本机无法直连集群 API Server，但可 SSH（账密）登录一台已配好
kubectl + ~/.kube/config 的运维机。本模块把 kubectl 命令投递到远端执行，
输出 / 退出码回传，对上层调用方透明。

约定（最小改动接入）：
- 环境配置带 ``ssh_host`` 即视为 SSH 环境；
- ``resolve_env_kubeconfig`` 返回 ``ssh://<env_name>`` 标记代替本地 kubeconfig 路径；
- ``run_kubectl`` / ``run_kubectl_async`` / ``stream_kubectl`` 识别该标记后
  委托到本模块 —— 因此 Pod 列表 / 日志（含 -f 流式跟随）/ events / describe /
  top / 快照等全部查询路径自动支持，路由层零改动。

远端 kubectl 使用远端默认配置（~/.kube/config / KUBECONFIG 环境变量）；
若环境里配了 context，会以 ``--context`` 透传。
"""
import asyncio
import shlex
import threading
import time

from core.errors import UserError

SSH_MARKER_PREFIX = "ssh://"

# 同一环境的 SSH 连接进程内复用（paramiko Transport 线程安全，可多命令并发）；
# 进程级锁兜底建连竞争。流式 / 一次性命令共用连接。
_SSH_CLIENTS: dict = {}
_SSH_LOCK = threading.Lock()
_SSH_CONNECT_TIMEOUT = 10


def is_ssh_target(kubeconfig):
    """kubeconfig 是否为 SSH 环境标记（``ssh://<env_name>``）。"""
    return bool(kubeconfig) and str(kubeconfig).startswith(SSH_MARKER_PREFIX)


def marker_env_name(kubeconfig):
    """从标记中解析环境名。"""
    return str(kubeconfig)[len(SSH_MARKER_PREFIX):]


def _drop_client(env_name):
    with _SSH_LOCK:
        cli = _SSH_CLIENTS.pop(env_name, None)
    if cli is not None:
        try:
            cli.close()
        except Exception:
            pass


def _get_client(env_name):
    """获取（必要时建立）该环境的 SSH 连接。延迟导入避免循环依赖。"""
    from .env import get_env  # env -> kubectl -> ssh_exec -> env，函数内导入断环
    import paramiko

    with _SSH_LOCK:
        cli = _SSH_CLIENTS.get(env_name)
        if cli is not None:
            try:
                tr = cli.get_transport()
                if tr is not None and tr.is_active():
                    return cli
            except Exception:
                pass
            _SSH_CLIENTS.pop(env_name, None)

        _, env = get_env(env_name)
        host = (env.get("ssh_host") or "").strip()
        if not host:
            raise UserError("环境 '%s' 未配置 SSH 主机（ssh_host 为空）。" % env_name)
        user = (env.get("ssh_user") or "root").strip()
        try:
            port = int(env.get("ssh_port") or 22)
        except (TypeError, ValueError):
            port = 22
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(
            host, port=port, username=user,
            password=env.get("ssh_password") or None,
            timeout=_SSH_CONNECT_TIMEOUT,
            allow_agent=False, look_for_keys=False,  # 纯账密认证
        )
        _SSH_CLIENTS[env_name] = cli
        return cli


def _remote_kubectl_command(args):
    """拼远端命令行：kubectl ...（逐参数 shlex.quote 防注入）。"""
    return " ".join(["kubectl"] + [shlex.quote(str(a)) for a in args])


def run_kubectl_ssh(env_name, args, timeout=60, input=None):
    """SSH 远端执行 kubectl，返回 (stdout, rc, stderr)，签名对齐 run_kubectl。

    - 错误信息带 ``(SSH <env>)`` 前缀，与本地 kubectl 报错明确区分；
    - ``recv_exit_status`` 轮询硬超时，远端命令挂死时返回 rc=124，
      不会把调用线程永久卡住；
    - 连接类异常自动丢弃缓存连接并重试一次（应对 sshd 回收半死连接）。
    """
    try:
        import paramiko  # noqa: F401
    except ImportError:
        raise UserError("SSH 远程执行需要 paramiko：请在服务实际使用的 venv 内 pip install paramiko。")
    cmd = _remote_kubectl_command(args)
    last_err = ""
    for attempt in (1, 2):  # 连接类故障重试一次（首次可能是缓存连接已半死）
        try:
            cli = _get_client(env_name)
        except UserError:
            raise
        except Exception as ex:
            _drop_client(env_name)
            last_err = "SSH 连接失败：%s" % ex
            continue
        try:
            stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
            if input is not None:
                stdin.write(input if isinstance(input, bytes) else str(input).encode("utf-8"))
                stdin.channel.shutdown_write()
            chan = stdout.channel
            deadline = time.time() + max(timeout, 1)
            while not chan.exit_status_ready():
                if time.time() >= deadline:
                    raise TimeoutError("远端命令 %ss 内未返回" % timeout)
                time.sleep(0.1)
            rc = chan.recv_exit_status()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            return out, rc, err
        except UserError:
            raise
        except Exception as ex:
            _drop_client(env_name)  # 连接类故障丢弃缓存连接，重试/下次重连
            last_err = "SSH 执行失败：%s" % ex
    return "", 255, "(SSH %s) %s" % (env_name, last_err)


async def run_kubectl_ssh_async(env_name, args, timeout=60, input=None):
    """异步版：放线程池避免阻塞事件循环（对齐 run_kubectl_async）。"""
    return await asyncio.to_thread(run_kubectl_ssh, env_name, args, timeout, input)


class SshKubectlStream:
    """``stream_kubectl`` 的 SSH 版适配器（用于 kubectl logs -f 流式跟随）。

    暴露与 ``asyncio.subprocess.Process`` 兼容的最小接口：
    ``stdout.read(n)``（异步，阻塞到有数据或 EOF）/ ``kill()`` / ``wait()``。
    """

    def __init__(self, env_name, args):
        self._env_name = env_name
        self._args = args
        self._cli = None
        self._chan = None
        self._rc = None

    async def start(self):
        self._cli = await asyncio.to_thread(_get_client, self._env_name)

        def _open():
            import paramiko  # noqa: F401
            stdin, stdout, stderr = self._cli.exec_command(
                _remote_kubectl_command(self._args))
            chan = stdout.channel
            chan.set_combine_stderr(True)  # 对齐 stream_kubectl 的 stderr->STDOUT
            return chan

        self._chan = await asyncio.to_thread(_open)
        return self

    @property
    def stdout(self):
        """对齐 asyncio.subprocess.Process 的 ``proc.stdout.read(n)`` 调用形态。"""
        return self

    async def read(self, n=4096):
        """阻塞直到有数据 / 通道关闭；返回 bytes（EOF 返回 b''）。"""
        chan = self._chan
        if chan is None:
            return b""

        def _recv():
            while True:
                if chan.recv_ready():
                    try:
                        return chan.recv(n)
                    except Exception:
                        return b""
                if chan.closed or (chan.exit_status_ready()
                                   and not chan.recv_ready()):
                    return b""
                time.sleep(0.05)

        try:
            return await asyncio.to_thread(_recv)
        except Exception:
            return b""

    def kill(self):
        """关闭通道并丢弃复用连接（客户端断开 / 组件卸载时由调用方触发）。"""
        try:
            if self._chan is not None:
                self._chan.close()
        except Exception:
            pass
        _drop_client(self._env_name)

    async def wait(self):
        return 0


__all__ = [
    "SSH_MARKER_PREFIX",
    "is_ssh_target",
    "marker_env_name",
    "run_kubectl_ssh",
    "run_kubectl_ssh_async",
    "SshKubectlStream",
]
