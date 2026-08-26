# -*- coding: utf-8 -*-
"""K8s 运维子模块（由 core/k8s_manager.py 拆分，保持 import 兼容）。"""
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
from .kubectl import run_kubectl

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


# --------------------------------------------------------------------------- #
#  kubeconfig 集中管理
#  目标：密钥不再散落在 Downloads 等任意目录；统一收口到受控目录
#  （~/.config/jira-git-gui/kubeconfigs/，权限 600），支持导入 / 导出，
#  便于团队共享与轮换。集中存储 / 轮换 / 权限控制的最佳实践见 README。
# --------------------------------------------------------------------------- #
KUBECONFIG_DIR = ENV_CONFIG_PATH.parent / "kubeconfigs"


def import_kubeconfig(env: str, content: str) -> str:
    """把 kubeconfig 内容安全导入受控目录（权限 600），并绑定到环境。

    校验 YAML 合法性，拒绝空内容；导入后自动更新 env.kubeconfig 指向新路径。
    """
    env = env.strip()
    if not env:
        raise UserError("环境名不能为空")
    content = (content or "").strip()
    if not content:
        raise UserError("kubeconfig 内容为空")
    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict) or "clusters" not in parsed:
            raise UserError("内容不是有效的 kubeconfig（缺少 clusters 字段）")
    except yaml.YAMLError as e:
        raise UserError(f"kubeconfig 不是合法 YAML：{e}")

    KUBECONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = KUBECONFIG_DIR / f"{env}.kubeconfig"
    target.write_text(content, encoding="utf-8")
    try:
        os.chmod(target, 0o600)  # 仅当前用户可读写
    except OSError:
        pass
    add_or_update_env(env, kubeconfig=str(target))
    return str(target)


def export_envs(with_content: bool = True) -> dict:
    """导出全部环境配置（含 kubeconfig 内容），用于团队共享 / 备份 / 迁移。

    返回结构与 k8s_envs.json 兼容，但额外携带每个环境的 kubeconfig_content。
    """
    data = load_envs()
    out: dict = {"environments": {}, "current": data.get("current")}
    for name, env in data["environments"].items():
        item = dict(env)
        if with_content:
            kc = env.get("kubeconfig") or ""
            if kc and Path(kc).is_file():
                try:
                    item["kubeconfig_content"] = Path(kc).read_text(encoding="utf-8")
                except OSError:
                    item["kubeconfig_content"] = None
            else:
                item["kubeconfig_content"] = None
        out["environments"][name] = item
    return out


def delete_env(name):
    data = load_envs()
    if name in data["environments"]:
        del data["environments"][name]
        if data.get("current") == name:
            data["current"] = next(iter(data["environments"]), None)
        save_envs(data)
    return data



