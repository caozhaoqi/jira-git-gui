# -*- coding: utf-8 -*-
"""K8s 文件「分片下载 / 断点续传」端到端测试（无需真实集群）。

背景与被测的根因
----------------
前端原来的「下载」按钮是直接 `new Blob([editContent])`：editContent 来自
`/api/k8s/file/read`，后端默认按 `max_bytes=200000` 截断，且二进制文件会被
`is_binary` 直接拦下 —— 于是 10MB 的文件下载下来只有几百 KB。

新链路是 `/api/k8s/file/stat` + `/api/k8s/file/download`：后者在容器内用
`tail -c +N | head -c L | base64` 取 `[offset, offset+length)` 的原始字节，
二进制安全、可按 offset 续传。

测试手法
--------
把 `routes_k8s_files._exec` 替换成「在本地真的跑一遍这段 shell 脚本」，
于是脚本生成、容器内编码、后端解码、base64/hex 归一化全都是真实代码路径，
只有 kubectl 建连这一层被跳过。

运行：./venv/bin/python -m pytest tests/test_k8s_file_download.py -s
"""
import asyncio
import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.k8s.routes_k8s_files as _rt  # noqa: E402


# ---- 替身：把「容器内执行」落到本机 sh -c ------------------------------------ #
def _local_exec(env, pod, container, namespace, args, timeout=30, input=None):
    """在本机执行 args，模拟 kubectl exec。

    仅支持 `sh -c <script>` 形式（stat / download 两条链路都用它）。
    脚本里的文件路径就是本机临时目录里的真实路径，因此能验证真实字节。
    """
    if len(args) >= 3 and args[0] == "sh" and args[1] == "-c":
        argv = ["sh", "-c", args[2]]      # stat / download / search 走脚本
    else:
        argv = list(args)                  # cat / ls 等直接执行（read 链路用到）
    try:
        p = subprocess.run(
            argv,
            input=input,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", 124, "kubectl timed out"
    return (
        p.stdout.decode("utf-8", "replace"),
        p.returncode,
        p.stderr.decode("utf-8", "replace"),
    )


_CALLS = []


def _recording_exec(env, pod, container, namespace, args, timeout=30, input=None):
    _CALLS.append(list(args))
    return _local_exec(env, pod, container, namespace, args, timeout=timeout, input=input)


def install(monkeypatch, record=False):
    monkeypatch.setattr(_rt, "_exec", _recording_exec if record else _local_exec)


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


# ---- 用例 ------------------------------------------------------------------- #
def test_stat_returns_size_and_mtime(monkeypatch, tmp_path):
    install(monkeypatch)
    f = tmp_path / "big.bin"
    f.write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    resp = asyncio.run(_rt.api_k8s_file_stat(
        _rt.K8sFileReq(env="dev", pod="p1", path=str(f))))
    assert resp["ok"], resp
    assert resp["size"] == f.stat().st_size, "size 不符：%r vs %r" % (resp["size"], f.stat().st_size)
    assert resp["mtime"] == int(f.stat().st_mtime), "mtime 不符"
    print("[OK] stat -> size=%d mtime=%s" % (resp["size"], resp["mtime"]))


def test_stat_missing_file_reports_stderr(monkeypatch, tmp_path):
    """文件不存在时必须失败，且把容器的真实报错带回前端（不能谎报 size=0）。

    回归：早期脚本以 `|| true` 收尾，导致 wc 失败也返回 rc=0，
    前端只会看到一个没头没尾的「无法获取文件大小」。
    """
    install(monkeypatch)
    resp = asyncio.run(_rt.api_k8s_file_stat(
        _rt.K8sFileReq(env="dev", pod="p1", path=str(tmp_path / "nope.bin"))))
    assert not resp.get("ok"), "缺失文件应返回 ok=False：%r" % resp
    err = resp.get("error") or ""
    assert "No such file" in err or "not found" in err.lower() or "无法" in err, \
        "错误信息应带上容器真实原因：%r" % err
    print("[OK] stat 缺失文件 ->", err[:80])


def test_chunked_download_reassembles_10mb_exactly(monkeypatch, tmp_path):
    """核心回归：10MB 文件按 1MB 分片下载，重组后必须与源文件逐字节一致。

    这正是用户报的现象（10MB 只下来几百 KB）。旧链路会在 200KB 处截断，
    本用例若回到旧实现必然失败。
    """
    install(monkeypatch)
    src = tmp_path / "big.bin"
    blob = os.urandom(10 * 1024 * 1024)
    src.write_bytes(blob)

    CHUNK = 1024 * 1024
    parts = []
    offset = 0
    while True:
        resp = asyncio.run(_rt.api_k8s_file_download(
            _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src),
                                   offset=offset, length=CHUNK)))
        assert resp["ok"], resp
        assert resp["offset"] == offset, "回包 offset 应与请求一致"
        raw = base64.b64decode(resp["data"])
        assert len(raw) == resp["length"], "length 字段与实际字节数不符"
        if not raw:
            break
        parts.append(raw)
        offset += len(raw)
        if resp["eof"]:
            break
    got = b"".join(parts)
    assert len(got) == len(blob), "重组长度不符：%d vs %d" % (len(got), len(blob))
    assert md5(got) == md5(blob), "重组内容 md5 不符（分片/解码有损）"
    print("[OK] 10MB 分片重组一致 -> %d bytes md5=%s" % (len(got), md5(got)))


def test_resume_from_middle_offset(monkeypatch, tmp_path):
    """断点续传：从 6MB 处续传，拿到的必须正好是源文件 [6MB:] 的尾巴。"""
    install(monkeypatch)
    src = tmp_path / "big.bin"
    blob = os.urandom(10 * 1024 * 1024)
    src.write_bytes(blob)

    RESUME = 6 * 1024 * 1024
    CHUNK = 1024 * 1024
    parts = []
    offset = RESUME
    while True:
        resp = asyncio.run(_rt.api_k8s_file_download(
            _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src),
                                   offset=offset, length=CHUNK)))
        assert resp["ok"], resp
        raw = base64.b64decode(resp["data"])
        if not raw:
            break
        parts.append(raw)
        offset += len(raw)
        if resp["eof"]:
            break
    got = b"".join(parts)
    assert got == blob[RESUME:], "续传内容与源文件尾部不一致（offset 语义有误）"
    print("[OK] 从 %d 续传 -> %d bytes 与源文件尾部一致" % (RESUME, len(got)))


def test_binary_safe_all_256_byte_values(monkeypatch, tmp_path):
    """二进制安全：含 NUL 与全部 256 种字节值的文件必须原样回传。

    旧链路走 `cat` + Python str，NUL 会被判为二进制直接拒绝，
    非 UTF-8 字节更会被 replace 成 U+FFFD 永久损坏。
    """
    install(monkeypatch)
    src = tmp_path / "bytes.bin"
    blob = bytes(range(256)) * 400 + b"\x00\x01\x00\xff"
    src.write_bytes(blob)
    resp = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src),
                               offset=0, length=len(blob) + 10)))
    assert resp["ok"], resp
    got = base64.b64decode(resp["data"])
    assert got == blob, "二进制内容被损坏（长度 %d vs %d）" % (len(got), len(blob))
    assert resp["eof"] is True, "请求长度超过文件长度时应标记 eof"
    print("[OK] 二进制安全 -> %d bytes 全字节值一致" % len(got))


def test_eof_semantics(monkeypatch, tmp_path):
    """eof = 本片长度 < 请求长度；offset 越过 EOF 时返回 0 字节且 eof=True。"""
    install(monkeypatch)
    src = tmp_path / "small.bin"
    src.write_bytes(b"A" * 1500)
    r1 = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src), offset=0, length=1024)))
    assert r1["length"] == 1024 and r1["eof"] is False, "首片满长度，不应 eof"
    r2 = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src), offset=1024, length=1024)))
    assert r2["length"] == 476 and r2["eof"] is True, "末片不足长度，应 eof"
    r3 = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src), offset=9999, length=1024)))
    assert r3["length"] == 0 and r3["eof"] is True, "越过 EOF 应返回 0 字节 + eof"
    print("[OK] eof 语义 -> %s / %s / %s" % (r1["eof"], r2["eof"], r3["eof"]))


def test_empty_file(monkeypatch, tmp_path):
    install(monkeypatch)
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")
    resp = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src), offset=0, length=1024)))
    assert resp["ok"], resp
    assert resp["length"] == 0 and resp["eof"] is True, "空文件应返回 0 字节 + eof"
    print("[OK] 空文件 -> length=0 eof=True")


def test_hex_fallback_is_normalized_to_base64(monkeypatch, tmp_path):
    """精简镜像没有 base64 时容器走 od 十六进制；出参仍必须统一成 base64。

    前端只需处理一种编码，归一化在后端完成。
    """
    payload = bytes([0x00, 0x01, 0xFE, 0xFF, 0x41, 0x0A, 0x0D])

    def _hex_only(env, pod, container, namespace, args, timeout=30, input=None):
        return "HEX\n" + payload.hex() + "\n", 0, ""

    monkeypatch.setattr(_rt, "_exec", _hex_only)
    resp = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path="/x", offset=0, length=1024)))
    assert resp["ok"], resp
    assert base64.b64decode(resp["data"]) == payload, "hex 回退解码结果与原始字节不符"
    assert resp["length"] == len(payload)
    print("[OK] hex 回退 -> base64 归一化，%d bytes" % len(payload))


def test_unknown_encoding_marker_rejected(monkeypatch):
    """容器内脚本异常（首行不是 B64/HEX）时不能把垃圾当数据返回。"""
    def _garbage(env, pod, container, namespace, args, timeout=30, input=None):
        return "command not found: base64\n", 0, ""

    monkeypatch.setattr(_rt, "_exec", _garbage)
    resp = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path="/x", offset=0, length=1024)))
    assert not resp["ok"], "无法识别的编码标记应返回 ok=False"
    assert "编码标记" in (resp.get("error") or ""), "错误信息应说明原因：%r" % resp
    print("[OK] 未知编码标记 ->", resp["error"])


def test_length_is_clamped_to_max_chunk(monkeypatch, tmp_path):
    """单片上限保护：请求 100MB 也应被 clamp 到 MAX_CHUNK，避免单次 JSON 过大。"""
    install(monkeypatch, record=True)
    _CALLS.clear()
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(12 * 1024 * 1024))
    resp = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src), offset=0, length=100 * 1024 * 1024)))
    assert resp["ok"], resp
    assert resp["length"] == _rt.MAX_CHUNK, "应被 clamp 到 MAX_CHUNK=%d，实际 %d" % (_rt.MAX_CHUNK, resp["length"])
    assert resp["requested"] == _rt.MAX_CHUNK
    assert "head -c %d" % _rt.MAX_CHUNK in _CALLS[0][2], "下发脚本应带上 clamp 后的长度"
    print("[OK] length clamp -> %d bytes" % resp["length"])


def test_path_with_spaces_and_shell_metachars(monkeypatch, tmp_path):
    """路径含空格 / 引号 / $ 时必须被 shlex.quote 正确转义（不能命令注入 / 报错）。"""
    install(monkeypatch)
    tricky = "weird name $(whoami) 'q' \"d\".bin"
    src = tmp_path / tricky
    blob = b"payload\x00\xff" * 100
    src.write_bytes(blob)
    resp = asyncio.run(_rt.api_k8s_file_download(
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=str(src), offset=0, length=1024 * 1024)))
    assert resp["ok"], "含特殊字符的路径应能正常下载：%r" % resp
    assert base64.b64decode(resp["data"]) == blob, "内容不符"
    # stat 同样要能扛住
    st = asyncio.run(_rt.api_k8s_file_stat(
        _rt.K8sFileReq(env="dev", pod="p1", path=str(src))))
    assert st.get("ok") and st["size"] == len(blob), "stat 特殊路径失败：%r" % st
    print("[OK] 特殊字符路径 -> %d bytes" % len(blob))


def test_missing_pod_or_path_rejected(monkeypatch):
    install(monkeypatch)
    for req in (
        _rt.K8sFileDownloadReq(env="dev", pod="", path="/tmp/x"),
        _rt.K8sFileDownloadReq(env="dev", pod="p1", path=""),
    ):
        resp = asyncio.run(_rt.api_k8s_file_download(req))
        assert not resp["ok"] and "必填" in (resp.get("error") or ""), "缺参应被拒：%r" % resp
    for req in (
        _rt.K8sFileReq(env="dev", pod="", path="/tmp/x"),
        _rt.K8sFileReq(env="dev", pod="p1", path=""),
    ):
        resp = asyncio.run(_rt.api_k8s_file_stat(req))
        assert not resp["ok"] and "必填" in (resp.get("error") or ""), "缺参应被拒：%r" % resp
    print("[OK] 缺参校验通过")


def test_read_still_truncates_at_max_bytes(monkeypatch, tmp_path):
    """存档旧行为：file/read 确实按 max_bytes 截断 —— 这就是「只下来几百 KB」的根因。

    保留此用例是为了说明：下载必须走 /download，绝不能复用 /read 的内容。
    """
    install(monkeypatch)
    src = tmp_path / "big.txt"
    src.write_bytes(b"x" * (2 * 1024 * 1024))
    resp = asyncio.run(_rt.api_k8s_file_read(
        _rt.K8sFileReq(env="dev", pod="p1", path=str(src), max_bytes=200000)))
    assert resp["ok"], resp
    assert resp["truncated"] is True, "应标记为已截断"
    assert len(resp["content"]) <= 200000 + 64, "内容应被截断到 max_bytes 附近"
    print("[OK] read 截断行为确认 -> content=%d bytes truncated=%s"
          % (len(resp["content"]), resp["truncated"]))


def test_read_rejects_binary(monkeypatch, tmp_path):
    """存档旧行为：含 NUL 的文件被 is_binary 直接拦下，连文本都拿不到。"""
    install(monkeypatch)
    src = tmp_path / "b.bin"
    src.write_bytes(b"\x00\x01\x02")
    resp = asyncio.run(_rt.api_k8s_file_read(
        _rt.K8sFileReq(env="dev", pod="p1", path=str(src))))
    assert resp["ok"] and resp["is_binary"] is True and resp["content"] == "", \
        "二进制应被标记且不返回内容：%r" % resp
    print("[OK] read 二进制拦截确认 -> is_binary=True")


def test_http_roundtrip_via_testclient(monkeypatch, tmp_path):
    """走完整 HTTP 栈（路由挂载 / pydantic 校验 / JSON 序列化）再验一次字节一致性。

    上面的用例是直接 await 路由函数，绕过了 FastAPI；这里补上真实 HTTP 通道，
    防止「函数对、路由没挂上或字段名写错」这类只在联调才暴露的问题。
    """
    try:
        from fastapi.testclient import TestClient
        from api.server import app
    except Exception as ex:  # pragma: no cover - 环境缺依赖时跳过
        print("[SKIP] HTTP 往返测试：%s" % ex)
        return

    install(monkeypatch)
    src = tmp_path / "big.bin"
    blob = os.urandom(4 * 1024 * 1024 + 999)
    src.write_bytes(blob)

    client = TestClient(app)
    r = client.post("/api/k8s/file/stat",
                    json={"env": "dev", "pod": "p1", "path": str(src)})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["size"] == len(blob), "stat 经 HTTP 后 size 不符：%r" % r.json()

    CHUNK = 1024 * 1024
    parts = []
    offset = 0
    while True:
        r = client.post("/api/k8s/file/download",
                        json={"env": "dev", "pod": "p1", "path": str(src),
                              "offset": offset, "length": CHUNK})
        assert r.status_code == 200, r.text[:200]
        d = r.json()
        assert d["ok"], d
        raw = base64.b64decode(d["data"])
        if not raw:
            break
        parts.append(raw)
        offset += len(raw)
        if d["eof"]:
            break
    assert md5(b"".join(parts)) == md5(blob), "经 HTTP 重组后 md5 不符"
    print("[OK] HTTP 往返 -> %d bytes md5 一致" % len(blob))
