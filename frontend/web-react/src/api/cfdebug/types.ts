// 云函数调试控制台：前端数据模型（对应 api/cfdebug/* 后端接口与 DAP 协议）。
// 与项目约定一致：同源请求走 location.origin；类型仅描述后端真实返回结构。

export type CfModel = 'A' | 'B';
export type CfEnv = 'mock' | 'test' | 'custom';
/** 调试模式：本地（mock，不连服务器）/ 远程（连 cf_accounts 里的真实服务器跑）。 */
export type CfMode = 'local' | 'remote';
export type CfDebugStatus =
  | 'idle'
  | 'connecting'
  | 'running'
  | 'paused'
  | 'finished'
  | 'error';

/** 扫描到的单个云函数（AST 提取）。 */
export interface CfFunction {
  name: string;
  path: string;
  model: CfModel;
  entry: string;
  params: string[];
  doc: string;
  size: number;
  mtime: number;
}

export interface CfFunctionList {
  root: string;
  functions: CfFunction[];
}

export interface CfSource {
  ok: boolean;
  file: string;
  lines: string[];
  error?: string;
}

export interface CfEnvServer {
  server: string;
  token: string;
}

/** GET /api/cf-debug/environments 的返回，也是 POST 的 patch 结构。 */
export interface CfEnvConfig {
  functions_root: string;
  current_env: CfEnv;
  envs: {
    test: CfEnvServer;
    custom: CfEnvServer;
  };
}

/** cf_accounts 服务账号（远程模式选服务器用；前端只拿 name/server_url，不含密码）。 */
export interface CfAccount {
  index: number;
  name: string;
  server_url: string;
  type: string;
  has_password: boolean;
}

export interface CfAccountList {
  ok: boolean;
  items: CfAccount[];
}

export interface CfRunReq {
  file: string;
  root?: string;
  kwargs: string;
  env?: CfEnv;
  server?: string;
  token?: string;
  /** 远程模式：传 cf_accounts 的 server_url 或 name，后端据此登录取 token。 */
  server_account?: string;
  debug_id?: string;
  db_url?: string;
  allow_ddl?: boolean;
  db_save?: boolean;
  write_real?: boolean;
  entry?: string;
  company_id?: number;
}

export interface CfRunResp {
  ok: boolean;
  session_id: string;
  dap_host: string;
  dap_port: number;
  ws_url: string;
  file: string;
  env: CfEnv;
  error?: string;
}

export interface CfSessionInfo {
  session_id: string;
  file: string;
  env: CfEnv;
  dap_port: number;
  started_at: number;
  alive: boolean;
}

export interface CfSessionList {
  sessions: CfSessionInfo[];
}

// ===== DAP（Debug Adapter Protocol）前端侧类型 =====

export interface DapSource {
  path?: string;
  name?: string;
}

export interface DapStackFrame {
  id: number;
  name: string;
  source?: DapSource;
  line: number;
  column: number;
  presentationHint?: string;
}

export interface DapScope {
  name: string;
  variablesReference: number;
  expensive?: boolean;
  presentationHint?: string;
}

export interface DapVariable {
  name: string;
  value: string;
  type?: string;
  variablesReference: number;
  evaluateName?: string;
  presentationHint?: { kind?: string; attributes?: string[] };
}

export interface DapThread {
  id: number;
  name: string;
}

export interface DapStoppedBody {
  reason: string;
  threadId?: number;
  description?: string;
  text?: string;
  allThreadsStopped?: boolean;
}

export interface DapOutputBody {
  category?: string;
  output?: string;
  variablesReference?: number;
}

/** 控制台日志（运行 / 错误 / debug）行。 */
export interface CfLogLine {
  session_id: string;
  level: string;
  msg: string;
}

// ===== SSE 事件载荷扩展（并入 src/api/types.ts 的 SSEEventMap） =====
export interface SSECFDebugLog {
  session_id: string;
  level: string;
  msg: string;
}
export interface SSECFDebugDone {
  session_id: string;
  returncode: number;
}

// ===== 配置同步（cf_accounts 导入/导出/复制） =====
export interface CfSyncImportReq {
  accounts: Array<Record<string, unknown>>;
  mode: 'merge' | 'replace';
}

export interface CfSyncExport {
  ok: boolean;
  accounts: Array<Record<string, unknown>>;
  count: number;
}
