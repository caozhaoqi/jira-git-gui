# -*- coding: utf-8 -*-
"""K8s 容器内文件操作路由：列表 / 读取 / 搜索 / 写入 / 上传 / 删除 / 建目录。

拆分自 ``api/routes_k8s.py``，业务子域：通过 ``kubectl exec`` 在容器内做文件系统操作，
支撑「日志 / 配置落地」等排障场景。

契约对齐说明（与 frontend/web-react K8sFiles 对齐，避免再次漂移）：
  - file/list   响应字段 ``entries``（每项含 ``type: 'dir'|'file'``），非 ``items``
  - file/read   请求字段 ``max_bytes``；响应含 ``is_binary`` / ``truncated``
  - file/search 请求字段 ``q``（兼容老 ``pattern``）；响应 ``results``（对象数组，非原始 grep 行）
  - file/write  接收明文 ``content``（前端保存编辑器内容）
  - file/upload 接收 base64 ``data``（兼容老 ``content``）
"""
import base64
import logging
import re
import shlex

from fastapi import APIRouter
from pydantic import BaseModel

from core import k8s_manager as _k8s_mgr
from core.k8s import run_kubectl as _k8s_run_kubectl

logger = logging.getLogger("api.routes_k8s_files")
router = APIRouter()

#: 分片下载单片默认 / 上限字节数。kubectl exec 每次建连有固定开销（约 0.5~2s），
#: 片太小会让 10MB 文件花掉几十秒；片太大则单次 JSON 过大、失败重试代价高。
#: 1MB 是实测较舒服的折中，前端可按网络情况调整。
DEFAULT_CHUNK = 1024 * 1024
MAX_CHUNK = 8 * 1024 * 1024


class K8sFileReq(BaseModel):
    """容器内文件操作统一请求体（list/read/search 共用）。"""
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = "/"
    pattern: str = ""        # 兼容老字段
    q: str = ""              # 搜索关键词（前端使用）
    max_lines: int = 2000
    max_bytes: int = 0       # 读取上限（前端使用）


def _resolve(env, namespace):
    """返回 (kc, namespace)，namespace 可被环境默认命名空间补全。"""
    kc, ns = _k8s_mgr.resolve_env_kubeconfig(env)
    if ns and not namespace:
        namespace = ns
    return kc, namespace


def _exec(env, pod, container, namespace, args, timeout=30, input=None):
    """在容器内执行命令，返回 (out, rc, err)。

    当 ``input`` 非 None（写文件/上传）时自动加 ``-i``，使 kubectl exec 真正消费 stdin。
    """
    kc, ns = _resolve(env, namespace)
    cmd = (["exec"] + (["-i"] if input is not None else []) + [pod]
           + (["-c", container] if container else [])
           + (["-n", ns] if ns else []) + ["--"] + args)
    try:
        return _k8s_run_kubectl(cmd, kc, timeout=timeout, input=input)
    except Exception as ex:
        return "", 1, getattr(ex, "message", None) or str(ex)


@router.post("/api/k8s/file/list")
async def api_k8s_file_list(body: K8sFileReq):
    """列出容器内某路径下的文件（含目录标记）。"""
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["ls", "-la", body.path])
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    lines = out.splitlines()[1:]  # 去掉 total 行
    entries = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 9:
            continue
        perm = parts[0]
        name = " ".join(parts[8:])
        size_raw = parts[4] if len(parts) > 4 else ""
        size = int(size_raw) if size_raw.isdigit() else size_raw
        entries.append({
            "name": name,
            "type": "dir" if perm.startswith("d") else "file",
            "size": size,
            "modtime": " ".join(parts[5:8]),
        })
    return {"ok": True, "entries": entries}


@router.post("/api/k8s/file/read")
async def api_k8s_file_read(body: K8sFileReq):
    """读取容器内文件内容（文本）。"""
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["cat", body.path], timeout=30)
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    is_binary = "\x00" in out
    if is_binary:
        return {"ok": True, "content": "", "is_binary": True, "truncated": False}
    truncated = False
    max_bytes = body.max_bytes or 0
    max_lines = body.max_lines or 0
    if max_bytes and len(out) > max_bytes:
        out = out[:max_bytes] + f"\n... (truncated, total {len(out)} bytes)"
        truncated = True
    elif max_lines and len(out.splitlines()) > max_lines:
        kept = out.splitlines()[:max_lines]
        out = "\n".join(kept) + f"\n... (truncated, total {len(out.splitlines())} lines)"
        truncated = True
    return {"ok": True, "content": out, "is_binary": False, "truncated": truncated}


class K8sFileDownloadReq(BaseModel):
    """分片下载请求：读取 ``[offset, offset+length)`` 区间的字节。"""
    env: str = ""
    pod: str = ""
    container: str = ""
    namespace: str = ""
    path: str = ""
    offset: int = 0
    length: int = DEFAULT_CHUNK


def _stat_script(path: str) -> str:
    """容器内取文件大小 + mtime（mtime 尽力而为，失败不影响下载）。

    * ``wc -c <`` 走重定向而非参数，避免文件名以 ``-`` 开头被当成选项；
    * ``wc`` 失败立刻 ``exit 1``，让 kubectl 的非零 rc 把 stderr（如
      "No such file or directory"）带回前端，而不是返回一个含糊的 size=0；
    * mtime 依次尝试 GNU ``stat -c %Y`` 与 BSD ``stat -f %m``（busybox 走前者），
      都失败也只是丢 mtime，不影响下载。
    """
    q = shlex.quote(path)
    return (
        f"wc -c < {q} || exit 1\n"
        f"stat -c %Y {q} 2>/dev/null || stat -f %m {q} 2>/dev/null || true"
    )


def _chunk_script(path: str, offset: int, length: int) -> str:
    """容器内读取 ``[offset, offset+length)`` 并编码成可安全穿过 kubectl stdout 的文本。

    * 首行输出 ``B64`` / ``HEX`` 标记本次用的编码，后端据此解码；
    * 优先 ``base64``（比 hex 省 50% 体积）；精简镜像没有 base64 时回退
      ``od -An -tx1`` 十六进制，让 distroless 这类无 coreutils 的镜像也能下载；
    * ``tr -d`` 去掉空白/换行，避免 kubectl 传输时被插入换行导致解码失败；
    * ``tail -c +N`` 的 N 从 1 开始，故 offset 要 +1。
    """
    q = shlex.quote(path)
    tail = f"tail -c +{int(offset) + 1} {q}"
    return (
        "if command -v base64 >/dev/null 2>&1; then\n"
        f"  printf 'B64\\n'; {tail} | head -c {int(length)} | base64 | tr -d '\\n'\n"
        "else\n"
        f"  printf 'HEX\\n'; {tail} | head -c {int(length)} | od -An -v -tx1 | tr -d ' \\n'\n"
        "fi"
    )


@router.post("/api/k8s/file/stat")
async def api_k8s_file_stat(body: K8sFileReq):
    """容器内文件大小与修改时间，供下载前算分片数与进度分母。"""
    if not (body.pod and body.path):
        return {"ok": False, "error": "pod / path 均为必填"}
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["sh", "-c", _stat_script(body.path)], timeout=30)
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "error": "无法获取文件大小（文件不存在或不可读）"}
    try:
        size = int(lines[0])
    except ValueError:
        return {"ok": False, "error": "无法解析文件大小：%s" % lines[0][:80]}
    mtime = None
    if len(lines) > 1:
        try:
            mtime = int(lines[1])
        except ValueError:
            mtime = None
    return {"ok": True, "size": size, "mtime": mtime}


@router.post("/api/k8s/file/download")
async def api_k8s_file_download(body: K8sFileDownloadReq):
    """分片读取容器内文件（**二进制安全**，供断点续传与进度条使用）。

    为什么不能再走 ``cat`` + 编辑器内容：编辑内容是截断过的文本（默认 200KB），
    二进制还会被直接拦下 —— 10MB 文件自然只能下来几百 KB。

    返回 ``{encoding, data, offset, length, requested, eof}``；无论容器内用的是
    base64 还是 hex，出参一律统一为 **base64**，前端只需处理一种编码。
    """
    if not (body.pod and body.path):
        return {"ok": False, "error": "pod / path 均为必填"}
    offset = max(0, int(body.offset or 0))
    length = int(body.length or DEFAULT_CHUNK)
    if length <= 0:
        length = DEFAULT_CHUNK
    length = min(length, MAX_CHUNK)

    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["sh", "-c", _chunk_script(body.path, offset, length)],
                         timeout=60)
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}

    # 首行是编码标记，其后才是数据
    head, _, payload = out.partition("\n")
    encoding = head.strip().upper()
    data = "".join(payload.split())   # 去掉所有空白，防止传输中被插入换行
    if encoding == "B64":
        try:
            raw = base64.b64decode(data)
        except Exception as ex:
            return {"ok": False, "error": "base64 解码失败：%s" % ex}
    elif encoding == "HEX":
        try:
            raw = bytes.fromhex(data)
        except ValueError as ex:
            return {"ok": False, "error": "hex 解码失败：%s" % ex}
    else:
        return {"ok": False, "error": "无法识别的编码标记：%r" % head[:40]}

    return {
        "ok": True,
        "data": base64.b64encode(raw).decode("ascii"),
        "offset": offset,
        "length": len(raw),
        "requested": length,
        "eof": len(raw) < length,
    }


@router.post("/api/k8s/file/search")
async def api_k8s_file_search(body: K8sFileReq):
    """在容器内按文本模式搜索文件内容（grep -rn），结果解析为对象数组。

    为避免在大目录（/、/proc、/sys 等）递归 grep 触发 kubectl 超时：
      - 排除常见虚拟/巨型目录（proc/sys/dev/.git/node_modules/__pycache__）
      - 经 head 限制输出行数（命中即停，grep 收 SIGPIPE 提前退出）
      - 超时放大到 120s，超时返回友好提示而非裸 "kubectl timed out"
    """
    pattern = body.pattern or body.q or ""
    if not pattern:
        return {"ok": False, "error": "pattern(q) 为必填"}
    # 排除项：虚拟文件系统 + 常见体积巨大的依赖/缓存目录
    excludes = ["proc", "sys", "dev", ".git", "node_modules", "__pycache__"]
    exclude_args = " ".join(f"--exclude-dir={d}" for d in excludes)
    # 注意：grep 走管道到 head，不使用 pipefail（容器 sh 不一定支持），
    # 故正常/无匹配均表现为管道 rc=0；仅 kubectl 整体超时才会得到 rc=124。
    shell_cmd = (
        f"grep -rn {exclude_args} -- {shlex.quote(pattern)} "
        f"{shlex.quote(body.path)} 2>/dev/null | head -n 2000"
    )
    out, rc, err = _exec(body.env, body.pod, body.container, body.namespace,
                         ["sh", "-c", shell_cmd], timeout=120)
    if rc == 124 or "timed out" in (err or ""):
        return {"ok": False, "error": "搜索超时：建议指定更具体的目录（避免从 / 全量递归）或缩短关键词后重试"}
    if rc not in (0, 1):
        return {"ok": False, "error": err.strip()[:300]}
    results = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        m = re.match(r"^(.+?):(\d+):(.*)$", ln)
        if m:
            results.append({"path": m.group(1), "line": int(m.group(2)), "snippet": m.group(3)})
        else:
            results.append({"path": body.path, "line": 0, "snippet": ln})
    truncated = len(results) >= 2000
    return {"ok": True, "results": results, "total": len(results), "truncated": truncated}


@router.post("/api/k8s/file/write")
async def api_k8s_file_write(body: dict):
    """把编辑器中的明文内容写回容器文件（kubectl exec cat > path）。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    content = body.get("content", "")
    if not (pod and path):
        return {"ok": False, "error": "pod / path 均为必填"}
    if content is None:
        return {"ok": False, "error": "content 为必填"}
    out, rc, err = _exec(env, pod, container, namespace,
                         ["sh", "-c", f"cat > {shlex.quote(path)}"],
                         timeout=60, input=content.encode("utf-8"))
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


@router.post("/api/k8s/file/upload")
async def api_k8s_file_upload(body: dict):
    """上传本地内容到容器内文件（base64 编码内容经 stdin 写入）。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    # 前端上传走 data（base64），兼容老 content 字段
    content_b64 = body.get("data")
    if not content_b64:
        content_b64 = body.get("content", "")
    if not (pod and path and content_b64):
        return {"ok": False, "error": "pod / path / data(content) 均为必填"}
    try:
        raw = base64.b64decode(content_b64)
    except Exception as ex:
        return {"ok": False, "error": f"content 不是合法 base64：{ex}"}
    out, rc, err = _exec(env, pod, container, namespace,
                         ["sh", "-c", f"cat > {shlex.quote(path)}"],
                         timeout=60, input=raw)
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


@router.post("/api/k8s/file/delete")
async def api_k8s_file_delete(body: dict):
    """删除容器内文件。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    if not (pod and path):
        return {"ok": False, "error": "pod / path 均为必填"}
    out, rc, err = _exec(env, pod, container, namespace, ["rm", "-f", path])
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}


@router.post("/api/k8s/file/mkdir")
async def api_k8s_file_mkdir(body: dict):
    """在容器内创建目录。"""
    env = body.get("env", "")
    pod = body.get("pod", "")
    container = body.get("container", "")
    namespace = body.get("namespace", "")
    path = body.get("path", "")
    if not (pod and path):
        return {"ok": False, "error": "pod / path 均为必填"}
    out, rc, err = _exec(env, pod, container, namespace, ["mkdir", "-p", path])
    if rc != 0:
        return {"ok": False, "error": err.strip()[:300]}
    return {"ok": True}
