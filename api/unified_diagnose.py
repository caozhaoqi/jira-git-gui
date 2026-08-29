# -*- coding: utf-8 -*-
"""统一诊断引擎 —— 将 CF 云函数错误诊断与 K8s 集群诊断合并为单一上下文。

当 HCM 云函数运行在 K8s 上时，一次错误可能涉及：
1. 云函数代码层面的错误（字段缺失/Token过期/权限不足）
2. 基础设施层面的异常（Pod OOM/Crash/探针失败/节点不可用）

本模块把两类诊断素材聚合到一个响应中，让 AI 能同时看到应用层和基础设施层的证据，
快速判断是代码问题还是环境问题。

调用链：
  POST /api/diagnose →
    cf_diagnose_context()   # 应用层：解析+词典+Wiki+源码+日志+案例
    k8s_collect_diagnostics()  # 基础设施层：Pod状态+事件+异常容器日志
    → 合并 → _build_unified_summary() → _build_unified_prompt()
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from api.common import logger, _PROJECT_ROOT

# K8s 错误词典路径
_K8S_ERRDICT_PATH = (
    _PROJECT_ROOT / "store" / "downloads" / "895" / "docs" / "metadata" / "reference" / "k8s_errdict.json"
)
_K8S_ERRDICT_CACHE = {"mtime": 0.0, "data": None}


def _load_k8s_errdict() -> dict:
    """加载 K8s 错误模式词典（带 mtime 缓存）。"""
    try:
        mt = _K8S_ERRDICT_PATH.stat().st_mtime
    except OSError:
        return {}
    if _K8S_ERRDICT_CACHE["data"] is not None and _K8S_ERRDICT_CACHE["mtime"] == mt:
        return _K8S_ERRDICT_CACHE["data"]
    try:
        data = json.loads(_K8S_ERRDICT_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[UNIFIED-DIAG] 读取 k8s_errdict.json 失败: {e}")
        return {}
    _K8S_ERRDICT_CACHE.update(mtime=mt, data=data)
    return data


def _lookup_k8s_pattern(reason: str) -> Optional[dict]:
    """根据事件 reason 或 Pod 状态查 K8s 错误词典。"""
    if not reason:
        return None
    d = _load_k8s_errdict()
    patterns = d.get("patterns", {})
    # 精确匹配
    if reason in patterns:
        return patterns[reason]
    # 模糊匹配（大小写不敏感）
    reason_lower = reason.lower()
    for key, val in patterns.items():
        if key.lower() in reason_lower or reason_lower in key.lower():
            return val
    return None


def _lookup_exit_code(code: int) -> Optional[str]:
    """查退出码含义。"""
    d = _load_k8s_errdict()
    return d.get("exit_codes", {}).get(str(code))


def _lookup_event_reason(reason: str) -> Optional[str]:
    """查事件 reason 含义。"""
    d = _load_k8s_errdict()
    return d.get("event_reasons", {}).get(reason)


# --------------------------------------------------------------------------- #
#  K8s 诊断信息采集
# --------------------------------------------------------------------------- #

def k8s_collect_diagnostics(env: str = "", namespace: str = "",
                            pod_filter: str = "", tail: int = 100) -> dict:
    """采集 K8s 集群诊断信息：异常 Pod + Warning 事件 + 异常容器日志。

    这是一个只读操作，不修改集群任何资源。

    参数：
        env: K8s 环境名（对应 config/k8s_envs.json 中的 key）
        namespace: 命名空间（留空则用环境默认命名空间或全部）
        pod_filter: Pod 名称过滤（模糊匹配）
        tail: 日志行数限制
    """
    result = {
        "available": False,
        "env": env,
        "namespace": namespace,
        "error": "",
        "abnormal_pods": [],
        "warning_events": [],
        "crash_logs": [],
        "top_consumers": [],
        "pattern_matches": [],
    }

    if not env:
        result["error"] = "未指定 K8s 环境"
        return result

    try:
        from core.k8s import list_pods, list_events, get_top
        from core.k8s.pods import run_kubectl_env
    except ImportError as e:
        result["error"] = f"K8s 模块不可用: {e}"
        return result

    # 1. 获取 Pod 列表，筛选异常 Pod
    try:
        pods = list_pods(env, namespace=namespace or None)
        abnormal = []
        for pod in pods:
            name = pod.get("name", "")
            phase = pod.get("phase", "")
            restarts = pod.get("restarts", 0)
            if pod_filter and pod_filter.lower() not in name.lower():
                continue
            # 筛选异常状态
            is_abnormal = (
                phase not in ("Running", "Succeeded", "")
                or restarts > 5
            )
            if is_abnormal or (pod_filter and pod_filter.lower() in name.lower()):
                # 查词典
                pattern_info = _lookup_k8s_pattern(phase) if phase not in ("Running", "Succeeded") else None
                pod_info = {
                    "name": name,
                    "namespace": pod.get("namespace", ""),
                    "phase": phase,
                    "restarts": restarts,
                    "node": pod.get("node", ""),
                    "pattern_match": pattern_info,
                }
                abnormal.append(pod_info)
        result["abnormal_pods"] = abnormal
        result["available"] = True
    except Exception as e:
        logger.warning(f"[UNIFIED-DIAG] 获取 Pod 列表失败: {e}")
        result["error"] = f"获取 Pod 列表失败: {e}"

    # 2. 获取 Warning 事件
    try:
        ev_data = list_events(env, namespace=namespace or None, limit=50, all_ns=not namespace)
        warnings = [e for e in ev_data.get("events", []) if e.get("type") == "Warning"]
        # 为每个 Warning 事件附加词典释义
        for w in warnings:
            reason = w.get("reason", "")
            w["meaning"] = _lookup_event_reason(reason) or ""
            pattern = _lookup_k8s_pattern(reason)
            if pattern:
                w["pattern_info"] = pattern
        result["warning_events"] = warnings
    except Exception as e:
        logger.warning(f"[UNIFIED-DIAG] 获取事件失败: {e}")

    # 3. 获取异常 Pod 的上一轮日志（崩溃日志）
    crash_logs = []
    for pod in result["abnormal_pods"][:5]:  # 最多取 5 个异常 Pod
        name = pod["name"]
        ns = pod["namespace"] or namespace
        if pod["restarts"] > 0 or pod["phase"] not in ("Running", "Succeeded", ""):
            try:
                log_args = ["logs", name, "--previous", f"--tail={tail}"]
                if ns:
                    log_args += ["-n", ns]
                out, rc, err = run_kubectl_env(env, log_args, timeout=15)
                if rc == 0 and out.strip():
                    crash_logs.append({
                        "pod": name,
                        "namespace": ns,
                        "log": out.strip()[-2000:],  # 截取最后 2000 字符
                        "phase": pod["phase"],
                        "restarts": pod["restarts"],
                    })
            except Exception:
                pass  # --previous 可能不存在
        # 也获取当前日志的尾部
        try:
            log_args = ["logs", name, f"--tail={tail}"]
            if ns:
                log_args += ["-n", ns]
            out, rc, err = run_kubectl_env(env, log_args, timeout=15)
            if rc == 0 and out.strip():
                crash_logs.append({
                    "pod": name,
                    "namespace": ns,
                    "log_type": "current",
                    "log": out.strip()[-2000:],
                    "phase": pod["phase"],
                    "restarts": pod["restarts"],
                })
        except Exception:
            pass
    result["crash_logs"] = crash_logs

    # 4. 获取资源 Top（找内存/CPU 大户）
    try:
        top_data = get_top(env, scope="pods", namespace=namespace or None)
        result["top_consumers"] = top_data.get("rows", [])[:10]
    except Exception:
        pass  # metrics-server 可能未启用

    # 5. 汇总模式匹配
    pattern_matches = []
    for pod in result["abnormal_pods"]:
        pm = pod.get("pattern_match")
        if pm:
            pattern_matches.append({
                "pod": pod["name"],
                "phase": pod["phase"],
                "pattern": pm.get("name", ""),
                "meaning": pm.get("meaning", ""),
                "common_causes": pm.get("common_causes", []),
                "diagnose_steps": pm.get("diagnose_steps", []),
            })
    for ev in result["warning_events"]:
        pi = ev.get("pattern_info")
        if pi and not any(pm["pattern"] == pi.get("name") for pm in pattern_matches):
            pattern_matches.append({
                "pod": ev.get("object_name", ""),
                "phase": ev.get("reason", ""),
                "pattern": pi.get("name", ""),
                "meaning": pi.get("meaning", ""),
                "common_causes": pi.get("common_causes", []),
                "diagnose_steps": pi.get("diagnose_steps", []),
            })
    result["pattern_matches"] = pattern_matches

    return result


# --------------------------------------------------------------------------- #
#  联合诊断摘要
# --------------------------------------------------------------------------- #

def _build_unified_summary(cf_summary: dict, k8s_diag: dict) -> dict:
    """合并 CF 诊断摘要和 K8s 诊断信息，给出联合根因判断。

    逻辑：
    - 如果 K8s 有异常 Pod（OOM/Crash），且 CF 报错是 17003（OpenAPI 执行异常），
      则根因可能是基础设施层面而非代码层面
    - 如果 K8s 一切正常，则根因大概率在 CF 代码/数据层面
    - 如果 K8s 节点不可用，则 CF 502/504 可能是基础设施导致
    """
    cf_root = cf_summary.get("root_cause", "UNKNOWN")
    cf_conf = cf_summary.get("confidence", 0.35)
    reasons = list(cf_summary.get("reasons", []))
    checks = list(cf_summary.get("checks_to_run", []))

    k8s_abnormal = k8s_diag.get("abnormal_pods", [])
    k8s_warnings = k8s_diag.get("warning_events", [])
    k8s_patterns = k8s_diag.get("pattern_matches", [])
    k8s_available = k8s_diag.get("available", False)

    if not k8s_available:
        return {
            "root_cause": cf_root,
            "confidence": cf_conf,
            "status": cf_summary.get("status", "need_verification"),
            "reasons": reasons + [f"K8s 诊断不可用: {k8s_diag.get('error', '')}"],
            "checks_to_run": checks,
            "infrastructure_status": "unknown",
            "cross_reference": "K8s 诊断未执行，无法判断是否为基础设施问题",
        }

    # 判断基础设施状态
    has_oom = any(
        p.get("phase") == "OOMKilled"
        or any("OOMKilled" in (pp.get("pattern", "") or "") for pp in k8s_patterns)
        for p in k8s_abnormal
    )
    has_crash = any(
        p.get("phase") in ("CrashLoopBackOff", "Error", "Failed")
        or p.get("restarts", 0) > 5
        for p in k8s_abnormal
    )
    has_node_not_ready = any(
        ev.get("reason") == "NodeNotReady" for ev in k8s_warnings
    )
    has_probe_failure = any(
        "Unhealthy" in (ev.get("reason", "") or "") for ev in k8s_warnings
    )

    infra_status = "healthy"
    cross_ref = "K8s 集群状态正常，根因大概率在 CF 代码/数据层面"

    if has_oom:
        infra_status = "oom_detected"
        cross_ref = (
            "检测到 OOMKilled 事件。CF 报错可能是容器内存溢出导致，"
            "而非业务逻辑错误。建议检查 Pod 内存限制和云函数内存使用。"
        )
        if cf_root in ("OPEN_API_EXECUTION_ERROR", "UNKNOWN"):
            cf_root = "INFRASTRUCTURE_OOM"
            cf_conf = 0.82
            reasons.append("K8s 检测到 OOMKilled，云函数可能因内存溢出而非业务逻辑异常")
            checks.append("检查 Pod resources.limits.memory 是否过低")
            checks.append("检查云函数是否有大批量数据处理逻辑")
    elif has_crash:
        infra_status = "crash_detected"
        cross_ref = (
            "检测到 Pod 频繁重启/CrashLoopBackOff。"
            "CF 报错可能是容器崩溃导致，而非 HCM 平台返回的业务错误。"
        )
        if cf_root in ("OPEN_API_EXECUTION_ERROR", "UNKNOWN"):
            cf_root = "INFRASTRUCTURE_CRASH"
            cf_conf = 0.75
            reasons.append("K8s 检测到 Pod 异常重启，云函数可能因容器崩溃导致")
            checks.append("查看异常 Pod 的 --previous 日志")
            checks.append("检查容器启动命令和依赖是否正确")
    elif has_node_not_ready:
        infra_status = "node_not_ready"
        cross_ref = (
            "检测到节点不可用。CF 502/504 可能是基础设施层面导致，"
            "而非网关或云函数代码问题。"
        )
        if cf_root in ("OPEN_API_EXECUTION_ERROR", "UNKNOWN"):
            cf_root = "INFRASTRUCTURE_NODE_DOWN"
            cf_conf = 0.85
            reasons.append("K8s 节点不可用，云函数报错可能是基础设施层面导致")
            checks.append("检查节点状态和 kubelet 日志")
    elif has_probe_failure:
        infra_status = "probe_failure"
        cross_ref = "检测到探针失败，服务可能未完全就绪，请求可能被路由到不健康的 Pod"
        reasons.append("K8s 探针检测失败，部分 Pod 可能未就绪")
        checks.append("检查探针配置和后端服务健康度")
    else:
        reasons.append("K8s 集群状态正常，异常大概率在应用/数据层面")

    return {
        "root_cause": cf_root,
        "confidence": cf_conf,
        "status": "high_probability" if cf_conf >= 0.8 else "need_verification",
        "reasons": reasons,
        "checks_to_run": checks,
        "infrastructure_status": infra_status,
        "cross_reference": cross_ref,
        "cf_diagnosis": {
            "root_cause": cf_summary.get("root_cause"),
            "confidence": cf_summary.get("confidence"),
        },
        "k8s_abnormal_count": len(k8s_abnormal),
        "k8s_warning_count": len(k8s_warnings),
    }


# --------------------------------------------------------------------------- #
#  联合 AI 提示词
# --------------------------------------------------------------------------- #

def _build_unified_prompt(cf_prompt: str, k8s_diag: dict, unified_summary: dict) -> str:
    """在 CF 诊断提示词基础上追加 K8s 基础设施诊断上下文。"""
    L = [cf_prompt, "", "---", ""]
    L.append("# K8s 基础设施诊断上下文")
    L.append("")

    if not k8s_diag.get("available"):
        L.append(f"K8s 诊断不可用: {k8s_diag.get('error', '未指定环境')}")
        return "\n".join(L)

    # 异常 Pod
    abnormal = k8s_diag.get("abnormal_pods", [])
    if abnormal:
        L.append("## 异常 Pod")
        for pod in abnormal[:10]:
            L.append(
                f"- `{pod['name']}` (ns={pod.get('namespace', '')}, "
                f"phase={pod.get('phase', '')}, restarts={pod.get('restarts', 0)}, "
                f"node={pod.get('node', '')})"
            )
            pm = pod.get("pattern_match")
            if pm:
                L.append(f"  - 模式: {pm.get('name')} — {pm.get('meaning')}")
                L.append(f"  - 常见原因: {'; '.join(pm.get('common_causes', [])[:3])}")
                L.append(f"  - 排查步骤: {'; '.join(pm.get('diagnose_steps', [])[:3])}")
        L.append("")
    else:
        L.append("## 异常 Pod")
        L.append("无异常 Pod（所有 Pod 处于 Running/Succeeded 状态）")
        L.append("")

    # Warning 事件
    warnings = k8s_diag.get("warning_events", [])
    if warnings:
        L.append("## Warning 事件")
        for ev in warnings[:10]:
            meaning = ev.get("meaning", "")
            L.append(
                f"- [{ev.get('last_seen', '')}] {ev.get('reason', '')} "
                f"on {ev.get('object_kind', '')}/{ev.get('object_name', '')}: "
                f"{ev.get('message', '')[:200]}"
            )
            if meaning:
                L.append(f"  - 释义: {meaning}")
        L.append("")

    # 崩溃日志
    crash_logs = k8s_diag.get("crash_logs", [])
    if crash_logs:
        L.append("## 异常容器日志（截取尾部）")
        for cl in crash_logs[:5]:
            L.append(f"### Pod `{cl.get('pod')}` (phase={cl.get('phase')}, restarts={cl.get('restarts')})")
            L.append("```")
            L.append(cl.get("log", ""))
            L.append("```")
            L.append("")
        L.append("")

    # 资源 Top
    top = k8s_diag.get("top_consumers", [])
    if top:
        L.append("## 资源占用 Top")
        for r in top[:5]:
            L.append(f"- {r.get('name', '')}: CPU={r.get('cpu', '')}, Memory={r.get('memory', '')}")
        L.append("")

    # 联合判断
    L.append("## 联合诊断结论")
    L.append(f"- 基础设施状态: {unified_summary.get('infrastructure_status', 'unknown')}")
    L.append(f"- 交叉参考: {unified_summary.get('cross_reference', '')}")
    L.append(f"- K8s 异常 Pod 数: {unified_summary.get('k8s_abnormal_count', 0)}")
    L.append(f"- K8s Warning 事件数: {unified_summary.get('k8s_warning_count', 0)}")
    L.append("")
    L.append("## 诊断要求")
    L.append("请综合以上 CF 云函数错误诊断和 K8s 基础设施诊断，")
    L.append("判断根因是应用层还是基础设施层，并给出联合修复建议。")
    L.append("如果 K8s 存在异常（OOM/Crash/NodeNotReady），优先排查基础设施。")
    L.append("如果 K8s 正常，则按 CF 诊断结论排查代码和数据问题。")

    return "\n".join(L)


# --------------------------------------------------------------------------- #
#  主入口
# --------------------------------------------------------------------------- #

def unified_diagnose(req) -> dict:
    """统一诊断入口：同时调用 CF 诊断和 K8s 诊断，返回合并后的上下文。

    参数（通过 req 对象传入）：
        text: 错误文本（必填，同 CF 诊断）
        server_url: HCM 网关地址
        token: HCM token
        k8s_env: K8s 环境名（留空则跳过 K8s 诊断）
        k8s_namespace: K8s 命名空间
        k8s_pod_filter: Pod 名称过滤
        k8s_tail: 日志行数（默认 100）
        + CF 诊断的其他参数（model, object_id, field 等）
    """
    from api.cf.cf_diagnose import cf_diagnose_context, _build_diagnosis_summary

    # 1. CF 诊断
    cf_result = cf_diagnose_context(req)
    cf_summary = cf_result.get("summary", {})
    cf_prompt = cf_result.get("aiPrompt", "")

    # 2. K8s 诊断
    k8s_env = getattr(req, "k8s_env", "") or ""
    k8s_namespace = getattr(req, "k8s_namespace", "") or ""
    k8s_pod_filter = getattr(req, "k8s_pod_filter", "") or ""
    k8s_tail = int(getattr(req, "k8s_tail", 100) or 100)

    k8s_diag = k8s_collect_diagnostics(
        env=k8s_env, namespace=k8s_namespace,
        pod_filter=k8s_pod_filter, tail=k8s_tail,
    ) if k8s_env else {"available": False, "error": "未指定 K8s 环境", "abnormal_pods": [], "warning_events": [], "crash_logs": [], "top_consumers": [], "pattern_matches": []}

    # 3. 合并诊断摘要
    unified_summary = _build_unified_summary(cf_summary, k8s_diag)

    # 4. 合并 AI 提示词
    unified_prompt = _build_unified_prompt(cf_prompt, k8s_diag, unified_summary)

    return {
        "ok": True,
        "unified_summary": unified_summary,
        "cf_diagnosis": {
            "summary": cf_summary,
            "parsed": cf_result.get("parsed"),
            "errDict": cf_result.get("errDict"),
            "wiki": cf_result.get("wiki"),
            "tokenHealth": cf_result.get("tokenHealth"),
            "currentData": cf_result.get("currentData"),
            "similarCases": cf_result.get("similarCases"),
            "sourceEvidence": cf_result.get("sourceEvidence"),
            "logMatches": cf_result.get("logMatches"),
            "evidenceBundle": cf_result.get("evidenceBundle"),
            "referenceError": cf_result.get("referenceError"),
        },
        "k8s_diagnosis": k8s_diag,
        "aiPrompt": unified_prompt,
        "diagnosed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
