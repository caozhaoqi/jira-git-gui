# -*- coding: utf-8 -*-
"""SSH kubectl 离线冒烟：伪造 paramiko 客户端，验证链路与生命周期。"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.k8s import ssh_exec
from core.k8s.env import add_or_update_env, list_envs, ENV_CONFIG_PATH
from core.k8s.exec import resolve_env_kubeconfig
from core.k8s.pods import run_kubectl_env
from core.k8s import kubectl as kubectl_mod


class FakeStdout:
    def __init__(self, chan, data):
        self.channel = chan
        self._data = data

    def read(self):
        return self._data


class FakeStderr:
    def __init__(self, chan, data):
        self.channel = chan
        self._data = data

    def read(self):
        return self._data


class FakeStdin:
    def __init__(self, chan):
        self.channel = chan
        self.written = b""

    def write(self, data):
        self.written = data if isinstance(data, bytes) else data.encode()


class FakeChannel:
    """一次性命令：立即完成（exit_status_ready 异步为 True，模拟真实 paramiko）。"""

    def __init__(self, rc=0):
        self._rc = rc
        self.closed = False

    def shutdown_write(self):
        pass

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self._rc

    def set_combine_stderr(self, v):
        pass

    def close(self):
        self.closed = True


class FakeClient:
    """伪造 paramiko.SSHClient：记录命令，返回预置输出。"""

    calls = []
    out, err, rc = "hello", "", 0

    def get_transport(self):
        return types.SimpleNamespace(is_active=lambda: True)

    def exec_command(self, cmd, timeout=None):
        FakeClient.calls.append(cmd)
        chan = FakeChannel(FakeClient.rc)
        return FakeStdin(chan), FakeStdout(chan, FakeClient.out.encode()), \
            FakeStderr(chan, FakeClient.err.encode())

    def close(self):
        pass


def main():
    ok = lambda name: print("  ✓", name)

    # 1) 环境写入 / 读取（用临时配置文件，不碰用户真实配置）
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "k8s_envs.json"
    orig = ssh_exec.__dict__  # noqa
    import core.k8s.env as env_mod
    env_mod.ENV_CONFIG_PATH = tmp
    add_or_update_env("ssh-test", label="SSH测试", kubeconfig="",
                      namespace="demo", ssh_host="10.6.6.9", ssh_port="2222",
                      ssh_user="ops", ssh_password="secret")
    envs = {e["name"]: e for e in list_envs()}
    e = envs["ssh-test"]
    assert e["ssh_host"] == "10.6.6.9" and e["ssh_port"] == "2222" \
        and e["ssh_user"] == "ops" and e["ssh_password"] == "secret", e
    ok("env.py SSH 字段落盘/读取")

    # 2) 标记识别
    assert ssh_exec.is_ssh_target("ssh://ssh-test")
    assert not ssh_exec.is_ssh_target("/path/kubeconfig")
    assert not ssh_exec.is_ssh_target(None)
    assert ssh_exec.marker_env_name("ssh://ssh-test") == "ssh-test"
    ok("ssh:// 标记识别/解析")

    # 3) resolve_env_kubeconfig 返回标记
    kc, ns = resolve_env_kubeconfig("ssh-test")
    assert kc == "ssh://ssh-test" and ns == "demo", (kc, ns)
    ok("resolve_env_kubeconfig → ssh:// 标记")

    # 4) run_kubectl_env / run_kubectl 路由到 SSH（伪造客户端）
    ssh_exec._get_client = lambda name: FakeClient()
    FakeClient.calls = []
    out, rc, err = run_kubectl_env("ssh-test", ["get", "pods", "-n", "demo"])
    assert out == "hello" and rc == 0, (out, rc, err)
    assert FakeClient.calls and "kubectl" in FakeClient.calls[0] \
        and "'get'" in FakeClient.calls[0] or "get" in FakeClient.calls[0], FakeClient.calls
    assert "--kubeconfig" not in FakeClient.calls[0]
    ok("run_kubectl_env SSH 路由（远端命令含 kubectl，无 --kubeconfig）")

    # 5) run_kubectl(marker) / async
    out, rc, err = kubectl_mod.run_kubectl(["get", "events"], kubeconfig="ssh://ssh-test")
    assert rc == 0 and out == "hello"
    out, rc, err = asyncio.run(kubectl_mod.run_kubectl_async(
        ["logs", "p1"], kubeconfig="ssh://ssh-test", timeout=30))
    assert rc == 0 and out == "hello"
    ok("run_kubectl / run_kubectl_async SSH 委托")

    # 6) stdin 透传（apply 场景）
    stdin_holder = {}
    orig_exec = FakeClient.exec_command
    def exec_cap(self, cmd, timeout=None):
        s, o, e2 = orig_exec(self, cmd, timeout)
        stdin_holder["stdin"] = s
        return s, o, e2
    FakeClient.exec_command = exec_cap
    out, rc, err = kubectl_mod.run_kubectl(
        ["apply", "-f", "-"], kubeconfig="ssh://ssh-test", input=b"kind: Pod")
    assert stdin_holder["stdin"].written == b"kind: Pod"
    FakeClient.exec_command = orig_exec
    ok("stdin（kubectl apply -f -）透传远端")

    # 7) 流式适配器（logs -f）
    class FakeStreamChan:
        closed = False
        def __init__(self):
            self._chunks = [b"line1\n", b"line2\n"]
            self._done = False
        def recv_ready(self):
            return bool(self._chunks)
        def recv(self, n):
            return self._chunks.pop(0)
        def exit_status_ready(self):
            return not self._chunks
        def set_combine_stderr(self, v):
            pass
        def close(self):
            self.closed = True

    class FakeStreamClient:
        def get_transport(self):
            return types.SimpleNamespace(is_active=lambda: True)
        def exec_command(self, cmd, timeout=None):
            chan = FakeStreamChan()
            return None, types.SimpleNamespace(channel=chan), None
        def close(self):
            pass

    async def stream_case():
        cli = FakeStreamClient()
        ssh_exec._get_client = lambda name: cli
        s = await ssh_exec.SshKubectlStream("ssh-test", ["logs", "-f", "p1"]).start()
        c1 = await s.stdout.read(4096)  # 与路由 gen() 的 proc.stdout.read 形态一致
        c2 = await s.stdout.read(4096)
        c3 = await s.stdout.read(4096)  # EOF
        s.kill()
        return c1, c2, c3

    c1, c2, c3 = asyncio.run(stream_case())
    assert c1 == b"line1\n" and c2 == b"line2\n" and c3 == b"", (c1, c2, c3)
    ok("SshKubectlStream 流式读取 + EOF + kill")

    # 8) 清理临时配置
    tmp.unlink(missing_ok=True)
    print("\nALL SSH OFFLINE TESTS PASSED")


if __name__ == "__main__":
    main()
