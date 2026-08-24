import { encryptParam, signParam, decryptParam } from './crypto';
import type {
  HcmObjectListResult,
  HcmModelMeta,
} from './types';

export class HcmApiError extends Error {
  status?: number;
  errcode?: number;
  constructor(message: string, status?: number, errcode?: number) {
    super(message);
    this.name = 'HcmApiError';
    this.status = status;
    this.errcode = errcode;
  }
}

/** HCM 平台常见业务错误码（与前端提示文案对应） */
export const HCM_ERR = {
  /** 登录态失效 / token 过期 —— 需重新获取 token */
  LOGIN_EXPIRED: 51006,
} as const;

export interface HcmConfig {
  // baseUrl 支持两种形态：
  //  1) 同源代理基址，如 '/hcm-api'（由 8787 转发到 HCM，浏览器零跨域）
  //  2) 绝对网关地址，如 'http://73.2.3.27'（页面直连 HCM 网关，依赖网关已开 CORS）
  baseUrl: string;
  // cookie 的 token 值（Flask 签名会话）。
  //  - 同源代理模式：经请求头 X-HCM-Token 传入（避免落入 URL/日志）
  //  - 直连网关模式：经请求头 Cookie: token=xxx 传入（网关 CORS 已允许 Cookie 头）
  token: string;
}

/** 是否直连模式（baseUrl 为绝对 http(s) 地址，而非同源相对路径） */
export function isDirectBaseUrl(baseUrl: string): boolean {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(baseUrl.trim());
}

// 自定义请求头名：token 走头而非 URL query，规避日志/历史留存凭证
export const HCM_TOKEN_HEADER = 'X-HCM-Token';

/**
 * apiName 形如 'hcm.paas.object.list'，model 可选（拼到 ?model= 上）。
 * 两种调用形态：
 *  - 同源代理（baseUrl 以 '/' 开头，如 '/hcm-api'）：浏览器只跟 127.0.0.1:8787 通信，零跨域；
 *    token 经 X-HCM-Token 请求头传递。
 *  - 直连网关（baseUrl 为绝对地址，如 'http://73.2.3.27'）：浏览器直接 fetch 网关，
 *    依赖网关已开 CORS（已验证允许 Origin=localhost:5173 + 自定义头 Token/Cookie）；
 *    token 经自定义请求头 `Token: <token>` 传递（浏览器禁止 JS 设置 Cookie 请求头，
 *    故直连用 Token 头；服务端 Python 脚本可用 Cookie 头，二者网关均识别）。
 */
export async function hcmCall<T = any>(
  cfg: HcmConfig,
  apiName: string,
  params: Record<string, any>,
  model?: string
): Promise<T> {
  const direct = isDirectBaseUrl(cfg.baseUrl);
  // 直连：绝对地址 + '/api/<api_name>'；代理：相对路径 '/hcm-api/<api_name>'
  const path = direct ? `/api/${apiName}` : `/hcm-api/${apiName}`;
  let url = `${cfg.baseUrl.replace(/\/+$/, '')}${path}`;
  const qs = new URLSearchParams();
  if (model) qs.set('model', model);
  const qstr = qs.toString();
  if (qstr) url += `?${qstr}`;

  const hp = encryptParam(params);
  const body = {
    hcm_transfer_strategy: 'ha',
    hcm_param: hp,
    // 注意：signParam 内部已完成 `hcm_param + 'hcm' + 'cloud'` 的拼接与 sm3，
    // 这里只传 hp，切勿再手动拼 'hcm'+'cloud'，否则会出现双重拼接导致签名错误、
    // 平台返回「规则校验不通过 / 数据完整性被破坏」(errcode 40016)。
    s3h: signParam(hp),
  };

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (cfg.token.trim()) {
    if (direct) {
      // 直连网关：token 经自定义请求头 `Token` 传入。
      // 注意：浏览器跨域 fetch **禁止 JS 设置 `Cookie` 请求头**（forbidden header name），
      // 因此不能用 Cookie 头；网关 CORS 已显式允许 `Token` 头，且网关按 `Token` 头取登录态，
      // 故直连走 `Token` 头（服务端 Python 脚本可用 Cookie 头，二者网关都认）。
      headers['Token'] = cfg.token.trim();
    } else {
      // 同源代理：token 走 X-HCM-Token 头（不落入 URL/日志）
      headers[HCM_TOKEN_HEADER] = cfg.token.trim();
    }
  }

  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
  } catch (e: any) {
    throw new HcmApiError(`网络连接失败：${e?.message || e}`, undefined);
  }

  const text = await res.text().catch(() => '');
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { _raw: text };
  }

  if (!res.ok) {
    // 解析平台业务错误码，便于前端区分「token 失效」与「其它错误」。
    // 注意：HCM 即便返回 4xx/5xx 也可能带 errcode（如 51006 登录失效、
    // 40016 规则校验不通过/数据完整性被破坏），这里统一提取。
    const errcode = data?.errcode ?? data?.code ?? undefined;
    let detail =
      data?.errmsg || data?.message || data?.description || text || `HTTP ${res.status}`;
    // 针对登录失效给出可操作的明确提示，避免用户误以为是代码/算法问题。
    if (errcode === HCM_ERR.LOGIN_EXPIRED) {
      detail = `${detail}（请重新获取 HCM 登录 Token 后填入）`;
    }
    throw new HcmApiError(detail, res.status, typeof errcode === 'number' ? errcode : undefined);
  }

  // 响应解析：本网关部分接口直接返回明文 {"result": {...}}（不带 hcm_param 加密字段），
  // 部分接口返回加密 {hcm_transfer_strategy, hcm_param}。两种都兼容：
  //  - 含 result 键（明文）→ 直接取 result
  //  - 含 hcm_param（加密）→ 解密后取 .result
  if (data && typeof data === 'object') {
    if ('result' in data && data.result !== undefined) {
      return data.result as T;
    }
    if (data.hcm_transfer_strategy && data.hcm_param) {
      const inner = decryptParam(data.hcm_param, data.hcm_transfer_strategy);
      return (inner?.result ?? inner) as T;
    }
  }
  return data as T;
}

// ---- 业务封装 ----

/**
 * 对象列表（通用 object 列表视图）。
 * 搜索字段 query_str 同时写入「顶层」与「extra_property.filter_params」两层，
 * 兼容不同网关版本对搜索位置的约定（实测某些版本只认 filter_params.query_str，
 * 老版本只认顶层 query_str），避免因位置错配导致「搜索不生效」。
 */
export function hcmObjectList(
  cfg: HcmConfig,
  opts: {
    baseObjectStr?: string;
    key?: string;
    pageIndex?: number;
    pageSize?: number;
    queryStr?: string;
    advanceFilterDict?: Record<string, any>;
  } = {}
): Promise<HcmObjectListResult> {
  const q = opts.queryStr?.trim() || null;
  const filterParams: Record<string, any> = {
    filter_str: q, // 搜索关键字放 filter_str（实测该字段才真正生效，query_str 无效）
    page_index: opts.pageIndex ?? 1,
    page_size: opts.pageSize ?? 20,
    advance_filter_dict: opts.advanceFilterDict || {},
    show_fields_key: ['class_', 'model_category', 'update_time'],
    base_object_str: opts.baseObjectStr ?? 'hcm.paas.object',
    key: opts.key ?? 'main.setting.hcm_model',
    query_str: q, // 兼容老版本网关约定
  };
  const params: Record<string, any> = {
    model: null,
    filter_str: q, // 顶层搜索字段（实测生效）
    filter_dict: {},
    query_str: q, // 兼容老版本
    page_index: opts.pageIndex ?? 1,
    page_size: opts.pageSize ?? 20,
    extra_property: { sorts: [], filter_params: filterParams, only_list: false },
    biz_type: 'list',
  };
  return hcmCall<HcmObjectListResult>(cfg, 'hcm.paas.object.list', params);
}

/** 单对象的完整字段 / JSON 元数据（hcm.model.meta） */
export function hcmModelMeta(cfg: HcmConfig, model: string): Promise<HcmModelMeta> {
  return hcmCall<HcmModelMeta>(cfg, 'hcm.model.meta', { model }, model);
}

/**
 * 查询单对象的「某一类 JSON」元数据（list / info / view 等展示维度）。
 * HCM 的模型元信息是一棵包含 property / fields / childrens / action / rules 等节点的 JSON 树，
 * 不同维度（list 列表视图、info 详情视图、view 表单视图）其实都是同一份 hcm.model.meta 的
 * 不同节点子集。这里统一走 hcm.model.meta，由前端按维度裁剪展示。
 * 若未来网关提供独立 api（如 hcm.model.view），可在此扩展 apiName。
 */
export async function hcmModelMetaByKind(
  cfg: HcmConfig,
  model: string,
  kind: 'list' | 'info' | 'view' | 'all' = 'all'
): Promise<HcmModelMeta> {
  // 多数网关按 model + meta_key 返回不同维度；统一传 meta_key，缺省 all。
  const params: Record<string, any> = { model, meta_key: kind === 'all' ? '' : kind };
  return hcmCall<HcmModelMeta>(cfg, 'hcm.model.meta', params, model);
}

/** 兼容旧名的别名 */
export const hcmModelInfo = hcmModelMeta;

export interface HcmEnv {
  key: string;
  name: string;
  server_url: string;
  source: string;
  has_preset_token: boolean;
}

/** 获取可选服务器环境列表（后端从 cf_accounts / hcm_whitelist 汇总，脱敏仅暴露 name+url） */
export async function hcmEnvs(): Promise<HcmEnv[]> {
  const res = await fetch('/api/hcm/envs', { method: 'GET' });
  if (!res.ok) throw new HcmApiError(`获取环境列表失败: HTTP ${res.status}`, res.status);
  const data = await res.json().catch(() => ({ envs: [] }));
  return (data?.envs || []) as HcmEnv[];
}
