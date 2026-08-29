# -*- coding: utf-8 -*-
r"""HCM 云函数「统一日志」SDK —— cf_diag()，自包含、零 import。

解决的问题（G3：日志格式不统一）
--------------------------------
实测各云函数写 dynamic_log 的写法五花八门：

    org_position_plugin.py :  "[PositionPlugin:%s] %s" % (rid, msg)
    wf_notify_specified.py :  "[RID:{}] {}".format(rid, msg)
    js_Kf_points.py        :  "[RID:129837] [POINTS_MODULE] [WRITE] ..."
    另有大量函数直接 repr(dict) 塞进 content

后端 `parse_cf_log_content()` 只能靠正则猜测，遇到新格式就得改解析规则。
本 SDK 约定一种**机器可解析且人类可读**的规范格式，一次约定，处处适用。

规范格式
--------
    [DIAG][<LEVEL>][stage:<阶段>][model:<模型>][id:<对象ID>][field:<字段>] <消息正文>

示例：

    [DIAG][INFO][stage:fetch][model:Employee][id:23178667] 查询员工成功
    [DIAG][ERROR][stage:field_read][model:Employee][id:23178667][field:id_card] 身份证号为空

设计取舍
--------
1. 用 `[KEY:VALUE]` 而非 `[KEY=VALUE]`：后端标签正则只接受 ``[\w:.\-]+``，
   `=` 会被当作正文吃掉。`:` 是既有的键值分隔符（如 `[RID:129837]`）。
2. 级别用裸标签 `[INFO]` / `[ERROR]`：后端已有逻辑直接从标签识别级别，
   零改造即可生效。
3. `[DIAG]` 作为「这是标准化日志」的哨兵：后端据此走精确解析分支，
   不再走猜测逻辑；不带该标签的老日志仍走兼容分支，不破坏存量。
4. 敏感字段走 `_mask()` 脱敏，与 locate_snippet 一致。
5. 写日志永不抛异常：日志是旁路，失败不能影响业务主流程。

用法（三档）
------------
【最小】替换散落的 log() 方法：

    def log(self, msg, level='INFO', stage='', **kw):
        cf_diag('my_func', msg, level=level, stage=stage, rid=self.rid, **kw)

【推荐】在关键节点打点（配合 locate_snippet 的 _cur/_step）：

    def _run(self, **kwargs):
        self._step = 'fetch'
        cf_diag('my_func', '开始拉取员工', stage='fetch', model='Employee')
        for emp in emp_list:
            self._cur = emp
            cf_diag('my_func', '处理员工', stage='process',
                    model='Employee', oid=emp.get('id'))

【进阶】错误现场一键落盘（自动带 traceback 与定位）：

    try:
        ...
    except Exception as e:
        cf_diag_error('my_func', e, self, stage='process')
        raise

与 locate_snippet.py 的关系
---------------------------
两者可独立使用，也可一起注入：
- locate_snippet 负责「抛错时」把定位信息编码进 err_msg
- cf_diag 负责「平时」把执行过程写进 dynamic_log
配合使用时，一次报错能同时看到「过程日志」和「错误现场」。
"""
import json
import traceback

# <<< CF_DIAG_BEGIN >>>

# 合法级别（后端 parse_cf_log_content 已识别这几个）
_DIAG_LEVELS = ("DEBUG", "INFO", "WARN", "WARNING", "ERROR")


def _mask(v):
    """敏感字段脱敏：与 locate_snippet._mask 保持一致。"""
    if v is None or v == "":
        return "null"
    s = v if isinstance(v, str) else str(v)
    if len(s) > 12:
        return s[:6] + "****" + s[-4:]
    return s


def _tok(v):
    """压成不含空格/方括号的 token，避免破坏标签结构。"""
    s = _mask(v)
    for ch in (" ", "\t", "\n", "[", "]"):
        s = s.replace(ch, "_")
    return s


def diag_format(level="INFO", stage="", model="", oid="", field="",
                rid="", dept_id="", msg="", **extra):
    """构造规范化的日志 content 文本（纯函数，便于单元测试）。"""
    level = (level or "INFO").upper()
    if level == "WARN":
        level = "WARNING"
    if level not in _DIAG_LEVELS:
        level = "INFO"
    parts = ["[DIAG]", "[%s]" % level]
    for key, val in (("stage", stage), ("model", model), ("id", oid),
                     ("field", field), ("rid", rid), ("dept", dept_id)):
        if val not in (None, "", "?"):
            parts.append("[%s:%s]" % (key, _tok(val)))
    head = "".join(parts)
    body = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
    if extra:
        body = "%s %s" % (body, json.dumps(extra, ensure_ascii=False, sort_keys=True))
    return "%s %s" % (head, body) if body else head


def cf_diag(log_type, msg, level="INFO", stage="", model="", oid="",
            field="", rid="", dept_id="", **extra):
    """写一条标准化日志到 dynamic_log。

    返回生成的 content 文本（便于调用方 print 或二次处理）。
    日志写入失败时静默返回，绝不影响业务流程。
    """
    content = diag_format(level=level, stage=stage, model=model, oid=oid,
                          field=field, rid=rid, dept_id=dept_id,
                          msg=msg, **extra)
    try:
        CustomerUtil.call_open_api("hcm.model.create", {
            "model": "dynamic_log",
            "info": {"log_type": log_type, "content": content},
        })
    except Exception:
        pass  # 日志是旁路，失败不能影响业务
    return content


def cf_diag_error(log_type, exc, self=None, stage="", model="", oid="", field=""):
    """错误现场一键落盘：级别 ERROR，正文带异常类型和消息，附带 traceback。

    ``self`` 传入云函数实例时，自动从 ``_cur`` / ``_step`` 补全定位信息
    （与 locate_snippet 的打点约定一致）。
    """
    cur = getattr(self, "_cur", None) if self is not None else None
    step = stage or (getattr(self, "_step", None) if self is not None else "") or ""
    if cur is not None and not model:
        model = (cur.get("__meta_model") if isinstance(cur, dict) else
                 getattr(cur, "__meta_model", None)) or ""
    if cur is not None and not oid:
        oid = (cur.get("id") if isinstance(cur, dict) else getattr(cur, "id", None)) or ""
    msg = "%s: %s" % (type(exc).__name__, exc)
    try:
        tb = traceback.format_exc()
    except Exception:
        tb = ""
    errcode = getattr(exc, "errcode", None) or getattr(exc, "code", None)
    extra = {"traceback": tb[-1500:]} if tb and tb.strip() != "NoneType: None" else {}
    if isinstance(errcode, int):
        extra["errcode"] = errcode
    return cf_diag(log_type, msg, level="ERROR", stage=step, model=model,
                   oid=oid, field=field, **extra)


def cf_diag_progress(log_type, done, total, stage="", model="", msg=""):
    """批量处理进度打点：避免逐条刷屏，按 10% 或首尾打印。

    返回 True 表示本次实际写了日志，False 表示被节流跳过。
    """
    total = total or 0
    if total <= 0:
        return False
    if done not in (1, total) and total > 10 and done % max(1, total // 10) != 0:
        return False
    cf_diag(log_type, msg or "进度 %s/%s" % (done, total), level="INFO",
            stage=stage, model=model, **{"done": done, "total": total})
    return True


# <<< CF_DIAG_END >>>
