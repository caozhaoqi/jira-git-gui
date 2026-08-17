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
import json
import os
import re
import socket
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
    cmd = ["kubectl"] + _env_kubectl_prefix(env) + list(args)
    return run_kubectl(cmd, kubeconfig=None, timeout=timeout)


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


# 供 GUI 在把 opts 交给 run_snapshot 之前解析环境变量使用
def resolve_env_kubeconfig(env_name):
    """返回 (kubeconfig_path, namespace) 供快照/日志使用。"""
    _, env = get_env(env_name)
    return env.get("kubeconfig") or None, env.get("namespace") or None
