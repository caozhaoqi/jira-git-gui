# -*- coding: utf-8 -*-
"""
云函数本地调试：常用模型离线 Mock 数据（api/cfdebug/mock_data.py）
================================================================
用途：env=mock 时，MockCustomerUtil 按 model 名从这里取样本数据，
      完全离线、不连测试环境，适合纯逻辑单测 / 断点验证。

数据来源：扫描 hcm-core/cloud_functions 得到的「云函数最常引用的模型」Top 清单
（共 110 个 token，去噪后约 45 个真实 HCM 模型）。覆盖出现频率最高的常用模型；
未在此显式 seed 的模型，MockCustomerUtil 会回退为「空列表 + 接受写登记」。

注意：
  - 样本字段是「代表性占位」，不保证与测试环境真实字段 100% 一致；
    Mock 模式只验证【逻辑流程 / 断点 / 分支】，不做字段准确性校验。
  - 加密字段（如 identity_card）一律不放明文，避免误导；如需测解密路径请用真实环境模式。
  - 每行必须带 id（云函数几乎都按 id 定位记录）。
"""
import os
import logging
from types import SimpleNamespace


DEBUG_ID = os.environ.get("DEBUG_ID")


def _seed():
    """返回 {model: [row, ...]}。"""
    d = {}

    def add(model, *rows):
        d[model] = [dict(r) for r in rows]

    # ── 薪酬 / 绩效 ──
    add("SalaryResultData",
        {"id": 1, "month": "2026-07", "u_month_end": None, "employee_id": 101},
        {"id": 2, "month": "2026-02", "u_month_end": None, "employee_id": 102},
        {"id": 3, "month": "2026-13", "u_month_end": None, "employee_id": 103},
        {"id": 4, "month": "2026-05", "u_month_end": "2026-05-31", "employee_id": 104},
    )
    add("SalaryProgram",
        {"id": 1, "name": "2026薪酬方案", "status": "enabled"},
        {"id": 2, "name": "试用期方案", "status": "enabled"},
    )
    add("SalaryProgramItem",
        {"id": 1, "program_id": 1, "name": "基本工资", "amount": 8000},
        {"id": 2, "program_id": 1, "name": "绩效工资", "amount": 3000},
    )
    add("PerfResult",
        {"id": 1, "employee_id": 101, "period": "2026-H1", "score": 92},
        {"id": 2, "employee_id": 102, "period": "2026-H1", "score": 85},
    )
    add("BudgetDataMonth", {"id": 1, "month": "2026-07", "amount": 120000})
    add("BudgetResultData", {"id": 1, "month": "2026-07", "amount": 118500})

    # ── 人员主数据 ──
    add("Employee",
        {"id": 101, "employee_code": "E0001", "name": "张三", "status": "active",
         "org_id": 11, "job_id": 21, "entry_date": "2020-03-01"},
        {"id": 102, "employee_code": "E0002", "name": "李四", "status": "active",
         "org_id": 12, "job_id": 22, "entry_date": "2021-06-15"},
        {"id": 103, "employee_code": "E0003", "name": "王五", "status": "left",
         "org_id": 11, "job_id": 23, "entry_date": "2019-09-01"},
    )
    add("PreEmployee",
        {"id": 201, "name": "赵六", "mobile": "13800000001", "status": "pending"},
        {"id": 202, "name": "钱七", "mobile": "13800000002", "status": "pending"},
    )
    add("User",
        {"id": 1, "username": "admin", "mobile": "666666", "employee_id": 101},
        {"id": 2, "username": "user1", "mobile": "666667", "employee_id": 102},
    )
    add("FamilyInformation",
        {"id": 1, "employee_id": 101, "relation": "配偶", "name": "孙氏"},
        {"id": 2, "employee_id": 102, "relation": "子女", "name": "小李"},
    )
    add("EmployeeEducation",
        {"id": 1, "employee_id": 101, "school": "清华大学", "degree": "本科"},
        {"id": 2, "employee_id": 102, "school": "北京大学", "degree": "硕士"},
    )
    add("QualificationInformation",
        {"id": 1, "employee_id": 101, "name": "注册会计师", "level": "高级"},
    )
    add("Certificate",
        {"id": 1, "employee_id": 101, "name": "PMP", "no": "C-001"},
        {"id": 2, "employee_id": 102, "name": "软考", "no": "C-002"},
    )
    add("CertBorrow",
        {"id": 1, "employee_id": 101, "cert_id": 1, "borrow_date": "2026-01-10"},
    )
    add("CertBorrowDetailInfo",
        {"id": 1, "borrow_id": 1, "cert_id": 1, "return_date": None},
    )
    add("TechnicalSkills",
        {"id": 1, "employee_id": 101, "skill": "Python", "level": "熟练"},
    )
    add("EmpWorkResume",
        {"id": 1, "employee_id": 101, "company": "A公司", "title": "工程师"},
    )
    add("EmployeeAction",
        {"id": 1, "employee_id": 101, "action": "转正", "date": "2020-09-01"},
    )
    add("EmployeeResume",
        {"id": 1, "employee_id": 101, "summary": "十年经验"},
    )
    add("EmployeePunishmentInfo",
        {"id": 1, "employee_id": 103, "type": "警告", "date": "2025-11-01"},
    )
    add("RewardInformation",
        {"id": 1, "employee_id": 101, "type": "优秀员工", "date": "2025-12-01"},
    )
    add("ContractInformation",
        {"id": 1, "employee_id": 101, "type": "固定期限", "start": "2020-03-01",
         "end": "2023-02-28", "u_contractpost": None},
        {"id": 2, "employee_id": 102, "type": "无固定", "start": "2021-06-15", "end": None},
    )
    add("ContractAuthority",
        {"id": 1, "name": "甲方A", "code": "CA01"},
    )
    add("ContractFirstParty",
        {"id": 1, "name": "甲方主体", "code": "FP01"},
    )

    # ── 组织 / 岗位 ──
    add("OrgUnit",
        {"id": 11, "name": "研发部", "parent_id": 1, "code": "RD"},
        {"id": 12, "name": "市场部", "parent_id": 1, "code": "MKT"},
    )
    add("OrgDepartment",
        {"id": 11, "name": "研发部", "parent_id": 1, "code": "RD", "manager_id": 101},
        {"id": 12, "name": "市场部", "parent_id": 1, "code": "MKT", "manager_id": 102},
        {"id": 1, "name": "总公司", "parent_id": None, "code": "HQ"},
    )
    add("OrgDepartmentHistory",
        {"id": 1, "dept_id": 11, "name": "研发部", "effective_date": "2020-01-01"},
    )
    add("Department",
        {"id": 11, "name": "研发部", "org_id": 11},
    )
    add("DepartmentBusinessTeam",
        {"id": 1, "dept_id": 11, "name": "算法组"},
        {"id": 2, "dept_id": 12, "name": "投放组"},
    )
    add("OrgPosition",
        {"id": 21, "name": "高级工程师", "dept_id": 11},
        {"id": 22, "name": "工程师", "dept_id": 11},
    )
    add("OrgPositionHistory",
        {"id": 1, "position_id": 21, "name": "高级工程师"},
    )
    add("JobInformation",
        {"id": 21, "employee_id": 101, "post": "高级工程师", "start": "2020-03-01"},
        {"id": 22, "employee_id": 102, "post": "工程师", "start": "2021-06-15"},
    )
    add("JobInformationMaster",
        {"id": 21, "post": "高级工程师", "grade": "P6"},
        {"id": 22, "post": "工程师", "grade": "P5"},
    )
    add("JobInformationWorkRecord",
        {"id": 1, "employee_id": 101, "post": "高级工程师", "start": "2020-03-01"},
    )
    add("JobStep",
        {"id": 1, "name": "试用", "type": "probation"},
        {"id": 2, "name": "正式", "type": "regular"},
    )
    add("JobStepType",
        {"id": 1, "name": "管理序列"},
        {"id": 2, "name": "专业序列"},
    )
    add("JobGrade",
        {"id": 1, "name": "P6", "level": 6},
        {"id": 2, "name": "P5", "level": 5},
    )
    add("CommonBasicItem",
        {"id": 1, "category": "民族", "item": "汉族"},
        {"id": 2, "category": "民族", "item": "回族"},
        {"id": 3, "category": "学历", "item": "本科"},
    )
    add("CommonBasicItemCategory",
        {"id": 1, "name": "民族"},
        {"id": 2, "name": "学历"},
    )
    add("TrainDemand",
        {"id": 1, "dept_id": 11, "course": "Python进阶", "count": 5},
    )
    add("TrainPlan",
        {"id": 1, "name": "2026培训计划", "year": 2026},
    )
    add("TrainCost",
        {"id": 1, "plan_id": 1, "amount": 50000},
    )
    add("TrainSession",
        {"id": 1, "name": "Python训练营", "status": "open"},
    )
    add("TrainSessionStudent",
        {"id": 1, "session_id": 1, "employee_id": 101},
    )
    add("TrainOnlineCourse",
        {"id": 1, "name": "在线课程A", "hours": 8},
    )

    # ── 业务扩展模型（U_*，云函数高频）──
    add("U_DelayPayment",
        {"id": 1, "employee_id": 101, "month": "2026-07", "amount": 1200, "status": "pending"},
        {"id": 2, "employee_id": 102, "month": "2026-07", "amount": 800, "status": "pending"},
    )
    add("U_DelayPaymentDetails",
        {"id": 1, "delay_payment_id": 1, "item": "课时费", "amount": 1200},
        {"id": 2, "delay_payment_id": 2, "item": "补贴", "amount": 800},
    )
    add("U_DelayRecoup",
        {"id": 1, "employee_id": 101, "month": "2026-07", "amount": 500, "status": "pending"},
        {"id": 2, "employee_id": 102, "month": "2026-07", "amount": 300, "status": "pending"},
    )
    add("U_DelayRecoupDetails",
        {"id": 1, "delay_recoup_id": 1, "item": "扣回", "amount": 500},
    )
    add("U_DelayPayState",
        {"id": 1, "employee_id": 101, "state": "paid"},
    )
    add("U_comprehensive_points",
        {"id": 1, "employee_id": 101, "period": "2026", "points": 95},
        {"id": 2, "employee_id": 102, "period": "2026", "points": 88},
    )
    add("U_points_rules",
        {"id": 1, "name": "全勤", "points": 5},
        {"id": 2, "name": "获奖", "points": 10},
    )
    add("U_length_of_service",
        {"id": 1, "employee_id": 101, "years": 6},
        {"id": 2, "employee_id": 102, "years": 4},
    )
    add("U_annual_assessment",
        {"id": 1, "employee_id": 101, "year": 2025, "level": "A"},
        {"id": 2, "employee_id": 102, "year": 2025, "level": "B"},
    )
    add("U_edu_jf",
        {"id": 1, "employee_id": 101, "score": 80},
    )
    add("U_qualification",
        {"id": 1, "employee_id": 101, "name": "资格A"},
    )
    add("U_reward",
        {"id": 1, "employee_id": 101, "name": "奖励A", "amount": 2000},
    )
    add("U_punishment",
        {"id": 1, "employee_id": 103, "name": "处罚A"},
    )
    add("U_AttendData",
        {"id": 1, "employee_id": 101, "month": "2026-07", "days": 22},
    )
    add("U_DeptMapping",
        {"id": 1, "dept_id": 11, "external_code": "EXT_RD"},
    )
    add("U_Organizational_Comparison",
        {"id": 1, "dept_id": 11, "external_code": "EXT_RD"},
    )

    # ── 集成 / 第三方 ──
    add("CompanyThirdBind",
        {"id": 1, "company_id": 1, "type": "dingtalk", "bound": 1},
    )
    add("ThirdAuthorizerInfo",
        {"id": 1, "company_id": 1, "app": "wecom", "auth": 1},
    )
    add("WorkFlowInstance",
        {"id": 1, "type": "leave", "status": "running"},
    )
    add("HCMModelCheckInstance",
        {"id": 1, "model": "Employee", "status": "ok"},
    )
    add("SystemParams",
        {"id": 1, "key": "version", "value": "73.2.3.27"},
    )
    add("DynamicScriptList",
        {"id": 1, "name": "demo", "status": "enabled"},
    )
    return d


MOCK_DB = _seed()


def model_fields(model):
    """由样本行推导字段列表（与真实 hcm.model.meta 结构对齐：[{key,label,type}]）。"""
    rows = MOCK_DB.get(model, [])
    keys = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    return [{"key": k, "label": k, "type": _guess_type(k)} for k in keys]


def _guess_type(k):
    if k in ("id",):
        return "int"
    if any(s in k for s in ("date", "time", "_at")):
        return "date"
    if any(s in k for s in ("amount", "points", "years", "score", "level", "days", "count", "hours")):
        return "number"
    if k in ("status", "enabled", "bound", "auth", "paid"):
        return "bool"
    return "string"


class MockCustomerUtil(object):
    """离线 Mock：按 model 返回样本，写接口只登记不落库。

    list 支持 filter_dict / page_size / page_index（与真实接口对齐）；
    DEBUG_ID 时收窄到单条，便于单步调试。
    """

    def __init__(self, debug_id=None, company_id=1, employee_id=101):
        self.writes = []
        self.debug_id = debug_id or DEBUG_ID
        self.company_id = int(company_id or 1)
        self.employee_id = employee_id
        self.token = None

    def call_open_api(self, api_name, param=None):
        param = dict(param or {})
        model = param.get("model")

        if api_name == "hcm.model.meta":
            return {"fields": model_fields(model)}

        if api_name == "hcm.model.list":
            rows = list(MOCK_DB.get(model, []))
            flt = param.get("filter_dict") or {}
            for k, v in flt.items():
                rows = [r for r in rows if r.get(k) == v]
            if self.debug_id:
                rows = [r for r in rows if str(r.get("id")) == str(self.debug_id)]
            page_size = int(param.get("page_size") or 2000)
            page_index = int(param.get("page_index") or 1)
            start = (page_index - 1) * page_size
            page = rows[start:start + page_size]
            return {"list": page, "total": len(rows), "count": len(rows)}

        if api_name == "hcm.model.get":
            for r in MOCK_DB.get(model, []):
                if str(r.get("id")) == str(param.get("id_")):
                    return dict(r)
            return None

        if api_name in ("hcm.model.edit", "hcm.model.create", "hcm.model.delete",
                        "hcm.model.create.batch", "hcm.model.edit.batch",
                        "hcm.model.remove", "hcm.model.remove.batch"):
            self.writes.append((api_name, param))
            return {"success": True, "id": param.get("id_") or param.get("info", {}).get("id")}

        # 其它接口（hcm.model.action.* 等）默认成功
        return {"success": True}

    def get_current_context(self):
        """复刻云函数沙箱中 CustomerUtil.get_current_context()：返回当前操作上下文。

        生产语义：返回含 company / employee / user / token 的上下文对象。
        离线 Mock：company.id 取配置的 company_id，employee.id / user.id 复用占位 employee_id，
        token 取 self.token（默认 None）。调用方（sdsy_customer_sso 读 .company.id、call_llm 读
        .user.id、call_llm_service 读 .token）均不报错，与真实结构对齐。
        """
        from types import SimpleNamespace
        return SimpleNamespace(
            company=SimpleNamespace(id=self.company_id),
            employee=SimpleNamespace(id=self.employee_id),
            user=SimpleNamespace(id=self.employee_id),
            token=self.token,
        )

    def call_llm(self, name, params=None, alter_message=None, use_cache=False, is_reasoning=False):
        """调试沙箱不支持真实 LLM 调用（需云端 LLMAgent 服务）。返回与云端一致的 dict 契约。"""
        return {"success": False, "message": "调试沙箱不支持 call_llm（需真实 LLM 服务）"}

    def call_llm_service(self, system_prompt, query, model_name=None, model_type=None, temperature=0.5):
        return {"success": False, "message": "调试沙箱不支持 call_llm_service"}

    def safeEval(self, script, param):
        # 真实实现用 SafeEnv 执行脚本（不常用）；调试沙箱做 no-op，原样返回 param。
        logging.info("[cfdebug] safeEval 在调试沙箱为 no-op（未执行脚本）")
        return param
