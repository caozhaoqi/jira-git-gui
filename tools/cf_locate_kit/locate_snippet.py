# -*- coding: utf-8 -*-
"""
HCM 云函数「错误定位」snippet —— 自包含、零 import、直接粘贴到云函数顶部即可用。

设计前提（约束：不改 hcm-core 框架源码）
------------------------------------------
1. `handlers.py:477` 给每次报错生成毫秒级 error_code，但默认 hide_error_msg=True 会把
   traceback 剥掉、只留「错误号」。**唯一能稳定透传到前端的通道是 err_msg 与 description**
   （handlers.py:544-554 重建 AppException 时只保留这两项）。
   因此本 snippet 把定位信息**编码进 err_msg 文本**，格式与 jira-git-gui 的
   「云函数错误定位」面板严格对齐：

       [定位] model=<模型> id=<对象ID> field=<字段> value=<值> stage=<阶段> || <人话原因>

2. `AppException` 由云函数沙箱注入（与 BasePrivateApiService 同批），本 snippet 零 import
   直接使用。若部署环境沙箱未注入，请在云函数顶部加 `from errors import AppException`
   并确保 loader 的 safe_import 白名单包含 errors 模块。

3. 面板解析依赖空格分词，因此 value 必须**不带空格**（已用 _tok() 保证）。

用法（三档，按需取用）
--------------------
【最小改动】把裸取值换成 safe_get：
    id_card = emp.get('id_card')                      # before
    id_card = safe_get(emp, 'id_card', '身份证号')     # after  → 缺失即抛带定位的错

【推荐】循环里打点 + execute 包一层 try（连 KeyError/TypeError/ZeroDivision 也能定位）：
    def execute(self, **kwargs):
        self._cur = None; self._step = None
        try:
            return self._run(**kwargs)
        except AppException:
            raise
        except Exception as e:
            raise locate_guard(self, e)               # 用 _cur/_step 兜底定位
    def _run(self, **kwargs):
        for emp in emp_list:
            self._cur = emp; self._step = '计算积分'
            id_card = safe_get(emp, 'id_card', '身份证号')
            ...

【进阶】字段值校验：
    assert_field(emp, 'id_card', lambda v: len(v) == 18, '身份证号必须为18位')
"""

# <<< CF_LOCATE_BEGIN >>>
# ---------------------------------------------------------------- 内部工具

def _mask(v):
    """敏感字段脱敏：身份证/手机号等只保留首尾，避免错误日志泄露隐私。"""
    if v is None or v == '':
        return 'null'
    s = v if isinstance(v, str) else str(v)
    if len(s) > 12:
        return s[:6] + '****' + s[-4:]
    return s


def _tok(v):
    """把值压成「不含空格」的单个 token，保证面板按空格分词能正确解析。"""
    s = _mask(v)
    return s.replace(' ', '_').replace('\t', '_').replace('\n', '_')


def _obj_field(obj, field):
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _obj_meta_model(obj):
    if isinstance(obj, dict):
        return obj.get('__meta_model') or obj.get('meta_model') or obj.get('model') or '?'
    return getattr(obj, '__meta_model', None) or '?'


def _obj_id(obj):
    if isinstance(obj, dict):
        return obj.get('id') or obj.get('object_id') or obj.get('_id') or '?'
    return getattr(obj, 'id', None) or '?'


def _loc_msg(model, oid, field, value, stage, reason):
    """构造面板可解析的 [定位] 文本（value 已脱敏且无空格）。"""
    return (f"[定位] model={model} id={oid} field={field} "
            f"value={_tok(value)} stage={stage} || {reason}")


# ---------------------------------------------------------------- 对外 API

def locate(model, oid, field, value, reason, stage='field_read', code=400014):
    """
    抛出「带定位信息」的 AppException。

    err_msg 与 description 都写入 [定位] 文本 —— 因为默认 hide_error_msg 只保留这两项。
    若已配置 hcm_cloud.hide_error_msg=False，则 error_info 也会透传到前端（结构化展示）。
    """
    txt = _loc_msg(model, oid, field, value, stage, reason)
    exc = AppException(400, code, txt).description(txt)
    try:
        exc.add_info('error_info', {
            'model': model, 'object_id': oid, 'field': field,
            'value': _mask(value), 'stage': stage,
        })
    except Exception:
        pass  # 某些沙箱版本的 AppException 可能无 add_info，忽略即可（err_msg 已够用）
    return exc


def safe_get(obj, field, alias='', stage='field_read', required=True, model=None):
    """
    读取对象字段；required=True 且取值为空时，抛出带定位信息的异常。
    这是替代 obj.get('x') 的直接写法，零额外成本即可让错误自带对象/字段。
    """
    v = _obj_field(obj, field)
    if required and (v is None or v == ''):
        raise locate(model or _obj_meta_model(obj), _obj_id(obj), field, v,
                     f"{alias or field} 为空，无法继续执行", stage)
    return v


def assert_field(obj, field, predicate, reason='', stage='field_validate', model=None):
    """字段值校验：不满足 predicate 时抛带定位的异常。"""
    v = _obj_field(obj, field)
    try:
        ok = bool(predicate(v))
    except Exception as e:
        ok = False
        reason = f"{reason}（校验函数异常：{type(e).__name__}: {e}）"
    if not ok:
        raise locate(model or _obj_meta_model(obj), _obj_id(obj), field, v,
                     reason or f"{field} 校验不通过", stage)
    return v


def locate_guard(self, exc, stage=None):
    """
    execute 的兜底包装：把「裸异常」转成带定位的 AppException。
    依赖你在业务循环里打点 self._cur（当前对象）与 self._step（当前步骤）。
    """
    cur = getattr(self, '_cur', None)
    step = stage or getattr(self, '_step', None) or 'unknown'
    if cur is None:
        raise AppException(500, 17003,
                           f"[定位] model=? id=? field=? value=null stage={step} || "
                           f"{type(exc).__name__}: {exc}")
    return locate(_obj_meta_model(cur), _obj_id(cur), '?', None,
                  f"执行到「{step}」时异常：{type(exc).__name__}: {exc}", stage=step)


# execute 兜底模板（复制进云函数类，把原 execute 体改名 _run）：
#
#     def execute(self, **kwargs):
#         self._cur = None
#         self._step = None
#         try:
#             return self._run(**kwargs)
#         except AppException:
#             raise
#         except Exception as e:
#             raise locate_guard(self, e)
#
#     def _run(self, **kwargs):
#         # ↓ 原 execute 的业务逻辑搬到这里
#         ...

# <<< CF_LOCATE_END >>>
