# -*- coding: utf-8 -*-
"""
云函数：private.cf_error_locator —— 云函数报错后的「对象数据反查」诊断工具。

作用
----
云函数报错后，运维手上通常只有 error_code 或一句 [定位] 文本。本函数让你用
「模型 + 对象ID + 字段」反查该对象**当前**的字段值，回答两个关键问题：
  1. 这个字段现在到底有没有值？（空 → 与报错一致；有值 → 报错后被人补上了）
  2. 值长什么样？（脱敏后展示，便于判断是格式问题还是缺失问题）

配合 jira-git-gui 的「云函数错误定位」面板：
  面板解析出 model/id/field 后，点「查询该对象当前字段值」就是调本函数（走
  hcm.model.get）。本函数是它的服务端等价物，供没有打开 GUI 的运维直接调用。

注册
----
  类型：私有 API（private_api）
  函数名称：private.cf_error_locator
  执行入口（脚本第一个顶层类）：CfErrorLocator

入参（execute 的 kwargs）
------------------------
  model      必填，如 'employee'
  object_id  必填，如 '5841977'
  field      可选，字段名；不给则返回该对象的全部字段（脱敏）
  raw        可选，'1' 表示返回未脱敏原值（默认脱敏，谨慎开启）

返回
----
  {
    "ok": true,
    "object_id": "5841977",
    "model": "employee",
    "field": "id_card",           # 传了 field 才有
    "value": "3401****1234",      # 脱敏后
    "present": false,             # 该字段是否有值
    "type": "str",
    "fields": {...}               # 未传 field 时返回全量字段（脱敏）
  }

注意
----
- 本函数是**新增脚本**，不改 hcm-core 框架源码，也不改任何其它云函数。
- 依赖沙箱注入的 BasePrivateApiService / AppException / CustomerUtil。
  若沙箱未注入 CustomerUtil，请在顶部加：
      from core.util import CustomerUtil
  并确保 loader 的 safe_import 白名单包含该模块。
- 敏感字段（身份证/手机号）默认脱敏；raw=1 才会返回原值，请仅在排查必要时使用。
"""

SENSITIVE_HINTS = ('id_card', 'card', 'mobile', 'phone', 'tel', 'bank', 'account',
                   'identity', 'idno', 'id_no', 'cert', 'ssn')


def _mask(v, raw=False):
    if v is None or v == '':
        return None
    if raw:
        return v
    s = v if isinstance(v, str) else str(v)
    if len(s) > 12:
        return s[:6] + '****' + s[-4:]
    # 短值也做轻度脱敏（如 11 位手机号保留前3后2）
    if len(s) > 6:
        return s[:3] + '****' + s[-2:]
    return s


def _is_sensitive(field):
    f = (field or '').lower()
    return any(h in f for h in SENSITIVE_HINTS)


class CfErrorLocator(BasePrivateApiService):
    def execute(self, **kwargs):
        model = kwargs.get('model')
        object_id = kwargs.get('object_id') or kwargs.get('id')
        field = kwargs.get('field')
        raw = str(kwargs.get('raw') or '') == '1'

        if not model or not object_id:
            raise AppException(400, 400014,
                               "[定位] model=? id=? field=? value=null stage=locator "
                               "|| 缺少 model / object_id 入参")

        self.log(f"[INFO] cf_error_locator model={model} object_id={object_id} field={field}")

        try:
            rec = CustomerUtil.call_open_api(
                "hcm.model.get", {"model": model, "id": object_id}
            )
        except Exception as e:
            raise AppException(
                500, 17003,
                f"[定位] model={model} id={object_id} field=? value=null stage=locator "
                f"|| 查询对象失败：{type(e).__name__}: {e}"
            )

        # 部分网关会把记录包在 list/items 里，做一层归一化
        if isinstance(rec, dict) and 'list' in rec and isinstance(rec['list'], list):
            rec = rec['list'][0] if rec['list'] else {}
        if not isinstance(rec, dict):
            raise AppException(
                500, 17003,
                f"[定位] model={model} id={object_id} field=? value=null stage=locator "
                f"|| 返回记录格式异常：{type(rec).__name__}"
            )

        out = {
            "ok": True,
            "model": model,
            "object_id": object_id,
        }

        if field:
            v = rec.get(field)
            out.update({
                "field": field,
                "value": _mask(v, raw if not _is_sensitive(field) else raw),
                "present": not (v is None or v == ''),
                "type": type(v).__name__,
            })
            if _is_sensitive(field) and not raw:
                out["masked"] = True
            return out

        out["fields"] = {
            k: _mask(v, raw if not _is_sensitive(k) else raw)
            for k, v in rec.items()
        }
        out["field_count"] = len(rec)
        return out
