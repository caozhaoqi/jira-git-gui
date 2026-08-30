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

/* ==================== 基础设施错误码 & 错误自动分类 ==================== */
// 与上面的业务错误码是两套体系：
//  - HCM_ERR_DICT       → HCM 平台业务错误码（正数，4~6 位），如 17003 / 51006
//  - HCM_INFRA_ERR_DICT → 底层依赖（数据库等）错误码，**可为负数**，如达梦 -70028
//
// 为什么要拆：达梦报错形如 [CODE:-70028]，是负数。旧版 extractErrcode 正则
// 只认正数 \d{4,6}，这类错误会被整个漏掉 → 面板显示「无有效信息」，排查卡死。

/**
 * 基础设施 / 中间件错误码词典（支持负数）。
 *
 * ⚠️ 收录原则：只收**有实证**的码（来自真实报错截图/日志），不臆测含义。
 *    未收录的 DB 码会退化为「模式识别」→ kind='db'，仍给出通用排查路径。
 */
export const HCM_INFRA_ERR_DICT: Record<number, HcmErrInfo> = {
  // 注意：负数 key 必须加引号 —— 裸写 -70028: 是语法错误（`-` 不能作标识符起始字符）。
  // JS 对象 key 本质上都是字符串，用 Record<number, ...> 索引时会自动转换，不影响查询。
  '-70028': {
    name: 'DM_SOCKET_CONNECT_FAILURE',
    meaning: '达梦数据库 SOCKET 建连失败 —— 客户端连不上数据库实例。' +
             '属基础设施故障，不是业务对象/字段问题，查对象字段定位不到根因。',
    fix: '1) 数据库服务器确认实例存活：ps -ef | grep dmserver；' +
         '2) 应用服务器测端口连通（达梦默认 5236）：telnet <DB_IP> <DB_PORT>；' +
         '3) 核对 HCM 数据源配置（host/port/实例名/账号）是否被改动；' +
         '4) 查连接数是否打满：SELECT COUNT(*) FROM V$SESSIONS；' +
         '5) 检查防火墙/安全组是否放通该端口。',
    level: 'error',
  },
};

/** 提取基础设施错误码（仅取负数），如达梦 [CODE:-70028] / CODE:-70028 */
export function extractInfraErrcode(text: string): number | undefined {
  if (!text) return undefined;
  const patterns = [
    /\[CODE:\s*(-?\d+)\]/i,
    /\bCODE\s*[:=]\s*(-?\d+)/i,
  ];
  for (const re of patterns) {
    const m = re.exec(text);
    if (m) {
      const code = Number(m[1]);
      // 只取负数：正数属业务 errcode，交给 extractErrcode，避免两套词典打架
      if (Number.isFinite(code) && code < 0) return code;
    }
  }
  return undefined;
}

export function lookupInfraErrcode(code: number | undefined): HcmErrInfo | undefined {
  if (!code) return undefined;
  return HCM_INFRA_ERR_DICT[code];
}

/** 错误类别：决定排查路径，而不是只盯业务错误码 */
export type ErrKind = 'token' | 'db' | 'network' | 'business' | 'unknown';

/**
 * 按报错文本判定错误类别（顺序重要，越具体越靠前）：
 *   token → db → network → business → unknown
 *
 * db 排在 network 之前：达梦 SOCKET 失败本质也是网络，但 dmPython/DatabaseError
 * 明确指向数据库层，按 db 给建议更有针对性。
 */
export function classifyErrorKind(text: string): ErrKind {
  if (!text) return 'unknown';
  const s = text;

  // ① 登录态 / Token
  if (/51006|LOGIN_EXPIRED|登录.*(失效|过期)|token.*(过期|失效|invalid|expire)|未登录|not\s*login|会话.*(超时|失效)/i.test(s)) {
    return 'token';
  }
  // ② 数据库
  if (/dmPython|dm8|达梦|DM\s*Database|\[CODE:-\d+\]|ORA-\d+|MySQLdb|psycopg2|pymysql|sqlite3|DatabaseError|OperationalError|Create SOCKET|数据库.*(连接|异常|失败)/i.test(s)) {
    return 'db';
  }
  // ③ 网络 / 网关
  if (/ETIMEDOUT|ECONNREFUSED|ECONNRESET|EHOSTUNREACH|ENOTFOUND|timed?\s*out|timeout|\b(502|503|504)\b|Bad Gateway|Service Unavailable|网络.*(异常|错误|不通)|连接被拒绝/i.test(s)) {
    return 'network';
  }
  // ④ 业务（有 [定位] 埋点或业务错误码）
  if (/\[定位\]/.test(s) || extractErrcode(s) !== undefined) return 'business';

  return 'unknown';
}

/** 各类别的排查路径，供 UI 直接渲染 */
export const ERR_KIND_GUIDE: Record<ErrKind, { label: string; steps: string[] }> = {
  token: {
    label: '登录态 / Token',
    steps: [
      'HCM token 默认仅 2 小时有效（hcm_cloud.context_expire_seconds）',
      '重新登录 HCM，取新 Cookie token 填入本工具',
      '或用面板「一键重新登录」自动刷新',
      '注意：A 服务器签发的 token 不能打到 B 服务器',
    ],
  },
  db: {
    label: '数据库',
    steps: [
      '确认数据库实例存活（达梦：ps -ef | grep dmserver）',
      '应用服务器测端口连通（达梦默认 5236）',
      '核对 HCM 数据源配置（host/port/实例名/账号）',
      '查连接数是否打满（达梦：V$SESSIONS）',
      '检查防火墙 / 安全组是否放通',
    ],
  },
  network: {
    label: '网络 / 网关',
    steps: [
      '确认网关地址可达（顶部网关下拉可切换环境）',
      '502/503/504 多为网关或反向代理层故障',
      '确认 DNS 解析正确、代理未拦截内网段',
      '必要时切换其它环境交叉验证',
    ],
  },
  business: {
    label: '业务 / 对象字段',
    steps: [
      '看 [定位] 里的 model + id 定位到具体对象',
      '用「查询字段当前值」比对报错时的 value',
      '字段为空/类型不符时按 field 修正数据或云函数取值方式',
      '配合错误码词典确认具体业务码含义',
    ],
  },
  unknown: {
    label: '未分类',
    steps: [
      '确认是否含 [定位] 标记（云函数需用 locate_snippet 埋点）',
      '粘贴完整 traceback，用「一键给 AI 排查」生成上下文',
      '用错误号/时间戳反查服务端日志',
    ],
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
