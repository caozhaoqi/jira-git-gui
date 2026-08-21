// 后端 API 数据模型（对应 FastAPI / Pydantic）。
// Pilot 仅有仓库/文件树/预览，后续迁移 K8s / CF / Diff / Commits 时持续扩充。

export type ConnectMode = 'pat' | 'cookie';

export interface StatusResp {
  mode?: ConnectMode;
  repo_id?: string;
  repo_name?: string;
  branch?: string;
  jira_url?: string;
  username?: string;
  cookie_set?: boolean;
  cookie_source?: string;
  pat_set?: boolean;
  qps?: number;
}

// 后端存在两套 /api/status 命名：server.py 返回 camelCase(patSet/cookieSet)，
// api/server.py 返回 snake_case(pat_set/cookie_set/qps/cookie_source)。
// 此处统一归一化为 snake_case，前端只认一种。
export function normalizeStatus(raw: any): StatusResp {
  if (!raw) return {};
  return {
    mode: raw.mode,
    repo_id: raw.repo_id,
    repo_name: raw.repo_name,
    branch: raw.branch,
    jira_url: raw.jira_url,
    username: raw.username,
    cookie_set: raw.cookie_set ?? raw.cookieSet ?? false,
    cookie_source: raw.cookie_source,
    pat_set: raw.pat_set ?? raw.patSet ?? false,
    qps: typeof raw.qps === 'number' ? raw.qps : undefined,
  };
}

export interface Repo {
  repo_id: string;
  display_name?: string;
  default_branch?: string;
  name?: string;
}

export interface ReposResp {
  repos?: Repo[];
  error?: string;
}

export type TreeEntryType = 'dir' | 'file';

export interface TreeEntry {
  name: string;
  path: string;
  type: TreeEntryType;
  size?: number;
  mtime?: number;
}

export interface TreeResp {
  entries?: TreeEntry[];
  error?: string;
}

export interface FileResp {
  content?: string;
  error?: string;
}

export interface SearchHit {
  path: string;
  line?: number;
  snippet?: string;
}

export interface SearchResp {
  results?: SearchHit[];
  total?: number;
  truncated?: boolean;
  error?: string;
}

export interface ConnectResp {
  cookieOk?: boolean;
  patTest?: { ok: boolean; msg?: string };
  repoDefaults?: { displayName?: string };
  note?: string;
  cookieSaved?: boolean;
  cookieWarning?: string;
  error?: string;
}

export interface RepoSelectBody {
  repo_id: string;
  repo_name?: string;
  branch?: string;
}

export interface ConnectBody {
  jira_url?: string;
  username?: string;
  mode: ConnectMode;
  pat?: string;
  cookie?: string;
  repo_id?: string;
  repo_name?: string;
  branch?: string;
}

// ===== Commits =====
export interface CommitFile {
  path: string;
  change_type?: string; // ADDED/MODIFIED/DELETED/RENAMED/COPIED 或 A/M/D/R/C
  lines_added?: number;
  lines_removed?: number;
}
export interface Commit {
  commit_id: string;
  author: string;
  date: string;
  message: string;
  branch?: string;
  repository_name?: string;
  files?: CommitFile[];
}
export interface CommitsResp {
  commits?: Commit[];
  error?: string;
}
export interface FileAtCommitResp {
  content?: string;
  error?: string;
}

// ===== Diff =====
export type DiffStatus =
  | 'modified'
  | 'whitespace_only'
  | 'local_only'
  | 'remote_only'
  | 'same';
export interface DiffEntry {
  path: string;
  status: DiffStatus;
  lines_added?: number;
  lines_removed?: number;
}
export interface DiffSummary {
  total?: number;
  modified?: number;
  local_only?: number;
  remote_only?: number;
  same?: number;
  whitespace_only?: number;
}
export interface DiffScanReq {
  local_dir: string;
  repo_name: string;
  ignore_line_endings?: boolean;
}
export interface DiffScanResp {
  entries?: DiffEntry[];
  summary?: DiffSummary;
  error?: string;
}
export interface DiffFileReq {
  local_dir: string;
  path: string;
}
export interface DiffFileResp {
  diff?: string;
  local_content?: string;
  remote_content?: string;
  normalized_same?: boolean;
  error?: string;
}
export interface DiffMergeReq {
  local_dir: string;
  path: string;
  status?: string;
}
export interface DiffMergeResp {
  ok?: boolean;
  error?: string;
}
export interface DiffMergeBatchReq {
  local_dir: string;
  path: string;
  status?: string;
}
export interface DiffMergeBatchItem {
  local_dir: string;
  path: string;
  status?: string;
}
export interface DiffMergeBatchResp {
  results?: { path: string; ok: boolean; error?: string }[];
  error?: string;
}

// ===== K8s =====
export interface K8sEnv {
  name: string;
  label?: string;
  kubeconfig?: string;
  is_current?: boolean;
  context?: string;
  namespace?: string;
  intranet_hosts?: string[];
}
export interface K8sEnvsResp {
  environments?: K8sEnv[];
  current?: string;
  error?: string;
}
export interface K8sEnvSaveReq {
  name: string;
  label?: string;
  kubeconfig?: string;
  context?: string;
  namespace?: string;
  intranet_hosts?: string[];
}
export interface K8sSnapshotReq {
  namespace?: string;
  selector?: string;
  pod_filter?: string;
  tail?: number;
  restart_threshold?: number;
  all_logs?: boolean;
  include_previous?: boolean;
  out_dir?: string;
  kubeconfig?: string;
  env?: string;
  log_level?: string;
}
export type K8sSev = 'ok' | 'med' | 'high';
export interface K8sRecord {
  name: string;
  phase?: string;
  ready?: number;
  total?: number;
  restarts?: number;
  problems?: [string, string][];
  reason?: string;
  node?: string;
  host_ip?: string;
  pod_ip?: string;
  age?: string;
  sev?: K8sSev;
}
export interface K8sSummary {
  total?: number;
  ok?: number;
  med?: number;
  high?: number;
  logs?: number;
}
export interface K8sPod {
  name: string;
  phase?: string;
  restarts?: number;
  namespace?: string;
}
export interface K8sPodsResp {
  ok?: boolean;
  pods?: K8sPod[];
  error?: string;
}
export interface K8sYamlReq {
  env: string;
  kind: string;
  name: string;
  namespace?: string;
  action: 'get' | 'apply';
  content?: string;
  clean?: boolean;
}
export interface K8sYamlResp {
  ok?: boolean;
  yaml?: string;
  stdout?: string;
  stderr?: string;
  error?: string;
}
export interface K8sNetworkReq {
  env: string;
  extra_hosts?: string[];
}
export interface K8sNetCheck {
  name: string;
  status?: 'ok' | 'fail' | 'warn';
  detail?: string;
}
export interface K8sNetIntranet {
  target: string;
  ok: boolean;
  ms?: number;
}
export interface K8sNetworkResp {
  ok?: boolean;
  summary?: string;
  checks?: K8sNetCheck[];
  intranet?: K8sNetIntranet[];
  cluster_ok?: boolean;
  error?: string;
}
export interface K8sEvent {
  type?: string;
  reason?: string;
  object_kind?: string;
  object_name?: string;
  object_ns?: string;
  source?: string;
  count?: number;
  message?: string;
  last_seen?: string;
}
export interface K8sEventsResp {
  ok?: boolean;
  events?: K8sEvent[];
  total?: number;
  warning?: number;
  error?: string;
}
export interface K8sTopRow {
  name: string;
  namespace?: string;
  cpu?: string;
  memory?: string;
  cpu_pct?: string;
  memory_pct?: string;
}
export interface K8sTopResp {
  ok?: boolean;
  scope?: 'pods' | 'nodes';
  rows?: K8sTopRow[];
  error?: string;
}
export interface K8sDescribeResp {
  ok?: boolean;
  text?: string;
  events?: K8sEvent[];
  error?: string;
}
export interface K8sFileEntry {
  name: string;
  type: 'dir' | 'file';
  size?: number;
  modtime?: string;
}
export interface K8sFileListReq {
  env: string;
  pod: string;
  container?: string;
  namespace?: string;
  path: string;
}
export interface K8sFileListResp {
  ok?: boolean;
  entries?: K8sFileEntry[];
  error?: string;
}
export interface K8sFileReadReq {
  env: string;
  pod: string;
  container?: string;
  namespace?: string;
  path: string;
  max_bytes?: number;
}
export interface K8sFileReadResp {
  ok?: boolean;
  content?: string;
  is_binary?: boolean;
  truncated?: boolean;
  error?: string;
}
export interface K8sFileWriteReq {
  env: string;
  pod: string;
  container?: string;
  namespace?: string;
  path: string;
  content: string;
}
export interface K8sFileWriteResp {
  ok?: boolean;
  error?: string;
}
export interface K8sFileSearchReq {
  env: string;
  pod: string;
  container?: string;
  namespace?: string;
  q: string;
  path: string;
}
export interface K8sFileSearchHit {
  path: string;
  line?: number;
  snippet?: string;
}
export interface K8sFileSearchResp {
  ok?: boolean;
  results?: K8sFileSearchHit[];
  total?: number;
  error?: string;
}
export interface K8sLogResp {
  // 纯文本日志（text/plain）
  error?: string;
}

// ===== CF 云函数日志 =====
export interface CfAccount {
  name: string;
  server_url?: string;
  username?: string;
  password?: string;
}
export interface CfAccountsResp {
  accounts?: CfAccount[];
}
export interface CfLoginReq {
  server_url: string;
  mobile: string;
  password: string;
  proxy?: string;
  image_code?: string;
  image_code_index?: string;
  captcha_id?: string;
}
export interface CfLoginResp {
  token?: string;
  ok?: boolean;
  message?: string;
  need_img_valid?: boolean;
  error?: string;
}
export interface CfCaptchaResp {
  captcha_id?: string;
  image_code_index?: string;
  image?: string; // data URL
  error?: string;
}
export interface CfLogsRow {
  id?: string | number;
  _id?: string | number;
  create_time?: string;
  createTime?: string;
  created_at?: string;
  content?: any;
  message?: any;
  data?: any;
  log_type?: string;
  logType?: string;
}
export interface CfLogsReq {
  server_url: string;
  token: string;
  log_type?: string;
  page_index: number;
  page_size: number;
  proxy?: string;
}
export interface CfLogsResp {
  data?: any;
  result?: any;
  list?: CfLogsRow[];
  total?: number;
  method?: string;
  error?: string;
}
export interface CfExportReq {
  server_url: string;
  log_type?: string;
  auth_method?: string;
  page_index: number;
  page_size: number;
  total: number;
  rows: CfLogsRow[];
  raw: any;
}
export interface CfExportResp {
  path?: string;
  count?: number;
  error?: string;
}
export interface CfClipboardSaveReq {
  text: string;
}
export interface CfClipboardSaveResp {
  path?: string;
  size?: number;
  error?: string;
}

// ===== SSE 事件载荷 =====

export interface SSELog {
  msg: string;
  level?: string;
}

export interface SSEProgress {
  done: number;
  total: number;
  pct?: number;
}

export interface SSECloneDone {
  ok?: boolean;
  msg?: string;
  path?: string;
}

export interface SSEDownloadDone {
  ok_count?: number;
  skipped?: number;
  fail_count?: number;
  dest?: string;
  fails?: { path: string; reason: string }[];
  total_fails?: number;
}

export interface SSENetworkWarning {
  message?: string;
  level?: string;
}

// K8s 快照相关 SSE
export interface SSEK8sLog {
  msg: string;
  ts?: string;
}
export interface SSEK8sProgress {
  done: number;
  total: number;
  pct: number;
  name?: string;
}
export interface SSEK8sDone {
  summary?: K8sSummary;
  records?: K8sRecord[];
  out_dir?: string;
  report?: string;
}
export interface SSEK8sError {
  message: string;
}
export interface SSEK8sFinished {
  running: boolean;
}

// Diff 扫描 / 合并相关 SSE
export interface SSEDiffStage {
  message: string;
  pct?: number;
}
export interface SSEDiffProgress {
  done: number;
  total: number;
  message?: string;
  pct?: number;
}
export interface SSEDiffDone {
  summary?: DiffSummary;
}
export interface SSEDiffError {
  message: string;
}
export interface SSEMergeStart {
  total: number;
}
export interface SSEMergeProgress {
  done: number;
  total: number;
  ok?: boolean;
  path?: string;
  pct?: number;
  error?: string;
}
export interface SSEMergeDone {
  ok_count?: number;
  fail_count?: number;
}

export type SSEEventMap = {
  log: SSELog;
  progress: SSEProgress;
  clone_done: SSECloneDone;
  download_done: SSEDownloadDone;
  network_warning: SSENetworkWarning;
  ping: Record<string, never>;
  k8s_log: SSEK8sLog;
  k8s_progress: SSEK8sProgress;
  k8s_done: SSEK8sDone;
  k8s_error: SSEK8sError;
  k8s_finished: SSEK8sFinished;
  scan_stage: SSEDiffStage;
  scan_progress: SSEDiffProgress;
  scan_done: SSEDiffDone;
  scan_error: SSEDiffError;
  merge_start: SSEMergeStart;
  merge_progress: SSEMergeProgress;
  merge_done: SSEMergeDone;
};
