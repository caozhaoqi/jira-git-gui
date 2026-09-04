# -*- coding: utf-8 -*-
"""
云函数本地调试核心：复刻云端 Loader + 模型B 全保真支撑（api/cfdebug/loader.py）
================================================================================

移植自 hcm-cloud-vue/temp/cf_debug_loader.py（已在本地 73.2.3.27 实测验证）。

关键事实（本 checkout 实测确认）：
  * core/extend/dynamic_plugin/loader.py / handler.py / customer_util.py 等
    **全是 0 字节桩文件**——真实 Loader 只存在于服务器运行时，本地 checkout 没有实现。
  => 因此“用真实 Loader 跑全保真”在本地【不能直接 import 实现】。替代方案：
       · 模型A（零 import 上传沙箱函数）：忠实复刻 Loader 行为——
         剥 import -> 注入固定沙箱全局 -> exec(compile(真实路径))。
       · 模型B（应用内模块函数，自带 import）：注入本地 shim 包
         service / common.utils / environment（底层接真实 HcmClient / 真实引擎），
         让源码里的 import 解析成功，断点仍命中。

本模块对外提供：
  - BasePrivateApiService         复刻的基类（注入 context / db / meta / call_open_api / log）
  - build_sandbox_globals()      沙箱全局（stdlib + SafeOs）
  - strip_imports(src)           剥顶层 import（复刻云端“零 import”沙箱）
  - SafeSession                  包装 self.db：DDL 默认拦截、CF_DB_SAVE 才落库
  - install_model_b_shims(...)   注入 service/common.utils/environment 三个 shim 包
  - run_model_a / run_model_b    两种执行上下文的统一入口
  - configure(...)               由 launcher 在 exec 前设置 company_id / db / customer_util
"""
import os
import sys
import ast
import types
import logging
import uuid
import time
import heapq
import datetime
import decimal
import json
import re
import copy
import math
import base64
import hashlib
import random
import string
import collections
from types import SimpleNamespace


# ───────── 运行时共享状态（exec 前由 launcher 配置） ─────────
_SETTINGS = {"company_id": 1, "db": None}
_RUNTIME = {"customer_util": None}


def configure(company_id=1, db=None, customer_util=None):
    _SETTINGS["company_id"] = company_id
    _SETTINGS["db"] = db
    if customer_util is not None:
        _RUNTIME["customer_util"] = customer_util


# ───────── 沙箱全局：标准库（零 import 语义） + SafeOs ─────────
def _safe_os():
    """复刻云端注入的受限 os：保留常用安全能力，屏蔽破坏性操作。"""
    return SimpleNamespace(
        path=os.path, environ=os.environ, sep=os.sep, name=os.name,
        getpid=os.getpid, getcwd=os.getcwd, listdir=os.listdir, walk=os.walk,
        makedirs=os.makedirs, mkdir=os.mkdir, environ_get=os.environ.get,
    )


def build_sandbox_globals():
    return {
        "BasePrivateApiService": BasePrivateApiService,
        "CustomerUtil": _RUNTIME.get("customer_util"),
        "logging": logging, "uuid": uuid, "time": time, "heapq": heapq,
        "datetime": datetime, "decimal": decimal, "json": json, "re": re,
        "copy": copy, "math": math, "base64": base64, "hashlib": hashlib,
        "random": random, "string": string, "collections": collections,
        "os": _safe_os(),
    }


# ───────── 复刻云端基类注入的 self.* 语义 ─────────
class _DbStub(object):
    """未提供 CF_DB_URL 时，直连 DB 调用明确报错。"""

    bind = None

    def execute(self, *a, **k):
        raise NotImplementedError(
            "本函数用到 self.db 直连数据库，但本地未提供 CF_DB_URL。"
            "请设置 CF_DB_URL='postgresql+psycopg2://u:p@h:5432/db' 后重试。")

    def commit(self, *a, **k):
        raise NotImplementedError("未提供 CF_DB_URL")

    def rollback(self, *a, **k):
        raise NotImplementedError("未提供 CF_DB_URL")


class BasePrivateApiService(object):
    def __init__(self, *a, **k):
        cid = _SETTINGS.get("company_id", 1)
        self.context = SimpleNamespace(company=SimpleNamespace(id=cid))
        self.db = _SETTINGS.get("db") or _DbStub()

    def call_open_api(self, api_name, param=None):
        if _RUNTIME.get("customer_util") is None:
            raise RuntimeError("CustomerUtil 未配置（launcher 须先 configure(customer_util=...)）")
        return _RUNTIME["customer_util"].call_open_api(api_name, param)

    def meta(self, *a, **k):
        model = (k or {}).get("model") or (a[2] if len(a) > 2 else None)
        return self.call_open_api("hcm.model.meta", {"model": model})

    def log(self, *a, **k):
        # 让云函数里的 self.log(...) 也进日志（便于调试时观察关键参数）
        msg = " ".join(str(x) for x in a)
        if msg:
            logging.info("[CF.log] %s", msg)


# ───────── 剥 import（复刻云端沙箱“零 import”，仅对模型A） ─────────
def strip_imports(src):
    """删掉模块顶层（0 缩进）的 import / from ... import 行。"""
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            continue  # 剥掉该 import 行
        out.append(line)
    return "\n".join(out)


def _first_top_class(src):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            return node.name
    raise SystemExit("源码里没找到顶层类，无法作为云函数入口")


# ───────── 直连 DB 安全包装（DDL 拦截 / 落库开关） ─────────
_DDL_KEYWORDS = ("CREATE ", "DROP ", "ALTER ", "TRUNCATE ", "RENAME ", "GRANT ", "REVOKE ")


class SafeSession(object):
    """包装真实 scoped_session：
       - DDL 默认拦截并抛错（ALLOW_DDL=1 才放行）
       - 默认不真正 commit（dry），CF_DB_SAVE=1 才落库；结束由 launcher 回滚未提交事务
    """

    bind = None

    def __init__(self, real_session, allow_ddl=False, save=False):
        self._s = real_session
        self._allow_ddl = allow_ddl
        self._save = save

    def _check_ddl(self, sql):
        s = " ".join(str(sql).split())
        up = s.upper()
        for kw in _DDL_KEYWORDS:
            if kw in up:
                if not self._allow_ddl:
                    raise RuntimeError(
                        "DDL 被拦截（默认安全）：%s ...\n"
                        "如需执行请设置 ALLOW_DDL=1（且仅在测试库！默认不落库，结束回滚）。"
                        % s[:140])
                return

    def execute(self, sql, *a, **k):
        self._check_ddl(sql)
        return self._s.execute(sql, *a, **k)

    def commit(self):
        if not self._save:
            logging.info("[SAFE] dry commit -> 跳过（未落库）。设置 CF_DB_SAVE=1 才真正提交。")
            return
        logging.info("[SAFE] 真正提交事务（CF_DB_SAVE=1）。")
        self._s.commit()

    def rollback(self):
        self._s.rollback()

    def __getattr__(self, name):
        # 透传其它方法；bind 已显式定义为 None
        return getattr(self._s, name)


def make_db(db_url, allow_ddl=False, save=False):
    """按 CF_DB_URL 建真实 scoped_session，并包成 SafeSession。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker
    eng = create_engine(db_url, pool_pre_ping=True)
    return SafeSession(scoped_session(sessionmaker(bind=eng)), allow_ddl=allow_ddl, save=save)


# ───────── 模型B：service / common.utils / environment shim 包 ─────────
def install_model_b_shims():
    """注入本地 shim 包，使模型B 源码里的 import 解析成功。
    依赖 _RUNTIME['customer_util']、_SETTINGS['company_id']、_SETTINGS['db'] 已配置。
    """
    # service
    if "service" not in sys.modules:
        svc = types.ModuleType("service")
        svc.BasePrivateApiService = BasePrivateApiService
        sys.modules["service"] = svc

    # common / common.utils —— 忠实复刻 store/downloads/895 customer_util.py 暴露的工具类
    if "common" not in sys.modules:
        sys.modules["common"] = types.ModuleType("common")
    if "common.utils" not in sys.modules:
        cu = types.ModuleType("common.utils")

        # 底层真实/离线网关（由 launcher.configure(customer_util=...) 注入）
        _cu = _RUNTIME.get("customer_util")

        class CustomerUtil(object):
            """复刻 common.utils.CustomerUtil：直接代理到调试网关（_RUNTIME['customer_util']）。"""
            def call_open_api(self, api_name, param=None):
                return _cu.call_open_api(api_name, param)

            def get_current_context(self):
                return _cu.get_current_context()

            def call_llm(self, name, params=None, alter_message=None, use_cache=False, is_reasoning=False):
                return _cu.call_llm(name, params, alter_message, use_cache, is_reasoning)

            def call_llm_service(self, system_prompt, query, model_name=None, model_type=None, temperature=0.5):
                return _cu.call_llm_service(system_prompt, query, model_name, model_type, temperature)

            def safeEval(self, script, param):
                return _cu.safeEval(script, param)

        cu.CustomerUtil = CustomerUtil()

        # 错误聚合（忠实于 common.utils.errors：提供常用异常类型，便于 from common.utils import errors 解析）
        class _AppException(Exception):
            def new(self, *a, **k):
                return self
        cu.errors = types.SimpleNamespace(
            AppException=_AppException, DATA_RULE_ERROR=_AppException,
            DATA_NOT_FOUND=_AppException, DATA_CONVERT_ERROR=_AppException,
        )

        # ── ModelUtil：忠实复刻，全部委托到 cu.CustomerUtil.call_open_api ──
        def _divide_list(lst, group=300):
            out = []
            for i in range(0, len(lst), group):
                out.append(lst[i:i + group])
            return out

        class ModelUtil(object):
            @classmethod
            def get(cls, model_, id_, **kwargs):
                return cu.CustomerUtil.call_open_api("hcm.model.get", dict({"model": model_, "id_": id_}, **kwargs))

            @classmethod
            def list(cls, model, filter_dict, state=None, fields=None, fields_key=None, sorts=None,
                     extra_property=None, query_str="", filter_str=None, page_index=None, page_size=None):
                extra_ = dict(extra_property) if extra_property else {}
                if "only_list" not in extra_:
                    extra_["only_list"] = True
                if state:
                    extra_["state"] = state
                if fields:
                    extra_["fields"] = fields
                if fields_key:
                    extra_["fields"] = [{"key": fk, "field": fk.split(".")} for fk in fields_key]
                if sorts:
                    extra_["sorts"] = sorts
                page_index = page_index if page_index else 1
                page_size = page_size if page_size else 10000
                result = cu.CustomerUtil.call_open_api("hcm.model.list", {
                    "model": model, "page_size": page_size, "page_index": page_index,
                    "query_str": query_str, "filter_str": filter_str,
                    "filter_dict": filter_dict, "extra_property": extra_})
                if extra_["only_list"]:
                    result = result["list"]
                return result

            @classmethod
            def list_ids(cls, model, filter_dict, state=None, fields=None, fields_key=None, sorts=None,
                         extra_property=None, query_str="", filter_str=None, page_index=None, page_size=None):
                extra_ = dict(extra_property) if extra_property else {}
                if "only_list" not in extra_:
                    extra_["only_list"] = True
                if state:
                    extra_["state"] = state
                if fields:
                    extra_["fields"] = fields
                if fields_key:
                    extra_["fields"] = [{"key": fk, "field": fk.split(".")} for fk in fields_key]
                if sorts:
                    extra_["sorts"] = sorts
                extra_["only_id"] = True
                page_index = page_index if page_index else 1
                page_size = page_size if page_size else 1000000
                result = cu.CustomerUtil.call_open_api("hcm.model.list", {
                    "model": model, "page_size": page_size, "page_index": page_index,
                    "query_str": query_str, "filter_str": filter_str,
                    "filter_dict": filter_dict, "extra_property": extra_})["list"]
                return [item.get("id") for item in result]

            @classmethod
            def count_value(cls, model, filter_dict, state=None, fields=None, sorts=None, extra_property=None,
                            query_str="", filter_str=None):
                extra_ = dict(extra_property) if extra_property else {}
                extra_["only_list"] = True
                if state:
                    extra_["state"] = state
                if fields:
                    extra_["fields"] = fields
                if sorts:
                    extra_["sorts"] = sorts
                param = {"model": model, "filter_dict": filter_dict, "count_by": "id",
                         "query_str": query_str, "filter_str": filter_str, "extra_property": extra_}
                return cu.CustomerUtil.call_open_api("hcm.model.count", param)["list"][0]["id"]

            @classmethod
            def count(cls, model, filter_dict, state=None, fields=None, sorts=None, extra_property=None,
                      query_str="", filter_str=None, group_by=None, count_by=None, distinct_count_by=None,
                      sum_by=None, avg_by=None, min_by=None, max_by=None, page_index=None, page_size=None):
                extra_ = dict(extra_property) if extra_property else {}
                if "only_list" not in extra_:
                    extra_["only_list"] = True
                if state:
                    extra_["state"] = state
                if fields:
                    extra_["fields"] = fields
                if sorts:
                    extra_["sorts"] = sorts
                page_index = page_index if page_index else 1
                page_size = page_size if page_size else 10000
                param = {"model": model, "filter_dict": filter_dict, "query_str": query_str,
                         "filter_str": filter_str, "group_by": group_by, "count_by": count_by,
                         "distinct_count_by": distinct_count_by, "page_index": page_index,
                         "page_size": page_size, "sum_by": sum_by, "avg_by": avg_by,
                         "min_by": min_by, "max_by": max_by, "extra_property": extra_}
                result = cu.CustomerUtil.call_open_api("hcm.model.count", param)
                if extra_["only_list"]:
                    result = result["list"]
                return result

            @classmethod
            def create(cls, model, info, role=None):
                return cu.CustomerUtil.call_open_api("hcm.model.create", {"model": model, "info": info, "role": role})

            @classmethod
            def create_batch(cls, model, info_list, role=None):
                if not info_list:
                    return
                result = []
                for group in _divide_list(info_list, group=300):
                    result += cu.CustomerUtil.call_open_api("hcm.model.create.batch",
                        {"model": model, "info_list": group, "role": role})["result"]
                return result

            @classmethod
            def edit(cls, model, id_, info, role=None):
                return cu.CustomerUtil.call_open_api("hcm.model.edit", {"model": model, "id_": id_, "info": info, "role": role})

            @classmethod
            def edit_batch(cls, model, edit_list, role=None):
                if not edit_list:
                    return
                result = []
                for group in _divide_list(edit_list, group=300):
                    result += cu.CustomerUtil.call_open_api("hcm.model.edit.batch",
                        {"model": model, "info_list": group, "role": role})
                return result

            @classmethod
            def edit_batch_simple(cls, model, edit_list, role=None):
                logging.warning("[cfdebug] ModelUtil.edit_batch_simple 在调试沙箱不支持（需真实引擎 ModelFactory）。")
                return None

            @classmethod
            def remove(cls, model, id_, role=None):
                if not id_:
                    return
                return cu.CustomerUtil.call_open_api("hcm.model.remove", {"model": model, "id_": id_, "role": role})

            @classmethod
            def remove_batch(cls, model, ids, role=None):
                if not ids:
                    return
                for group in _divide_list(ids, group=500):
                    cu.CustomerUtil.call_open_api("hcm.model.remove.batch", {"model": model, "ids": group, "role": role})

            @classmethod
            def record(cls, category, content, type_=3, enabled=True):
                if not enabled:
                    return
                logging.info("[cfdebug] ModelUtil.record: %s | %s", category, content)

        cu.ModelUtil = ModelUtil

        # ── DBUtil / SapUtil / DataUtil：忠实签名；连接/重依赖在调用时才需驱动，导入即可解析 ──
        class DBUtil(object):
            __db_factory__ = {"pymysql": "_mysql", "dm": "_dm", "psy": "_postgresql"}
            def __init__(self, db_type=None, **kwargs):
                logging.info("[cfdebug] DBUtil(%s) 在调试沙箱默认不建立真实连接。", db_type)
                self.db_type = db_type
                self.kwargs = kwargs
            def __enter__(self):
                raise NotImplementedError("调试沙箱未建立真实数据库连接：DBUtil 需要对应 DBAPI 驱动且需设置 CF_DB_URL。")
            def __exit__(self, *a):
                pass

        class SapUtil(object):
            @classmethod
            def call(cls, api, param, sap_type="sap"):
                logging.info("[cfdebug] SapUtil.call(%s) 在调试沙箱为 no-op。", sap_type)
                return {"success": False, "message": "调试沙箱不支持 SapUtil"}
            @classmethod
            def get_connection(cls, sap_type="sap"):
                return None
            @classmethod
            def get_setting(cls, sap_type="sap"):
                return {"success": False, "message": "调试沙箱不支持 SapUtil"}

        class DataUtil(object):
            @classmethod
            def convert_num_to_id(cls, model_name, num, default="None"):
                logging.warning("[cfdebug] DataUtil.convert_num_to_id 在调试沙箱不支持（需 ModelFactory）。")
                if default == "None":
                    raise RuntimeError("调试沙箱不支持 DataUtil.convert_num_to_id")
                return default

        cu.DBUtil = DBUtil
        cu.SapUtil = SapUtil
        cu.DataUtil = DataUtil

        # ── 线程缓存 / Redis：用进程内字典做可用兜底，保证调试期调用不报错 ──
        _thread_cache = {}
        class CustomerThreadCacheUtil(object):
            @classmethod
            def has_cache(cls, key):
                return key in _thread_cache
            @classmethod
            def get_cache(cls, key, default=None, section=None):
                return _thread_cache.get(key, default)
            @classmethod
            def set_cache(cls, key, value):
                _thread_cache[key] = value
            @classmethod
            def clear_cache(cls):
                _thread_cache.clear()
        cu.CustomerThreadCacheUtil = CustomerThreadCacheUtil

        _redis_cache = {}
        class CustomerRedisUtil(object):
            @classmethod
            def get_key(cls, company_id, key):
                return _redis_cache.get(f"{company_id}:{key}")
            @classmethod
            def set_key(cls, company_id, key, value, expire=100):
                _redis_cache[f"{company_id}:{key}"] = value
            @classmethod
            def del_key(cls, company_id, key):
                return _redis_cache.pop(f"{company_id}:{key}", None)
        cu.CustomerRedisUtil = CustomerRedisUtil

        sys.modules["common.utils"] = cu

    # environment
    if "environment" not in sys.modules:
        env = types.ModuleType("environment")
        db = _SETTINGS.get("db")
        engine = getattr(db, "_s", None)  # SafeSession 内部真实 session 的 engine
        real_engine = None
        if engine is not None:
            real_engine = getattr(engine, "bind", None) or engine
        env.environment = SimpleNamespace(e=real_engine, initialize=lambda *a, **k: None)
        env.e = real_engine
        env.initialize = lambda *a, **k: None
        sys.modules["environment"] = env

    # dummy 导入器：兜底 core.* / libs.* / util 等未提供的顶层包，避免 exec 崩溃
    if not any(isinstance(f, _DummyFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _DummyFinder())


class _DummyLoader(object):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        pass


class _DummyFinder(object):
    def find_spec(self, fullname, path, target=None):
        import importlib.util
        if fullname.split(".")[0] in ("core", "libs", "util"):
            return importlib.util.spec_from_loader(fullname, _DummyLoader())
        return None


# ───────── 统一执行入口 ─────────
def run_model_a(path, entry=None, kwargs=None):
    """模型A（零 import 上传沙箱函数）：剥 import + 注入沙箱全局 + 真实路径 exec。"""
    kwargs = kwargs or {}
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    src = strip_imports(src)
    entry = entry or _first_top_class(src)
    NS = build_sandbox_globals()
    NS["__name__"] = "__main__"
    exec(compile(src, path, "exec"), NS)
    Cls = NS[entry]
    return Cls().execute(**kwargs)


def run_model_b(path, entry=None, kwargs=None):
    """模型B（应用内模块函数，自带 import）：shim 解析 import + 真实路径 exec。"""
    kwargs = kwargs or {}
    install_model_b_shims()
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    entry = entry or _first_top_class(src)
    NS = {"__name__": "__main__"}
    exec(compile(src, path, "exec"), NS)
    Cls = NS[entry]
    return Cls().execute(**kwargs)


def detect_model(path):
    """扫描源码判断执行上下文：含 service/common.utils/environment import -> 模型B。"""
    try:
        src = open(path, "r", encoding="utf-8").read()
    except Exception:
        return "A"
    for marker in ("from service import", "import service",
                   "from common.utils import", "from environment import",
                   "from core.", "from libs.", "from util import"):
        if marker in src:
            return "B"
    return "A"
