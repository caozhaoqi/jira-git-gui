# -*- coding: utf-8 -*-
"""Kubernetes 多环境运维核心逻辑（无 PyQt 依赖，可在后台线程调用）。

在 :mod:`core.k8s_snapshot` 的快照能力之上，扩展：

* **多环境管理**：开发 / 测试 / 正式 三套（可增删），每套保存独立的
  kubeconfig 路径、context、默认命名空间、内网探测主机列表。
  配置持久化到 ``~/.config/jira-git-gui/k8s_envs.json``。
* **Pod / 资源 YAML 管理**：``get_resource_yaml``（获取）/ ``apply_yaml_content``（修改后上传）。
* **网络检测**：``detect_network`` 一次性给出
  kubectl 客户端、kubeconfig 文件、集群连通、内网(API Server TCP)、
  用户自定义内网主机、外网探测 等检查结果，并判定当前是否处于该环境的内网。

所有对外函数均接受 ``on_log`` 回调，配置类错误抛 :class:`core.errors.UserError`，
其余异常原样上抛。
"""
import base64
import json
import os
import re
import shlex
import shutil
import socket
import subprocess as _subprocess
import tempfile
import time
from pathlib import Path

import yaml
from core.errors import UserError
from core.k8s_snapshot import run_kubectl

# 环境配置存储位置（用户级，不随项目提交）
ENV_CONFIG_PATH = Path.home() / ".config" / "jira-git-gui" / "k8s_envs.json"

# 三套默认环境；开发环境预填 ~/Downloads/kubeconfig.txt 作为示例
DEFAULT_ENV_SEED = {
    "environments": {
        "dev": {
            "label": "开发",
            "kubeconfig": str(Path.home() / "Downloads" / "kubeconfig.txt"),
            "context": "",
            "namespace": "default",
            "intranet_hosts": [],
        },
        "test": {
            "label": "测试",
            "kubeconfig": "",
            "context": "",
            "namespace": "default",
            "intranet_hosts": [],
        },
        "prod": {
            "label": "正式",
            "kubeconfig": "",
            "context": "",
            "namespace": "default",
            "intranet_hosts": [],
        },
    },
    "current": "dev",
}


# ===================================================================== 环境管理
def _seed_defaults():
    save_envs(DEFAULT_ENV_SEED)
    return DEFAULT_ENV_SEED


def load_envs():
    """返回环境配置 dict：{environments:{name:{...}}, current:name}。"""
    if ENV_CONFIG_PATH.exists():
        try:
            data = json.loads(ENV_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("environments"), dict) \
                and data["environments"]:
            cur = data.get("current")
            if cur not in data["environments"]:
                data["current"] = next(iter(data["environments"]))
            return data
    return _seed_defaults()


def save_envs(data):
    ENV_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_envs():
    """返回 [(name, label, is_current), ...]。"""
    data = load_envs()
    cur = data.get("current")
    return [(n, e.get("label", n), n == cur)
            for n, e in data["environments"].items()]


def get_env(name=None):
    """解析环境名 -> (name, env_dict)。name 为 None 取 current。"""
    data = load_envs()
    if name is None:
        name = data.get("current")
    env = data["environments"].get(name)
    if env is None:
        raise UserError("未找到环境 '%s'，请先在「环境管理」中配置。" % name)
    return name, env


def add_or_update_env(name, label=None, kubeconfig=None, context=None,
                      namespace=None, intranet_hosts=None):
    data = load_envs()
    env = data["environments"].get(name, {})
    env["label"] = label if label not in (None, "") else (env.get("label") or name)
    if kubeconfig is not None:
        env["kubeconfig"] = kubeconfig
    if context is not None:
        env["context"] = context
    if namespace is not None:
        env["namespace"] = namespace
    if intranet_hosts is not None:
        env["intranet_hosts"] = intranet_hosts
    data["environments"][name] = env
    save_envs(data)
    return data


def set_current_env(name):
    data = load_envs()
    if name not in data["environments"]:
        raise UserError("环境 '%s' 不存在。" % name)
    data["current"] = name
    save_envs(data)
    return data


def delete_env(name):
    data = load_envs()
    if name in data["environments"]:
        del data["environments"][name]
        if data.get("current") == name:
            data["current"] = next(iter(data["environments"]), None)
        save_envs(data)
    return data


def _env_kubectl_prefix(env):
    args = []
    if env.get("kubeconfig"):
        args += ["--kubeconfig", env["kubeconfig"]]
    if env.get("context"):
        args += ["--context", env["context"]]
    return args


def run_kubectl_env(env_name, args, timeout=60):
    """以指定环境身份执行 kubectl。返回 (stdout, rc, stderr)。"""
    _, env = get_env(env_name)
    # 合并 kubectl 前缀参数和子命令参数，传给 run_kubectl
    full_args = _env_kubectl_prefix(env) + list(args)
    return run_kubectl(full_args, kubeconfig=None, timeout=timeout)


# ===================================================================== 资源 YAML
def list_pods(env_name, selector=None, namespace=None):
    """列出 pod（精简信息），用于 YAML 管理界面的快速选择。"""
    args = ["get", "pods", "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    if selector:
        args += ["-l", selector]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("列出 Pod 失败：%s" % err.strip()[:400])
    try:
        items = json.loads(out).get("items", [])
    except Exception:
        raise UserError("kubectl 返回非 JSON（可能未连接集群）。")
    res = []
    for it in items:
        meta = it.get("metadata", {})
        st = it.get("status", {})
        cs = st.get("containerStatuses", []) or []
        res.append({
            "name": meta.get("name", "?"),
            "namespace": meta.get("namespace", ""),
            "phase": st.get("phase", ""),
            "restarts": max((c.get("restartCount", 0) for c in cs), default=0),
            "node": it.get("spec", {}).get("nodeName", ""),
        })
    return res


# 顶层始终剔除（运行时状态，不可被 apply 覆盖）
_TOP_DENY_KEYS = {"status"}

# metadata 下由服务端托管的字段（每次 get 都会变，编辑/回传时应剔除，避免无意义的 diff 与冲突）
_META_DENY_KEYS = {
    "resourceVersion", "uid", "creationTimestamp", "generation",
    "selfLink", "managedFields", "ownerReferences",
}

# metadata.annotations 中由 kubectl apply 写入、不应再次 apply 的字段
_META_DENY_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
}

# 部分资源在 spec 下由集群自动分配、不可直接 apply 的字段（按 kind 清理）
_SPEC_DENY_BY_KIND = {
    "Service": {"clusterIP", "clusterIPs", "healthCheckNodePort"},
    "Endpoints": {"subset"},          # 端点由控制器维护
    "Pod": {"nodeName"},              # 调度后由 kubelet 写入
    "PersistentVolumeClaim": {"volumeName"},
    "PodDisruptionBudget": set(),
}


def clean_manifest_obj(obj):
    """递归清理从 ``kubectl get`` 出来的资源对象，剔除服务端托管字段，
    使其变成可直接编辑 / ``kubectl apply`` 的干净清单。

    仅删除「服务端写入、客户端不应管控」的字段，保留 spec / labels /
    annotations（除 last-applied 外）等用户侧内容，因此清理后仍可安全回传。
    """
    if not isinstance(obj, dict):
        return obj
    # 顶层运行时状态
    for k in _TOP_DENY_KEYS:
        obj.pop(k, None)
    # metadata
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        for k in list(meta.keys()):
            if k in _META_DENY_KEYS:
                meta.pop(k, None)
        ann = meta.get("annotations")
        if isinstance(ann, dict):
            for a in list(ann.keys()):
                if a in _META_DENY_ANNOTATIONS:
                    ann.pop(a, None)
            if not ann:
                meta.pop("annotations", None)
    # spec 按 kind 清理自动分配字段
    kind = obj.get("kind")
    spec = obj.get("spec")
    if isinstance(spec, dict) and kind in _SPEC_DENY_BY_KIND:
        for k in _SPEC_DENY_BY_KIND[kind]:
            spec.pop(k, None)
    return obj


def get_resource_yaml(env_name, kind, name, namespace=None, clean=True, raw=False):
    """获取资源 YAML。

    * ``raw=True`` 或 ``clean=False``：直接返回 ``kubectl get ... -o yaml``
      原始文本（含 status / 服务端字段）。
    * 默认 ``clean=True``：走 ``-o json`` 解析后剔除服务端托管字段，再以
      **稳定顺序**（sort_keys=False，保留 apiVersion/kind/metadata/spec 原序）
      重新序列化为「可编辑、可二次 apply」的干净 YAML。
    """
    if raw or not clean:
        args = ["get", kind, name, "-o", "yaml"]
    else:
        args = ["get", kind, name, "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("获取 %s/%s 失败：%s" % (kind, name, err.strip()[:400]))
    if raw or not clean:
        return out
    try:
        obj = json.loads(out)
    except Exception:
        raise UserError("kubectl 返回非 JSON（可能未连接集群或资源不存在）。")
    clean_manifest_obj(obj)
    return yaml.safe_dump(
        obj, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def apply_yaml_content(env_name, content, namespace=None):
    """把 YAML 内容修改后上传（kubectl apply -f）。返回 (stdout, stderr)。"""
    if not content or not content.strip():
        raise UserError("YAML 内容为空，无法上传。")
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="k8s_apply_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        args = ["apply", "-f", path, "--record=false"]
        if namespace:
            args += ["-n", namespace]
        elif (env := get_env(env_name)[1]).get("namespace"):
            args += ["-n", env["namespace"]]
        out, rc, err = run_kubectl_env(env_name, args, timeout=60)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    if rc != 0:
        raise UserError("kubectl apply 失败：%s" % err.strip()[:600])
    return out, err


# ===================================================================== 事件 / 描述 / Top
def list_events(env_name, namespace=None, kind=None, name=None, limit=200, all_ns=False):
    """列出集群事件，按时间倒序。返回 {events, warning, total}。

    - ``all_ns``：跨全部命名空间（忽略 namespace）。
    - ``kind`` / ``name``：对 involvedObject 做服务端无关过滤（name 支持模糊匹配）。
    - 原始命令：``kubectl get events -o json --sort-by=.metadata.creationTimestamp``。
    """
    args = ["get", "events", "-o", "json"]
    if all_ns:
        args += ["--all-namespaces"]
    elif namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    args += ["--sort-by=.metadata.creationTimestamp"]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("获取事件失败：%s" % err.strip()[:400])
    try:
        items = json.loads(out).get("items", [])
    except Exception:
        raise UserError("kubectl 返回非 JSON（可能未连接集群）。")

    events = []
    for it in items:
        meta = it.get("metadata", {})
        last = it.get("lastTimestamp") or it.get("eventTime") or ""
        first = it.get("firstTimestamp") or it.get("eventTime") or ""
        obj = it.get("involvedObject", {})
        src = (it.get("source") or {}).get("component", "") \
            or (it.get("reportingComponent", "") or "")
        events.append({
            "type": it.get("type", "Normal"),
            "reason": it.get("reason", ""),
            "message": it.get("message", ""),
            "object_kind": obj.get("kind", ""),
            "object_name": obj.get("name", ""),
            "object_ns": obj.get("namespace", ""),
            "source": src,
            "count": it.get("count", 1),
            "first_seen": first,
            "last_seen": last,
        })
    # kubectl 默认按时间升序，倒序使最新在前
    events.reverse()
    if kind or name:
        events = [e for e in events
                  if (not kind or e["object_kind"].lower() == kind.lower())
                  and (not name or name.lower() in e["object_name"].lower())]
    if limit:
        events = events[:limit]
    warning = sum(1 for e in events if e["type"] == "Warning")
    return {"events": events, "warning": warning, "total": len(events)}


def describe_resource(env_name, kind, name, namespace=None):
    """kubectl describe <kind> <name>，返回原始文本 + 该资源相关事件（便于排障）。

    文本忠实于 kubectl describe 输出；事件由 ``list_events`` 按名称过滤得到。
    """
    if not kind or not name:
        raise UserError("请指定资源类型与名称。")
    args = ["describe", kind, name]
    if namespace:
        args += ["-n", namespace]
    elif (env := get_env(env_name)[1]).get("namespace"):
        args += ["-n", env["namespace"]]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        raise UserError("describe %s/%s 失败：%s" % (kind, name, err.strip()[:400]))
    related = []
    try:
        evd = list_events(env_name, namespace=namespace, kind=kind, name=name, limit=50)
        related = evd.get("events", [])
    except Exception:
        related = []
    return {"text": out, "events": related}


def _parse_top_val(s):
    """把 kubectl top 的量（如 '250m'/'1'/'512Mi'/'2Gi'/'1Gi'）解析为浮点数值，
    统一为「核」(CPU) 或「MiB」(内存) 量级，仅用于排序与条形占比估算。
    """
    s = (s or "").strip()
    if not s:
        return 0.0
    if s.endswith("m"):                        # millicores / millis
        try:
            return float(s[:-1]) / 1000.0
        except ValueError:
            return 0.0
    m = re.match(r"^([\d.]+)\s*(Ki|Mi|Gi|Ti|K|M|G|T|i|n)?$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return 0.0
    val = float(m.group(1))
    unit = m.group(2) or ""
    mult = {
        "n": 1e-9, "Ki": 1 / 1024.0, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 * 1024,
        "K": 1e-6, "M": 1e-3, "G": 1.0, "T": 1e3, "i": 1.0,
    }
    return val * mult.get(unit, 1.0)


def get_top(env_name, scope="pods", namespace=None):
    """kubectl top pods/nodes。返回 {scope, rows}。

    rows（pod）：{name, namespace, cpu, memory}
    rows（node）：{name, cpu, cpu_pct, memory, memory_pct}
    均按内存消耗降序排列，便于一眼定位资源大户。
    """
    if scope not in ("pods", "nodes"):
        scope = "pods"
    args = ["top", scope, "--no-headers"]
    if scope == "pods":
        if namespace:
            args += ["-n", namespace]
        elif (env := get_env(env_name)[1]).get("namespace"):
            args += ["-n", env["namespace"]]
    out, rc, err = run_kubectl_env(env_name, args, timeout=30)
    if rc != 0:
        # metrics-server 未就绪是最常见原因
        raise UserError("获取 Top 失败（集群需启用 metrics-server）：%s" % err.strip()[:400])
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if scope == "pods":
            # kubectl top pods（非 --all-namespaces）输出为 NAME CPU MEMORY，不含命名空间列
            if len(parts) >= 3:
                rows.append({
                    "name": parts[0],
                    "namespace": "",
                    "cpu": parts[1],
                    "memory": parts[2],
                })
            elif len(parts) == 2:        # 极少数输出仅 NAME + CPU
                rows.append({"name": parts[0], "namespace": "", "cpu": parts[1], "memory": "?"})
        else:
            if len(parts) >= 5:
                rows.append({
                    "name": parts[0], "cpu": parts[1], "cpu_pct": parts[2],
                    "memory": parts[3], "memory_pct": parts[4],
                })
            elif len(parts) >= 3:
                rows.append({
                    "name": parts[0], "cpu": parts[1], "cpu_pct": "",
                    "memory": parts[2], "memory_pct": "",
                })
    rows.sort(key=lambda r: _parse_top_val(r.get("memory", "")), reverse=True)
    return {"scope": scope, "rows": rows}


# ===================================================================== 网络检测
def _api_server_host(kubeconfig):
    """从 kubeconfig 解析第一个 cluster.server 的 (host, port)。"""
    if not kubeconfig or not Path(kubeconfig).exists():
        return None
    try:
        text = Path(kubeconfig).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"server:\s*([^\s#]+)", text)
    if not m:
        return None
    url = m.group(1).strip().rstrip("/")
    mm = re.match(r"https?://([^:/]+)(?::(\d+))?", url)
    if not mm:
        return None
    host = mm.group(1)
    port = int(mm.group(2)) if mm.group(2) else 443
    return host, port


def _split_host(target, default_port=443):
    if ":" in target:
        h, p = target.rsplit(":", 1)
        try:
            return h, int(p)
        except ValueError:
            return target, default_port
    return target, default_port


def _tcp_probe(host, port, timeout=3):
    """TCP 探测，返回 (ok, latency_ms|None)。"""
    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True, round((time.time() - t0) * 1000, 1)
    except Exception:
        return False, None


def detect_network(env_name, extra_hosts=None, on_log=None):
    """检测当前机器到指定环境的网络状况。

    返回 dict：
        env, checks[], intranet[], cluster_ok, internet_ok, summary
    checks[] 元素：{name, status:'ok'|'warn'|'fail', detail}
    """
    if on_log is None:
        on_log = lambda m: None  # noqa: E731
    _, env = get_env(env_name)
    checks = []
    intranet = []

    # 1) kubectl 客户端
    on_log("[*] 检测 kubectl 客户端…")
    out, rc, err = run_kubectl(["version", "--client", "--request-timeout=5s"], timeout=15)
    if rc == 0:
        first = out.strip().split("\n", 1)[0][:140]
        checks.append({"name": "kubectl 客户端", "status": "ok", "detail": first})
    else:
        checks.append({"name": "kubectl 客户端", "status": "fail",
                       "detail": err.strip()[:200] or "未安装 kubectl"})

    # 2) kubeconfig 文件
    kc = env.get("kubeconfig")
    if kc and Path(kc).exists():
        checks.append({"name": "kubeconfig 文件", "status": "ok", "detail": kc})
    else:
        checks.append({"name": "kubeconfig 文件", "status": "fail",
                       "detail": "未配置或不存在：%s" % (kc or "(空)")})

    # 3) 集群连通（kubectl version）
    on_log("[*] 检测集群连通…")
    out, rc, err = run_kubectl_env(env_name, ["version", "--request-timeout=5s"], timeout=15)
    cluster_ok = rc == 0
    checks.append({
        "name": "集群连通 (kubectl version)",
        "status": "ok" if cluster_ok else "fail",
        "detail": (out.strip().split("\n", 1)[0][:140] if cluster_ok
                   else "无法连接集群：" + err.strip()[:160]),
    })

    # 4) 内网 - API Server TCP 探测（最关键的"是否在内网/VPN"信号）
    on_log("[*] 探测集群 API Server（内网）…")
    api = _api_server_host(kc)
    if api:
        host, port = api
        ok, ms = _tcp_probe(host, port, 3)
        intranet.append({"target": "%s:%d" % (host, port), "ok": ok, "ms": ms})
        checks.append({
            "name": "内网·集群 API Server",
            "status": "ok" if ok else "fail",
            "detail": ("可达 %s (延迟 %sms)" % (host, ms)) if ok
            else ("不可达 %s:%d（可能不在内网/VPN）" % (host, port)),
        })
    else:
        checks.append({"name": "内网·集群 API Server", "status": "warn",
                       "detail": "未能从 kubeconfig 解析 API Server 地址"})

    # 5) 用户自定义内网主机
    hosts = list(extra_hosts or []) + list(env.get("intranet_hosts") or [])
    for h in hosts:
        host, port = _split_host(h, 443)
        ok, ms = _tcp_probe(host, port, 3)
        intranet.append({"target": "%s:%d" % (host, port), "ok": ok, "ms": ms})
        checks.append({
            "name": "内网探测 %s:%d" % (host, port),
            "status": "ok" if ok else "fail",
            "detail": ("可达 (延迟 %sms)" % ms) if ok else "不可达",
        })

    # 6) 外网探测（判断是双通还是仅内网）
    on_log("[*] 探测外网…")
    ok, ms = _tcp_probe("8.8.8.8", 53, 3)
    internet_ok = ok
    checks.append({
        "name": "外网探测 (8.8.8.8:53)",
        "status": "ok" if ok else "warn",
        "detail": ("可达 (延迟 %sms)" % ms) if ok else "不可达（可能处于隔离内网）",
    })

    fails = [c for c in checks if c["status"] == "fail"]
    if not fails:
        summary = "网络正常：可连接集群与内网。"
    else:
        summary = "存在 %d 项异常（多因未接入对应内网/VPN 或 kubeconfig 缺失）。" % len(fails)
    return {
        "env": env_name,
        "checks": checks,
        "intranet": intranet,
        "cluster_ok": cluster_ok,
        "internet_ok": internet_ok,
        "summary": summary,
    }


# ===================================================================== Pod 内执行 / 文件浏览
# （Xshell 式终端 + Xftp 式文件浏览器后端。纯 subprocess 调 kubectl，无第三方 k8s 库）
def _exec_base_args(env_name, pod, container, namespace):
    """构造 ``kubectl exec`` 的基础参数（含 env 前缀 / namespace / container）。

    返回 ``(args, ns)``，其中 ``args`` 形如
    ``['kubectl', '--kubeconfig', <kc>, 'exec', <pod>, ('-n', <ns>)?, ('-c', <c>)?]``，
    调用方需自行补上 ``-- sh -c <script>``。env 不存在时抛 ``UserError``。
    """
    _, env = get_env(env_name)
    args = ["kubectl"] + _env_kubectl_prefix(env) + ["exec", pod]
    ns = namespace or env.get("namespace")
    if ns:
        args += ["-n", ns]
    if container:
        args += ["-c", container]
    return args, ns


def _resolve_kubectl_binary():
    binary = shutil.which("kubectl")
    if binary:
        return binary
    for candidate in (
        "/opt/homebrew/bin/kubectl",
        "/usr/local/bin/kubectl",
        "/usr/bin/kubectl",
        "/bin/kubectl",
    ):
        if os.path.exists(candidate):
            return candidate
    return "kubectl"


def _kubectl_subprocess_env(sub_env=None):
    env = (sub_env or os.environ).copy()
    resolved = _resolve_kubectl_binary()
    if resolved and os.path.dirname(resolved):
        bin_dir = os.path.dirname(resolved)
        entries = [bin_dir] + [p for p in env.get("PATH", "").split(os.pathsep) if p]
        env["PATH"] = os.pathsep.join(dict.fromkeys(entries))
    return env


def _run_kubectl_bytes(argv, timeout=60, sub_env=None):
    """以字节模式执行 kubectl（用于二进制安全的 exec / 文件读写）。"""
    args = list(argv)
    resolved = _resolve_kubectl_binary()
    if not args or args[0] != resolved:
        if args and args[0] == "kubectl":
            args[0] = resolved
        else:
            args.insert(0, resolved)
    try:
        proc = _subprocess.run(
            args, capture_output=True, timeout=timeout, env=_kubectl_subprocess_env(sub_env)
        )
        return proc.stdout or b"", proc.returncode, proc.stderr or b""
    except _subprocess.TimeoutExpired:
        return b"", 124, b"kubectl timed out"
    except FileNotFoundError:
        return b"", 127, b"kubectl not found in PATH (install kubectl and add to PATH)"


def _build_exec_script(command, cwd=None, track_cwd=False):
    """构造传给 ``sh -c`` 的脚本。

    - ``cwd``：前置 ``cd '<cwd>' && ``（路径经 shlex.quote，避免注入）。
    - 用户命令按 shell 语义执行（保留管道 / 重定向）。
    - ``track_cwd=True``：追加 ``__PWD__`` 标记 + ``pwd``，并保留用户命令退出码
      （``exit $__EX__``），便于上层解析新工作目录且不掩盖命令真实失败。
    """
    script = "cd %s && %s" % (shlex.quote(cwd), command) if cwd else command
    if track_cwd:
        script = (
            "set +e\n" + script +
            "\n__EX__=$?\nprintf '\\n__PWD__\\n'\npwd\nexit $__EX__"
        )
    return script


def _split_pwd(merged):
    """从带 ``__PWD__`` 标记的输出中解析新工作目录，并返回去掉标记后的内容。

    返回 ``(new_cwd, clean_output)``；无标记时 ``new_cwd=None``。
    """
    idx = merged.rfind("__PWD__")
    if idx == -1:
        return None, merged
    head = merged[:idx]
    tail = merged[idx + len("__PWD__"):]
    new_cwd = None
    for ln in tail.splitlines():
        if ln.strip():
            new_cwd = ln.strip()
            break
    return new_cwd, head.rstrip("\n")


def exec_command(env, pod, container, namespace, command, cwd=None, timeout=60):
    """在 Pod 内一次性执行命令。

    返回 ``(output, new_cwd)``：``output`` 为合并后的 stdout/stderr（已剔除
    ``__PWD__`` 标记），``new_cwd`` 为命令执行后的工作目录（``cd`` 未触发则回退
    到传入的 ``cwd``）。kubectl 层错误（Pod 不存在 / 未连接等）抛 ``UserError``。
    """
    if not pod:
        raise UserError("缺少 pod 参数。")
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = _build_exec_script(command, cwd, track_cwd=True)
    argv = base + ["--", "sh", "-c", script]
    data, rc, err = _run_kubectl_bytes(argv, timeout=timeout)
    out = data.decode("utf-8", "replace")
    err_s = err.decode("utf-8", "replace")
    merged = out
    if err_s:
        merged = (merged + "\n" + err_s) if merged else err_s
    if "__PWD__" not in merged:
        raise UserError(
            "在 Pod(%s) 中执行命令失败：%s" % (pod, err_s.strip()[:400] or "未知错误")
        )
    new_cwd, clean = _split_pwd(merged)
    if not new_cwd and cwd:
        new_cwd = cwd
    return clean, new_cwd


_LS_RE = re.compile(
    r"^(?P<mode>[dl-][rwxST\-]{9})\s+"
    r"(?P<link>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<mdate>\S+\s+\S+\s+\S+)\s+"
    r"(?P<name>.+?)\s*$"
)

_TOTAL_RE = re.compile(r"^total\s+\d+")


def _parse_ls(text):
    """解析 ``ls -la`` 输出为统一条目列表。"""
    entries = []
    for line in text.splitlines():
        if _TOTAL_RE.match(line) or not line.strip():
            continue
        m = _LS_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            continue
        mode = m.group("mode")
        entries.append({
            "name": name,
            "type": "dir" if mode.startswith("d") else "file",
            "size": int(m.group("size")),
            "mode": mode,
            "modtime": m.group("mdate").strip(),
        })
    return entries


def list_dir(env, pod, container, namespace, path, timeout=60):
    """列出 Pod 内某路径下的文件 / 目录。"""
    if not pod:
        raise UserError("缺少 pod 参数。")
    path = path or "/"
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = "ls -la %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    data, rc, err = _run_kubectl_bytes(argv, timeout=timeout)
    if rc != 0:
        raise UserError(
            "列目录失败(%s)：%s" % (
                path, err.decode("utf-8", "replace").strip()[:400] or "未知错误")
        )
    return _parse_ls(data.decode("utf-8", "replace"))


def read_file(env, pod, container, namespace, path, max_bytes=200000, timeout=60):
    """读取 Pod 内文本文件内容（默认上限 200KB）。

    返回 ``(content, is_binary)``：
    - 文本：``content`` 为解码后的字符串（已截断到 ``max_bytes``），``is_binary=False``。
    - 二进制（含 NUL 或不可解码 UTF-8）：``content`` 为 base64 字符串，``is_binary=True``。
    """
    if not pod:
        raise UserError("缺少 pod 参数。")
    if max_bytes is None or max_bytes <= 0:
        max_bytes = 200000
    base, _ = _exec_base_args(env, pod, container, namespace)
    # 多取 1 字节用于判断截断
    script = "head -c %d %s" % (int(max_bytes) + 1, shlex.quote(path))
    argv = base + ["--", "sh", "-c", script]
    data, rc, err = _run_kubectl_bytes(argv, timeout=timeout)
    if rc != 0:
        raise UserError(
            "读取文件失败(%s)：%s" % (
                path, err.decode("utf-8", "replace").strip()[:400] or "未知错误")
        )
    if b"\x00" in data:
        return base64.b64encode(data[:max_bytes]).decode("ascii"), True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(data[:max_bytes]).decode("ascii"), True
    return text[:max_bytes], False


def _file_size_bytes(env, pod, container, namespace, path, timeout=60):
    """返回 Pod 内文件字节数（供读取端点判断 truncated）。"""
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = "wc -c < %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    data, rc, _ = _run_kubectl_bytes(argv, timeout=timeout)
    if rc != 0:
        return None
    try:
        return int(data.decode("utf-8", "replace").strip())
    except ValueError:
        return None


def write_file(env, pod, container, namespace, path, content, binary=False, timeout=60):
    """将内容写入 Pod 内文件。

    - 文本：``content`` 为 str，经 stdin 送入 ``cat > <path>``。
    - 二进制：``content`` 为 bytes，经 ``base64 -d > <path>`` 解码写入。
    """
    if not pod:
        raise UserError("缺少 pod 参数。")
    payload = content.encode("utf-8") if isinstance(content, str) else content
    base, _ = _exec_base_args(env, pod, container, namespace)
    if binary:
        script = "base64 -d > %s" % shlex.quote(path)
    else:
        script = "cat > %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    proc = _subprocess.run(
        list(argv), input=payload, capture_output=True, timeout=timeout,
        env=_kubectl_subprocess_env()
    )
    if proc.returncode != 0:
        raise UserError(
            "写入文件失败(%s)：%s" % (
                path, (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
                or "未知错误")
        )


def delete_path(env, pod, container, namespace, path, is_dir=False, timeout=60):
    """删除 Pod 内文件或目录。"""
    if not pod:
        raise UserError("缺少 pod 参数。")
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = ("rm -rf %s" if is_dir else "rm -f %s") % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    proc = _subprocess.run(
        list(argv), capture_output=True, timeout=timeout,
        env=_kubectl_subprocess_env())
    if proc.returncode != 0:
        raise UserError(
            "删除失败(%s)：%s" % (
                path, (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
                or "未知错误")
        )


def mkdir_path(env, pod, container, namespace, path, timeout=60):
    """在 Pod 内创建目录（含父级）。"""
    if not pod:
        raise UserError("缺少 pod 参数。")
    base, _ = _exec_base_args(env, pod, container, namespace)
    script = "mkdir -p %s" % shlex.quote(path)
    argv = base + ["--", "sh", "-c", script]
    proc = _subprocess.run(
        list(argv), capture_output=True, timeout=timeout,
        env=_kubectl_subprocess_env())
    if proc.returncode != 0:
        raise UserError(
            "创建目录失败(%s)：%s" % (
                path, (proc.stderr or b"").decode("utf-8", "replace").strip()[:400]
                or "未知错误")
        )


# 供 GUI 在把 opts 交给 run_snapshot 之前解析环境变量使用
def resolve_env_kubeconfig(env_name):
    """返回 (kubeconfig_path, namespace) 供快照/日志使用。"""
    _, env = get_env(env_name)
    return env.get("kubeconfig") or None, env.get("namespace") or None
