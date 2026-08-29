/**
 * HCM 平台常见业务错误码词典（errcode → 含义 + 排查建议）。
 *
 * 重要区分（源码核实）：
 *  - errcode：业务错误码（如 17003 / 51006 / 40016），即本词典的 key。
 *  - error_code：handlers.py:477 生成的**毫秒时间戳**（10+ 位），只是服务端日志索引号，
 *    不是业务错误码，不要拿它来查词典。
 *
 * 来源：hcm-core/errors.py、core/service/handlers.py:482/524、
 *       cloud_functions/system_error_popup.py（errcode→弹窗级别映射）、api/hcm/client.ts。
 */

export interface HcmErrInfo {
  /** 错误码名称/简称 */
  name: string;
  /** 含义 */
  meaning: string;
  /** 排查/修复建议 */
  fix: string;
  /** 弹窗级别（仅系统级错误码有） */
  level?: 'error' | 'warning';
}

export const HCM_ERR_DICT: Record<number, HcmErrInfo> = {
  11002: {
    name: 'AUTH_PASSWORD_ERROR',
    meaning: '账号或密码错误',
    fix: '检查登录账号/密码；若走预设 Token，请确认 cf_accounts 配置里的凭据未过期。',
  },
  11006: {
    name: 'AUTH_VALID_CODE_ERROR',
    meaning: '验证码错误',
    fix: '重新获取验证码；若服务端要求验证码，考虑改用已登录的 Token 直连。',
  },
  17003: {
    name: 'EXECUTE_OPEN_API_ERROR',
    meaning: '执行 OpenAPI（含云函数）内部异常，框架兜底捕获。' +
             '⚠️ 实测最常见的真实原因是 Token 失效，而非业务代码异常',
    fix: '【先按 Token 排查，命中率最高】HCM token 默认有效期只有 2 小时' +
         '（hcm_cloud.context_expire_seconds，见 hcm-core apps/idm/auth_util.py:AuthUtil.get_expire）；' +
         '过期后服务端解析不出会话，接口内部抛异常即被兜底成 17003（而非标准的 51006）。' +
         '且 token 不可跨服务器复用——A 服务器签发的有效 token 打到 B 服务器同样 17003。' +
         '步骤：①看本面板「Token 健康度」的年龄，>2h 直接判定过期；②点「一键重新登录」换当前网关签发的新 token；' +
         '③若仍失败，再按 [定位] 格式让云函数抛错、或把 hcm_cloud.hide_error_msg 配成 False，' +
         '用 error_code（毫秒时间戳）+ log_index 去服务端日志反查 traceback。',
  },
  18003: {
    name: 'NO_PERMISSION',
    meaning: '无权限访问',
    fix: '检查当前账号对该模型/字段的权限；确认 Token 所属账号具备对应角色。',
  },
  40016: {
    name: 'RULE_CHECK_FAILED',
    meaning: '规则校验不通过 / 数据完整性被破坏（多为签名问题）',
    fix: '多数是 Token 无效或过期导致签名校验失败。重新获取 Token；' +
         '切勿在 s3h 签名里重复拼接 hcm+cloud（client.ts 已处理）。',
  },
  400014: {
    name: 'FIELD_DATA_IS_INVALID',
    meaning: '字段数据不合法（云函数读取/校验对象字段失败）',
    fix: '本码为「错误定位」方案建议采用的字段类错误码。配合 [定位] 文本可直接看到' +
         '是哪个对象(model+id)、哪个字段(field)、当前值(value) 出了问题。',
  },
  51001: { name: 'SYS_ERROR_1', meaning: '系统级错误（触发前端 error 弹窗）', fix: '按弹窗提示处理；查看详情可拿到 error_code 反查服务端日志。', level: 'error' },
  51002: { name: 'SYS_ERROR_2', meaning: '系统级错误（触发前端 error 弹窗）', fix: '同上。', level: 'error' },
  51003: { name: 'SYS_ERROR_3', meaning: '系统级错误（触发前端 error 弹窗）', fix: '同上。', level: 'error' },
  51005: { name: 'SYS_WARNING', meaning: '系统级警告（触发前端 warning 弹窗）', fix: '按弹窗提示处理，一般不影响主流程。', level: 'warning' },
  51006: {
    name: 'LOGIN_EXPIRED',
    meaning: '登录态失效 / Token 过期',
    fix: '重新登录 HCM，取新的 Cookie token 填入工具（或更新预设 Token）后重试。',
  },
};

/**
 * 从报错文本里提取业务错误码（errcode）。
 * 只认 4~6 位数字（errcode 量级），避免把 10+ 位的 error_code 毫秒时间戳误判成错误码。
 */
export function extractErrcode(text: string): number | undefined {
  if (!text) return undefined;
  const patterns = [
    /errcode["']?\s*[:=]\s*"?(\d{4,6})/i,
    /错误码\s*[:：]?\s*(\d{4,6})/,
    /错误号\s*[:：]?\s*(\d{4,6})/,
    /code["']?\s*[:=]\s*"?(\d{4,6})/i,
  ];
  for (const re of patterns) {
    const m = re.exec(text);
    if (m) return Number(m[1]);
  }
  // 兜底：文本里出现且命中词典的 4~6 位数字
  const dictKeys = Object.keys(HCM_ERR_DICT);
  const m = /\b(\d{4,6})\b/.exec(text);
  if (m && dictKeys.includes(m[1])) return Number(m[1]);
  return undefined;
}

/** 查词典；未命中返回 undefined */
export function lookupErrcode(code: number | undefined): HcmErrInfo | undefined {
  if (!code) return undefined;
  return HCM_ERR_DICT[code];
}

/* -------------------------------------------------------------------------- */
/*  Token 有效期诊断                                                            */
/* -------------------------------------------------------------------------- */

/**
 * HCM token 默认有效期（小时）。
 * 源码：hcm-core/apps/idm/auth_util.py:66 AuthUtil.get_expire()
 *   → environment.get_conf("hcm_cloud", "context_expire_seconds")
 *   → 未配置时默认 60 * 60 * 2 = 7200 秒 = 2 小时
 */
export const HCM_TOKEN_TTL_HOURS = 2;

/**
 * 解析 token 的签发时间（Unix 秒）。
 * HCM token 是 Tornado secure cookie：2|1:0|10:<unix_ts>|5:token|56:<b64>|<sig>
 * 注意：不能用缓存里的 ts 字段判断过期——那是「最后一次登录尝试时间」，
 * 登录失败时也会被更新但 token 保持旧值（cf_login.py:110）。
 */
export function parseTokenIssueTime(token: string): number | null {
  if (!token) return null;
  const m = /^"?2\|1:0\|10:(\d+)\|5:token\|56:/.exec(token.trim());
  if (!m) return null;
  const ts = Number(m[1]);
  return Number.isFinite(ts) && ts > 0 ? ts : null;
}

/** token 年龄（小时）；无法解析返回 null */
export function tokenAgeHours(token: string): number | null {
  const ts = parseTokenIssueTime(token);
  if (ts === null) return null;
  return (Date.now() / 1000 - ts) / 3600;
}

/** 疑似已过期：能解析出签发时间且年龄超过默认 TTL */
export function isTokenLikelyExpired(token: string): boolean {
  const age = tokenAgeHours(token);
  return age !== null && age > HCM_TOKEN_TTL_HOURS;
}
