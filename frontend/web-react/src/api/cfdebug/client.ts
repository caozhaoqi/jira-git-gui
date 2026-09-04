// 云函数调试控制台：后端 HTTP 接口封装。
// 直接复用项目统一的 apiGet / apiPost（同源、错误分类、JSON 解析一致）。
import { apiGet, apiPost } from '../client';
import type {
  CfFunctionList,
  CfSource,
  CfEnvConfig,
  CfRunReq,
  CfRunResp,
  CfSessionList,
  CfAccountList,
  CfSyncExport,
  CfSyncImportReq,
  DynLogListResp,
  DynLogDeleteResp,
  DynLogRecord,
  CfEnv,
} from './types';

export const cfdebug = {
  /** 列出云函数目录下的全部云函数（AST 扫描）。root 留空则用后端默认/已配目录。 */
  listFunctions(root?: string): Promise<CfFunctionList> {
    const q = root ? `?root=${encodeURIComponent(root)}` : '';
    return apiGet<CfFunctionList>(`/api/cf-debug/functions${q}`);
  },

  /** 读取单个云函数源码（供源码窗格 + 断点）。 */
  getSource(file: string): Promise<CfSource> {
    return apiGet<CfSource>(`/api/cf-debug/source?file=${encodeURIComponent(file)}`);
  },

  /** 当前调试环境配置（functions_root + 各环境 server/token）。 */
  getEnv(): Promise<CfEnvConfig> {
    return apiGet<CfEnvConfig>('/api/cf-debug/environments');
  },

  /** 保存调试环境配置（持久化到 config/cfdebug_env.local.json）。 */
  saveEnv(patch: Partial<CfEnvConfig>): Promise<CfEnvConfig> {
    return apiPost<CfEnvConfig>('/api/cf-debug/environment', patch);
  },

  /** 启动一次调试会话，返回 session_id 与 DAP ws_url。 */
  run(req: CfRunReq): Promise<CfRunResp> {
    return apiPost<CfRunResp>('/api/cf-debug/run', req);
  },

  /** 停止指定会话。 */
  stop(sessionId: string): Promise<{ ok: boolean; error?: string }> {
    return apiPost<{ ok: boolean; error?: string }>('/api/cf-debug/stop', {
      session_id: sessionId,
    });
  },

  /** 列出当前活动会话。 */
  listSessions(): Promise<CfSessionList> {
    return apiGet<CfSessionList>('/api/cf-debug/sessions');
  },

  /** 远程模式：列出 cf_accounts 服务账号（供选服务器）。 */
  listAccounts(): Promise<CfAccountList> {
    return apiGet<CfAccountList>('/api/cf-debug/accounts');
  },

  /** 同步配置：导出全部账号（含密码明文，仅本地/受信任环境用）。 */
  exportAccounts(): Promise<CfSyncExport> {
    return apiGet<CfSyncExport>('/api/services/cloud-functions/export');
  },

  /** 同步配置：导入/合并账号（merge 去重追加 / replace 整体替换）。 */
  importAccounts(accounts: CfSyncImportReq['accounts'], mode: CfSyncImportReq['mode'] = 'merge') {
    return apiPost<{ ok: boolean; count?: number; error?: string }>(
      '/api/services/cloud-functions/import',
      { accounts, mode } as CfSyncImportReq,
    );
  },

  /** 同步配置：按序号复制一条账号（生成「副本」）。 */
  copyAccount(index: number) {
    return apiPost<{ ok: boolean; count?: number; new_index?: number; error?: string }>(
      '/api/services/cloud-functions/copy',
      { index },
    );
  },

  /**
   * 日志管理：列出服务器 dynamic_log 记录。
   * - env=test/custom 时未传 server/token 则取后端环境配置
   * - log_type: 按函数名过滤
   * - search: 在 content 字段做包含匹配（前端关键字搜索）
   */
  listDynamicLogs(opts: {
    env?: CfEnv;
    server?: string;
    token?: string;
    company_id?: number;
    log_type?: string;
    search?: string;
    page?: number;
    page_size?: number;
  } = {}): Promise<DynLogListResp> {
    const q: string[] = [];
    if (opts.env) q.push(`env=${encodeURIComponent(opts.env)}`);
    if (opts.server) q.push(`server=${encodeURIComponent(opts.server)}`);
    if (opts.token) q.push(`token=${encodeURIComponent(opts.token)}`);
    if (opts.company_id) q.push(`company_id=${opts.company_id}`);
    if (opts.log_type) q.push(`log_type=${encodeURIComponent(opts.log_type)}`);
    if (opts.search) q.push(`search=${encodeURIComponent(opts.search)}`);
    if (opts.page) q.push(`page=${opts.page}`);
    if (opts.page_size) q.push(`page_size=${opts.page_size}`);
    const qs = q.length ? `?${q.join('&')}` : '';
    return apiGet<DynLogListResp>(`/api/cf-debug/dynamic-logs${qs}`);
  },

  /** 日志管理：批量删除 dynamic_log（按 id_）。 */
  deleteDynamicLogs(opts: {
    ids: Array<string | number>;
    env?: CfEnv;
    server?: string;
    token?: string;
    company_id?: number;
  }): Promise<DynLogDeleteResp> {
    return apiPost<DynLogDeleteResp>('/api/cf-debug/dynamic-logs/delete', opts);
  },

  /** 日志管理：解析当前选中行用到的字段（前端渲染辅助）。 */
  formatDynLog(r: DynLogRecord): string {
    const t = r.log_type || '?';
    const c = r.content || '';
    const ts = r.create_date || r.create_time || '';
    return `${ts} [${t}] ${c}`.trim();
  },
};
