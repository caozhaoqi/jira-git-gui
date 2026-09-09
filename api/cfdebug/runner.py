# -*- coding: utf-8 -*-
"""
云函数调试：会话管理与函数扫描（api/cfdebug/runner.py）
================================================================
职责：
  - 扫描云函数目录，解析每个 .py 的「模型 A/B、入口类、execute 参数、docstring」供前端选择。
  - 启动调试会话：以后端解释器 `python -m debugpy --listen ... --wait-for-client
    launcher.py ...` 方式拉起，debugpy 在断点处暂停并通过 DAP 暴露变量/调用栈。
  - 采集子进程 stdout/stderr，按行经 SSE 广播 cf_debug_log（带 session_id，便于前端按会话区分）。
  - 停止会话、列出会话、环境配置持久化（functions_root + 各环境 server/token）。
"""
import ast
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.common import logger, broadcast
from api.cfdebug import loader

_LAUNCHER = Path(__file__).resolve().parent / "launcher.py"
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "cfdebug_env.local.json"

# 默认云函数根目录候选（存在则用，否则留空由前端填写）
_DEFAULT_ROOT_CANDIDATES = [
    "/Users/caozhaoqi/Downloads/other/hcm-cloud-vue/hcm-core/cloud_functions",
]


def _default_root() -> str:
    for c in _DEFAULT_ROOT_CANDIDATES:
        if Path(c).is_dir():
            return c
    return ""


# ───────────────────────── 环境配置持久化 ─────────────────────────
def get_env() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "functions_root": _default_root(),
        "current_env": "mock",
        "envs": {
            "test": {"server": "", "token": ""},
            "custom": {"server": "", "token": ""},
        },
    }
    try:
        if _ENV_FILE.exists():
            saved = json.loads(_ENV_FILE.read_text("utf-8"))
            cfg.update({k: saved[k] for k in ("functions_root", "current_env") if k in saved})
            if isinstance(saved.get("envs"), dict):
                for k in ("test", "custom"):
                    if isinstance(saved["envs"].get(k), dict):
                        cfg["envs"][k].update(saved["envs"][k])
    except Exception as e:
        logger.warning("[cfdebug] 读取环境配置失败: %s", e)
    return cfg


def set_env(patch: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_env()
    if "functions_root" in patch and isinstance(patch["functions_root"], str):
        cfg["functions_root"] = patch["functions_root"]
    if "current_env" in patch and patch["current_env"] in ("mock", "test", "custom"):
        cfg["current_env"] = patch["current_env"]
    for k in ("test", "custom"):
        if isinstance(patch.get(k), dict):
            cfg["envs"][k].update({kk: patch[k][kk] for kk in ("server", "token") if kk in patch[k]})
    try:
        _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ENV_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        logger.warning("[cfdebug] 写入环境配置失败: %s", e)
    return cfg


# ───────────────────────── 函数扫描 ─────────────────────────
def _scan_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        src = Path(path).read_text("utf-8")
    except Exception:
        return None
    try:
        tree = ast.parse(src)
    except Exception:
        return None
    model = loader.detect_model(path)
    entry = None
    params: List[str] = []
    doc = ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            entry = node.name
            doc = ast.get_docstring(node) or ""
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "execute":
                    for a in sub.args.args:
                        if a.arg == "self":
                            continue
                        params.append(a.arg)
                    if sub.args.vararg:
                        params.append("*" + sub.args.vararg.arg)
                    if sub.args.kwarg:
                        params.append("**" + sub.args.kwarg.arg)
            break
    if not entry:
        return None
    st = Path(path).stat()
    return {
        "name": Path(path).name,
        "path": path,
        "model": model,
        "entry": entry,
        "params": params,
        "doc": (doc or "").strip()[:500],
        "size": st.st_size,
        "mtime": int(st.st_mtime),
    }


def list_functions(root: Optional[str] = None) -> Dict[str, Any]:
    root = (root or get_env()["functions_root"] or "").strip()
    functions: List[Dict[str, Any]] = []
    if root and Path(root).is_dir():
        for p in sorted(Path(root).rglob("*.py")):
            if p.name.startswith(".") or "test" in p.name.lower() and p.name.lower().endswith("_test.py"):
                # 仍纳入，但跳过隐藏文件
                if p.name.startswith("."):
                    continue
            meta = _scan_file(str(p))
            if meta:
                functions.append(meta)
    return {"root": root, "functions": functions}


def read_source(file: str) -> Dict[str, Any]:
    try:
        text = Path(file).read_text("utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "file": file, "lines": text.splitlines()}


# ───────────────────────── 会话管理 ─────────────────────────
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()


def _free_port() -> int:
    import socket
    for cand in range(5910, 6010):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", cand))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
    raise RuntimeError("找不到可用 DAP 端口")


_LEVEL_RE = re.compile(r"\[(\w+)\]")


def _classify_level(line: str) -> str:
    m = _LEVEL_RE.search(line)
    if m:
        lv = m.group(1).upper()
        return {"ERROR": "error", "WARNING": "warn", "WARN": "warn",
                "DEBUG": "debug", "INFO": "info", "CRITICAL": "error"}.get(lv, "info")
    return "info"


def _pump(proc, pipe_name, session_id):
    """后台线程：读取子进程 stdout/stderr 行，经 SSE 广播 cf_debug_log。"""
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\n")
            if not line:
                continue
            lvl = "error" if pipe_name == "stderr" else _classify_level(line)
            broadcast("cf_debug_log", {"session_id": session_id, "level": lvl, "msg": line})
    except Exception:
        pass


def start_session(req: Dict[str, Any]) -> Dict[str, Any]:
    file = req.get("file") or ""
    if not file or not Path(file).is_file():
        raise ValueError(f"云函数文件不存在: {file}")
    dap_port = _free_port()
    env_cfg = get_env()
    env = req.get("env") or env_cfg["current_env"] or "mock"

    # 解析真实 HCM 的 server / token（test/custom）
    server = req.get("server") or ""
    token = req.get("token") or ""
    if env in ("test", "custom"):
        if not server:
            server = env_cfg["envs"].get(env, {}).get("server", "")
        if not token:
            token = env_cfg["envs"].get(env, {}).get("token", "")

    argv = [
        sys.executable, "-Xfrozen_modules=off", str(_LAUNCHER),
        "--cf-dap-port", str(dap_port),
        "--cf-file", file,
        "--cf-env", env,
        "--cf-kwargs", req.get("kwargs") or "{}",
        "--cf-company-id", str(req.get("company_id") or 1),
        "--cf-debug-id", str(req.get("debug_id") or ""),
        "--cf-allow-ddl", "1" if req.get("allow_ddl") else "0",
        "--cf-db-save", "1" if req.get("db_save") else "0",
        "--cf-write-real", "1" if req.get("write_real") else "0",
    ]
    if server:
        argv += ["--cf-server", server]
    if token:
        argv += ["--cf-token", token]
    if req.get("db_url"):
        argv += ["--cf-db-url", req["db_url"]]
    if req.get("entry"):
        argv += ["--cf-entry", req["entry"]]

    session_id = uuid.uuid4().hex[:12]
    proc = __import__("subprocess").Popen(
        argv,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        stdout=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(Path(__file__).resolve().parent.parent.parent),
             "PYDEVD_DISABLE_FILE_VALIDATION": "1"},
    )
    info = {
        "session_id": session_id,
        "proc": proc,
        "dap_host": "127.0.0.1",
        "dap_port": dap_port,
        "file": file,
        "env": env,
        "started_at": int(time.time()),
        "log_threads": [],
    }
    t_out = threading.Thread(target=_pump, args=(proc, "stdout", session_id), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc, "stderr", session_id), daemon=True)
    t_out.start()
    t_err.start()
    info["log_threads"] = [t_out, t_err]

    # 退出监听：子进程结束时广播 cf_debug_done
    def _watch():
        rc = proc.wait()
        broadcast("cf_debug_log", {
            "session_id": session_id, "level": "info",
            "msg": f"[session] 云函数进程已退出 (returncode={rc})",
        })
        # 若已被 stop/orphan_guard 处理（stopped=True），不再重复广播 done
        if not info.get("stopped"):
            broadcast("cf_debug_done", {"session_id": session_id, "returncode": rc})
        with _SESSIONS_LOCK:
            _SESSIONS.pop(session_id, None)

    threading.Thread(target=_watch, daemon=True).start()

    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = info

    logger.info("[cfdebug] 启动会话 %s -> %s (dap=%s)", session_id, file, dap_port)
    return {
        "session_id": session_id,
        "dap_host": info["dap_host"],
        "dap_port": info["dap_port"],
        "ws_url": f"/api/cf-debug/ws/{session_id}",
        "file": file,
        "env": env,
    }


def stop_session(session_id: str) -> Dict[str, Any]:
    with _SESSIONS_LOCK:
        info = _SESSIONS.get(session_id)
    if not info:
        return {"ok": False, "error": "会话不存在或已结束"}
    info["stopped"] = True
    proc = info.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    broadcast("cf_debug_log", {"session_id": session_id, "level": "warn",
                                "msg": "[session] 已手动停止调试会话"})
    broadcast("cf_debug_done", {"session_id": session_id, "returncode": -1})
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)
    return {"ok": True}


def orphan_guard(session_id: str) -> None:
    """WS 桥接断开但会话仍在（前端未走正常 stop）→ 兜底终止子进程，避免孤儿进程。"""
    with _SESSIONS_LOCK:
        info = _SESSIONS.get(session_id)
        if not info:
            return
        info["stopped"] = True
    proc = info.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)


def list_sessions() -> Dict[str, Any]:
    out = []
    with _SESSIONS_LOCK:
        for info in _SESSIONS.values():
            out.append({
                "session_id": info["session_id"],
                "file": info["file"],
                "env": info["env"],
                "dap_port": info["dap_port"],
                "started_at": info["started_at"],
                "alive": info["proc"].poll() is None,
            })
    return {"sessions": out}


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with _SESSIONS_LOCK:
        return _SESSIONS.get(session_id)
