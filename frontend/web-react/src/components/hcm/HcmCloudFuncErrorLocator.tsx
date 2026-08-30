import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import { useT } from '../../i18n';
import { HcmApiError } from '../../api/hcm/client';
import { hcmDirect } from '../../api/hcm/direct';
import {
  extractErrcode, lookupErrcode,
  extractInfraErrcode, lookupInfraErrcode,
  classifyErrorKind, ERR_KIND_GUIDE, type ErrKind,
  HCM_TOKEN_TTL_HOURS, tokenAgeHours, isTokenLikelyExpired,
} from '../../api/hcm/errDict';

const LS_TOKEN = 'hcm.token';
const LS_HISTORY = 'hcm.cfErrHistory';
const LS_GW = 'hcm.cfErrGw';
const HISTORY_MAX = 20;
const BATCH_MAX = 50;

interface LocInfo {
  model?: string;
  objectId?: string;
  field?: string;
  value?: string;
  stage?: string;
  errorCode?: string;   // 毫秒时间戳（服务端日志索引）
  errcode?: number;     // 业务错误码（查词典用）
  message?: string;
  raw?: string;         // 该定位块原始文本（多块分诊用）
  // P3-1: 面向「无 [定位] 埋点」的通用错误（如达梦 -70028），
  // 保证这类错误也能解析出有效信息，而不是被判为「无有效信息」。
  exception?: string;   // 异常类型，如 dmPython.DatabaseError
  dbCode?: number;      // 基础设施错误码（负数），如 -70028
  trace?: string;       // 关键栈帧（取最接近抛出点的一帧），如 xxx.py:264 in _dm
  kind?: ErrKind;       // 错误类别，决定排查路径
}

interface HistoryItem {
  ts: number;
  text: string;
}

interface DiagnosisContext {
  summary?: {
    root_cause?: string;
    confidence?: number;
    status?: string;
    reasons?: string[];
    checks_to_run?: string[];
  };
  parsed?: Record<string, any>;
  errDict?: { name?: string; meaning?: string; fix?: string } | null;
  wiki?: {
    snippets?: { file?: string; section?: string; content?: string; address?: { absolute_path?: string; read_hint?: string } }[];
    matched_patterns?: { title?: string; hit_keywords?: string[] }[];
  };
  tokenHealth?: { age_hours?: number | null; ttl_hours?: number; expired?: boolean | null; hint?: string };
  similarCases?: { file?: string; title?: string; score?: number }[];
  sourceEvidence?: {
    terms?: string[];
    hits?: { file?: string; score?: number; address?: { absolute_path?: string; read_hint?: string }; hits?: { line?: number; matched?: string[]; excerpt?: string; address?: { absolute_path?: string; line?: number; read_hint?: string } }[] }[];
  };
  logMatches?: { matches?: { file?: string; id?: string | number; create_time?: string; stage?: string; score?: number; message?: string }[] };
  referenceError?: {
    matched?: { name?: string; status_code?: number; errmsg?: string }[];
    coverage_percent?: number;
    verified_coverage_percent?: number;
    inferred_coverage_percent?: number;
    verified_errdict_count?: number;
    inferred_errdict_count?: number;
    source_error_code_count?: number;
  };
  aiPrompt?: string;
}

// 敏感字段脱敏（身份证/手机号等），与云函数端 locate_snippet 的 _mask 保持一致
function mask(v: unknown): string {
  if (v === null || v === undefined) return '(空)';
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  if (s === '') return '(空)';
  if (s.length > 12) return s.slice(0, 6) + '****' + s.slice(-4);
  return s;
}

// 解析云函数抛出的 [定位] model=.. id=.. field=.. value=.. stage=.. || 原因
// 也兼容纯 error_code（毫秒时间戳）或散落的 model=/id=/field= token。
function parseLoc(text: string): LocInfo {
  const info: LocInfo = {};
  const m = (k: string) => {
    const r = new RegExp(`${k}=([^\\s]+)`).exec(text);
    return r ? r[1] : undefined;
  };
  info.model = m('model');
  info.objectId = m('id');
  info.field = m('field');
  info.value = m('value');
  info.stage = m('stage');
  const msgMatch = /(?:\|\||错误原因|原因)\s*:?\s*(.*)$/s.exec(text);
  if (msgMatch) info.message = msgMatch[1].trim();
  // 纯数字（10 位以上）→ 视为 error_code（毫秒时间戳，服务端日志索引）
  const digits = text.trim().match(/^\d{10,}$/);
  if (digits) info.errorCode = digits[0];
  else {
    const codeMatch = /(?:error_code|错误号)\D*(\d{10,})/i.exec(text);
    if (codeMatch) info.errorCode = codeMatch[1];
  }
  // 业务错误码（4~6 位），用于查词典
  info.errcode = extractErrcode(text);

  // ---- P3-1: 通用错误特征（无 [定位] 埋点也能提取出有效信息）----
  // 异常类型：如 dmPython.DatabaseError / ValueError
  const excMatch = /([A-Za-z_][\w.]*(?:Error|Exception|Warning))\s*:/.exec(text);
  if (excMatch) info.exception = excMatch[1];

  // 基础设施错误码（达梦 [CODE:-70028]，负数）；正数归业务 errcode，互不干扰
  info.dbCode = extractInfraErrcode(text);

  // 关键栈帧：取最后一条 File "...", line N, in xxx（最接近抛出点）。
  // 用 exec 循环而非 matchAll，避免依赖 ES2020 lib。
  const frameRe = /File\s+"([^"]+)",\s*line\s+(\d+)(?:,\s*in\s+(\S+))?/g;
  let lastFrame: RegExpExecArray | null = null;
  let fm: RegExpExecArray | null;
  while ((fm = frameRe.exec(text)) !== null) lastFrame = fm;
  if (lastFrame) {
    info.trace = `${lastFrame[1]}:${lastFrame[2]}${lastFrame[3] ? ` in ${lastFrame[3]}` : ''}`;
  }

  // 错误类别：决定排查路径（token / db / network / business / unknown）
  info.kind = classifyErrorKind(text);

  return info;
}

function hasAnyInfo(i: LocInfo | null): boolean {
  if (!i) return false;
  // P3-1: 补上 dbCode / exception / trace —— 否则达梦这类「无 [定位]」的
  // 错误会被判成无有效信息，面板直接卡在「请先输入」。
  return Boolean(
    i.model || i.objectId || i.field || i.errorCode || i.errcode ||
    i.dbCode || i.exception || i.trace,
  );
}

// P2-1: 把整段日志拆成多个 [定位] 块，逐块独立解析，供多错误分诊。
// 单条日志（无 [定位] 或仅一个）整体作为一块，保持旧行为。
function parseBlocks(raw: string): LocInfo[] {
  if (!raw.trim()) return [];
  const segs = raw.split(/\[定位\]/);
  const blocks: LocInfo[] = [];
  segs.forEach((seg, idx) => {
    const segText = (idx === 0 ? seg : `[定位]${seg}`).trim();
    if (!segText) return;
    const info = parseLoc(segText);
    if (hasAnyInfo(info)) {
      info.raw = segText;
      blocks.push(info);
    }
  });
  return blocks;
}

// P2-2: 从粘贴日志中识别已知网关域名（server_url），自动预选对应环境。
function matchGatewayByText(text: string, gateways: { key: string; server_url: string; name?: string }[]): { key: string; server_url: string; name?: string } | null {
  if (!text) return null;
  const hostOf = (u: string) => {
    try { return new URL(u).host; } catch { return u.replace(/^https?:\/\//, '').split('/')[0]; }
  };
  for (const g of gateways) {
    if (!g.server_url) continue;
    const host = hostOf(g.server_url);
    if (host && text.includes(host)) return g;
  }
  return null;
}

// 合并多个定位块给 AI：逐块列出已解析信息与词典含义，便于一次性排查一批错误。
function buildAiMultiContext(blocks: LocInfo[], gwName: string, gwUrl: string): string {
  const L: string[] = [];
  L.push(`# HCM 云函数批量错误排查（共 ${blocks.length} 条）`);
  L.push('');
  L.push(`- 网关: ${gwName}（${gwUrl || '默认 proxy_target'}）`);
  L.push(`- 时间: ${new Date().toLocaleString('zh-CN')}`);
  L.push('');
  blocks.forEach((p, i) => {
    L.push(`## 第 ${i + 1} 条`);
    L.push(`- 模型(model): ${p.model ?? '未知'}`);
    L.push(`- 对象ID: ${p.objectId ?? '未知'}`);
    L.push(`- 字段(field): ${p.field ?? '未知'}`);
    L.push(`- 字段值(脱敏): ${mask(p.value)}`);
    if (p.stage) L.push(`- 阶段(stage): ${p.stage}`);
    if (p.errcode) {
      const d = lookupErrcode(p.errcode);
      L.push(`- 业务错误码: ${p.errcode}${d ? ` (${d.name})` : ''}`);
      if (d?.meaning) L.push(`  - 含义: ${d.meaning}`);
      if (d?.fix) L.push(`  - 建议: ${d.fix}`);
    }
    if (p.errorCode) L.push(`- 错误号: ${p.errorCode}`);
    if (p.message) L.push(`- 原因: ${p.message}`);
    L.push('');
  });
  L.push('## 请回答');
  L.push('1. 以上多条错误是否存在共同根因（如同一网关 token 过期、同一字段缺失）？');
  L.push('2. 分别给出最小修复方案或修复 SQL。');
  return L.join('\n');
}

// 一键给 AI：把错误原文 + 解析出的对象/字段/值 + 词典建议 + 当前数据 + 网关，汇总成一段
// 对 AI 友好的 Markdown 排查上下文，可直接复制粘贴给任意 AI 助手分析。
function buildAiContext(opts: {
  raw: string;
  p: LocInfo | null;
  cur: { value: string; present: boolean } | null;
  gwName: string;
  gwUrl: string;
  errName?: string;
  errMeaning?: string;
  errFix?: string;
  diagnosis?: DiagnosisContext | null;
}): string {
  const { raw, p, cur, gwName, gwUrl, diagnosis } = opts;
  const L: string[] = [];
  L.push('# HCM 云函数执行错误排查请求');
  L.push('');
  L.push('请根据以下信息帮我定位根因并给出修复建议（含可能出错的云函数代码位置、数据修复 SQL/配置建议）。');
  L.push('');
  L.push('## 错误信息');
  L.push(`- 时间: ${new Date().toLocaleString('zh-CN')}`);
  L.push(`- 网关: ${gwName}（${gwUrl || '默认 proxy_target'}）`);
  L.push(`- 错误原文: ${raw.trim() || '(未粘贴原文)'}`);
  if (diagnosis?.summary) {
    L.push('');
    L.push('## 机器初步判断（需要结合证据验证）');
    L.push(`- 候选根因: ${diagnosis.summary.root_cause || 'UNKNOWN'}`);
    L.push(`- 置信度: ${diagnosis.summary.confidence ?? 0}`);
    L.push(`- 状态: ${diagnosis.summary.status || 'need_verification'}`);
    (diagnosis.summary.reasons || []).slice(0, 6).forEach((r) => L.push(`- 依据: ${r}`));
    (diagnosis.summary.checks_to_run || []).slice(0, 4).forEach((r) => L.push(`- 待验证: ${r}`));
  }
  L.push('');
  if (p) {
    L.push('## 已解析的定位信息');
    L.push(`- 模型(model): ${p.model ?? '未知'}`);
    L.push(`- 对象ID: ${p.objectId ?? '未知'}`);
    L.push(`- 字段(field): ${p.field ?? '未知'}`);
    L.push(`- 字段值(报错时,已脱敏): ${mask(p.value)}`);
    if (p.stage) L.push(`- 阶段(stage): ${p.stage}`);
    if (p.errcode) {
      L.push(`- 业务错误码: ${p.errcode}${opts.errName ? ` (${opts.errName})` : ''}`);
      if (opts.errMeaning) L.push(`  - 含义: ${opts.errMeaning}`);
      if (opts.errFix) L.push(`  - 建议: ${opts.errFix}`);
    }
    if (p.errorCode) L.push(`- 错误号(error_code, 服务端日志索引): ${p.errorCode}`);
    if (p.message) L.push(`- 原因: ${p.message}`);
  }
  if (cur) {
    L.push('');
    L.push('## 对象当前数据（已查询对比）');
    L.push(`- 当前字段值: ${cur.value}（${cur.present ? '有值' : '空/不存在'}）`);
  }
  if (diagnosis?.wiki?.snippets?.length) {
    L.push('');
    L.push('## 相关 Wiki 规范片段（后端按错误类型路由）');
    diagnosis.wiki.snippets.forEach((s) => {
      L.push(`### 来源: ${s.file || '未知文档'}${s.section ? ` / ${s.section}` : ''}`);
      if (s.address?.absolute_path) L.push(`- 文档地址: ${s.address.absolute_path}`);
      if (s.content) L.push(s.content);
      L.push('');
    });
  }
  if (diagnosis?.tokenHealth?.hint) {
    L.push('');
    L.push('## Token 健康度（后端复核）');
    const th = diagnosis.tokenHealth;
    if (th.age_hours !== null && th.age_hours !== undefined) {
      L.push(`- 年龄: ${th.age_hours} 小时；默认 TTL: ${th.ttl_hours ?? HCM_TOKEN_TTL_HOURS} 小时；疑似过期: ${th.expired ? '是' : '否'}`);
    }
    L.push(`- 结论: ${th.hint}`);
  }
  if (diagnosis?.similarCases?.length) {
    L.push('');
    L.push('## 历史相似案例');
    diagnosis.similarCases.forEach((c) => L.push(`- ${c.title || c.file || '案例'}${c.score ? `（匹配度 ${c.score}）` : ''}`));
  }
  if (diagnosis?.referenceError?.matched?.length) {
    L.push('');
    L.push('## 参考源码错误定义');
    diagnosis.referenceError.matched.forEach((e) => L.push(`- ${e.name || '未知'}（HTTP ${e.status_code ?? '?'}）：${e.errmsg || ''}`));
    if (diagnosis.referenceError.coverage_percent !== undefined) {
      const v = diagnosis.referenceError.verified_coverage_percent;
      const t = diagnosis.referenceError.inferred_coverage_percent;
      const src = diagnosis.referenceError.source_error_code_count ?? '?';
      if (v !== undefined && t !== undefined) {
        L.push(`- 当前本地 errdict 覆盖率：已校验 ${v}%（errdict.json），含推断 ${t}%（源码唯一错误码 ${src} 个）`);
      } else {
        L.push(`- 当前本地 errdict 覆盖率：${diagnosis.referenceError.coverage_percent}%`);
      }
    }
  }
  if (diagnosis?.sourceEvidence?.hits?.length) {
    L.push('');
    L.push('## 参考云函数源码证据');
    diagnosis.sourceEvidence.hits.slice(0, 5).forEach((item) => {
      L.push(`### ${item.file || '未知文件'}`);
      if (item.address?.absolute_path) L.push(`- 源码地址: ${item.address.absolute_path}`);
      (item.hits || []).slice(0, 2).forEach((hit) => {
        L.push(`- L${hit.line ?? '?'} 命中 ${(hit.matched || []).join(', ')}`);
        if (hit.address?.absolute_path) L.push(`- 行地址: ${hit.address.absolute_path}:${hit.address.line ?? hit.line ?? '?'}`);
        if (hit.excerpt) L.push(hit.excerpt);
      });
    });
  }
  if (diagnosis?.logMatches?.matches?.length) {
    L.push('');
    L.push('## 本地日志关联');
    diagnosis.logMatches.matches.slice(0, 8).forEach((m) => {
      L.push(`- ${m.file || '日志'} id=${m.id ?? '?'} time=${m.create_time || '?'} stage=${m.stage || '?'}：${m.message || ''}`);
    });
  }
  L.push('');
  L.push('## 请回答');
  L.push('1. 该错误最可能的原因是什么（字段缺失/类型错误/数据异常/网关不可达）？');
  L.push('2. 若要快速修复，应在哪个云函数/哪段逻辑处理？给出最小改动或修复 SQL。');
  L.push('3. 若为数据问题，如何批量修复这些对象记录？');
  return L.join('\n');
}

export function HcmCloudFuncErrorLocator() {
  const { t } = useT();
  const urlParams = useMemo(() => new URLSearchParams(window.location.search), []);

  const [token] = useState(() => localStorage.getItem(LS_TOKEN) || '');
  const [text, setText] = useState(() => urlParams.get('hcm-loc') || '');
  const [manual, setManual] = useState({
    model: urlParams.get('hcm-loc-model') || '',
    objectId: urlParams.get('hcm-loc-id') || '',
    field: urlParams.get('hcm-loc-field') || '',
  });

  const [parsed, setParsed] = useState<LocInfo | null>(null);
  const [blocks, setBlocks] = useState<LocInfo[]>([]);   // P2-1: 多 [定位] 块分诊
  const [gwAutoName, setGwAutoName] = useState('');       // P2-2: 自动识别网关提示
  const [current, setCurrent] = useState<{ value: string; present: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [errKind, setErrKind] = useState<'' | 'gw' | 'biz' | 'other'>('');
  const [copied, setCopied] = useState('');
  // 一键给 AI：生成的排查上下文（Markdown）
  const [ai, setAi] = useState('');
  const [aiBusy, setAiBusy] = useState(false);

  // 反馈闭环反哺：根据 feedback JSONL 把根因反哺进 errdict / 路由索引
  const [learnBusy, setLearnBusy] = useState(false);
  const [learnMsg, setLearnMsg] = useState('');
  const [learnDetail, setLearnDetail] = useState<{
    applied?: boolean; sample_count?: number;
    applied_changes?: { errdict_new?: string[]; errdict_updated?: string[]; route_new?: string[] };
    proposal_path?: string | null;
  } | null>(null);

  // 案例库 & 反馈闭环（写入侧）：把本次定位存成案例，并支持把诊断准确率反馈写回闭环
  const [caseFile, setCaseFile] = useState('');
  const [caseBusy, setCaseBusy] = useState(false);
  const [caseMsg, setCaseMsg] = useState('');
  const [fbResult, setFbResult] = useState('correct');
  const [fbRootCause, setFbRootCause] = useState('');
  const [fbNotes, setFbNotes] = useState('');
  const [fbBusy, setFbBusy] = useState(false);
  const [fbMsg, setFbMsg] = useState('');

  // ③ 最近错误历史本
  const [history, setHistory] = useState<HistoryItem[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(LS_HISTORY) || '[]') as HistoryItem[];
    } catch {
      return [];
    }
  });

  // ④ 批量字段体检
  const [batch, setBatch] = useState({ model: '', fields: '', ids: '' });
  const [batchRows, setBatchRows] = useState<
    { id: string; field: string; value: string; present: boolean }[]
  >([]);
  const [batchRunning, setBatchRunning] = useState(false);

  // ⑥ Jira 工单
  const [jira, setJira] = useState({ projectKey: '', issuetype: '' });
  const [jiraBusy, setJiraBusy] = useState(false);
  const [jiraResult, setJiraResult] = useState<{ key: string; url: string } | null>(null);
  const [reloginBusy, setReloginBusy] = useState(false);
  const [reloginMsg, setReloginMsg] = useState('');

  // P2-4 改造工具前端化：上传云函数 .py → 审计/预览 diff/应用下载
  const [retroContent, setRetroContent] = useState('');
  const [retroName, setRetroName] = useState('');
  const [retroMode, setRetroMode] = useState<'audit' | 'diff' | 'apply'>('audit');
  const [retroRedact, setRetroRedact] = useState(false);
  const [retroBusy, setRetroBusy] = useState(false);
  const [retroReport, setRetroReport] = useState<any>(null);
  const [retroDiff, setRetroDiff] = useState('');
  const [retroNewContent, setRetroNewContent] = useState('');
  const [retroChanged, setRetroChanged] = useState(false);
  const [retroMsg, setRetroMsg] = useState('');

  const onRetroFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    setRetroName(f.name);
    const reader = new FileReader();
    reader.onload = () => setRetroContent(String(reader.result || ''));
    reader.readAsText(f);
  };

  const runRetrofit = async () => {
    if (!retroContent.trim()) { setRetroMsg(t('hcm.cfErrRetrofitNeedFile')); return; }
    setRetroBusy(true);
    setRetroMsg('');
    setRetroReport(null);
    setRetroDiff('');
    setRetroNewContent('');
    try {
      const res = await fetch('/api/cf/retrofit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: retroContent, mode: retroMode, redact_sensitive: retroRedact }),
      });
      const d = await res.json().catch(() => ({} as any));
      if (!res.ok || d?.ok === false) throw new Error(d?.detail || `HTTP ${res.status}`);
      setRetroChanged(Boolean(d.changed));
      if (retroMode === 'audit') {
        setRetroReport(d.report || null);
      } else if (retroMode === 'diff') {
        setRetroDiff(d.diff || '');
      } else {
        setRetroNewContent(d.new_content || '');
        if (!d.changed) setRetroMsg(t('hcm.cfErrRetrofitNoChange'));
      }
      if (d.changed && retroMode !== 'apply') setRetroMsg(`${t('hcm.cfErrRetrofitChanged')}: ${retroMode}`);
    } catch (e: any) {
      setRetroMsg(String(e?.message || e));
    } finally {
      setRetroBusy(false);
    }
  };

  const downloadRetro = () => {
    if (!retroNewContent) return;
    const blob = new Blob([retroNewContent], { type: 'text/x-python;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = retroName ? `${retroName.replace(/\.py$/, '')}.patched.py` : 'cloud_function.patched.py';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // 网关选择：默认用后端 proxy_target；当该网关 502 不可达时，可临时切到其它可达的 HCM 部署。
  interface Gateway { key: string; name: string; server_url: string; source: string; has_preset_token: boolean }
  const [gateways, setGateways] = useState<Gateway[]>([]);
  // P3-3: 后端连通性。连不上时直接在页面给出启动命令，
  // 避免「界面所有功能都失效」却没有任何提示、难以定位。
  const [backendDown, setBackendDown] = useState(false);
  const [gwKey, setGwKey] = useState(() => localStorage.getItem(LS_GW) || 'hcm_proxy');
  // 用 ref 镜像 gwKey，使自动选网关的 effect 不依赖 gwKey，避免手动切换后被日志域名再次覆盖
  const gwKeyRef = useRef(gwKey);
  useEffect(() => { gwKeyRef.current = gwKey; }, [gwKey]);
  const [gwTokenOverride, setGwTokenOverride] = useState('');
  const selectedGw = useMemo(() => gateways.find((g) => g.key === gwKey), [gateways, gwKey]);
  const targetArg = selectedGw?.server_url || ''; // 空 → 后端用默认 proxy_target
  const effectiveToken = gwTokenOverride.trim() || token; // 非预设网关需自带 token

  useEffect(() => {
    fetch('/api/hcm/envs')
      .then((r) => r.json())
      .then((d) => {
        if (Array.isArray(d?.envs)) setGateways(d.envs);
        setBackendDown(false);
      })
      .catch(() => {
        // 连不上后端：置位后在顶部显示启动命令，而不是静默回退到默认网关
        setBackendDown(true);
      });
  }, []);

  // 网关选择持久化：刷新后仍保留上次选的网关（与 HcmObjectBrowser 的 hcm.selectedEnv 一致）
  useEffect(() => {
    try { localStorage.setItem(LS_GW, gwKey); } catch { /* 忽略 */ }
  }, [gwKey]);

  // 把 HcmApiError 分级为可读提示（502=网关不可达 / 504=超时 / 业务 errcode=词典）
  function classifyError(e: any): { kind: 'gw' | 'biz' | 'other'; title: string; hint: string } {
    if (e instanceof HcmApiError) {
      const msg = e.message || '';
      if (e.status === 502) return { kind: 'gw', title: t('hcm.cfErrGateway502Title'), hint: t('hcm.cfErrGateway502Hint') };
      if (e.status === 504) return { kind: 'gw', title: t('hcm.cfErrGateway504Title'), hint: t('hcm.cfErrGateway504Hint') };
      const ec = extractErrcode(msg);
      if (ec) {
        const d = lookupErrcode(ec);
        if (d) return { kind: 'biz', title: `业务错误 ${ec}`, hint: `${d.meaning}　建议：${d.fix}` };
      }
      return { kind: 'biz', title: `HTTP ${e.status}`, hint: msg };
    }
    return { kind: 'other', title: t('hcm.cfErrReqFailTitle'), hint: String(e?.message || e) };
  }

  const fmt = (i: LocInfo) =>
    `[定位] model=${i.model ?? '?'} id=${i.objectId ?? '?'} field=${i.field ?? '?'} ` +
    `value=${i.value ?? '?'} stage=${i.stage ?? '?'} || ${i.message ?? ''}`;

  const pushHistory = (raw: string) => {
    if (!raw.trim()) return;
    setHistory((prev) => {
      const next = [{ ts: Date.now(), text: raw.trim() }, ...prev.filter((h) => h.text !== raw.trim())];
      const capped = next.slice(0, HISTORY_MAX);
      try {
        localStorage.setItem(LS_HISTORY, JSON.stringify(capped));
      } catch {
        /* 存储不可用时静默 */
      }
      return capped;
    });
  };

  // ⑤ 粘贴即解析：输入变化（含粘贴）时自动尝试定位，无需点按钮。
  // P2-1 多块分诊 + P2-2 粘贴即自动选网关，都在这里统一处理。
  // 注意：自动选网关用 gwKeyRef 比较，不把 gwKey 列入依赖，否则手动切换网关会被日志域名再次覆盖。
  useEffect(() => {
    const bs = parseBlocks(text);
    setBlocks(bs);
    // P2-2: 日志里含已知网关域名时，自动预选对应环境（仅在匹配到且与当前不同才切）
    const gw = matchGatewayByText(text, gateways);
    if (gw && gw.key !== gwKeyRef.current) {
      setGwKey(gw.key);
      setGwAutoName(gw.name ?? '');
    }
    if (bs.length === 1) {
      setParsed(bs[0]);
      setCurrent(null);
    }
    // 多块时交由分诊列表交互选择，避免覆盖用户正在看的那条
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, gateways]);

  const locate = () => {
    const fromText = parseLoc(text);
    const merged: LocInfo = {
      ...fromText,
      model: fromText.model || manual.model || undefined,
      objectId: fromText.objectId || manual.objectId || undefined,
      field: fromText.field || manual.field || undefined,
    };
    if (!hasAnyInfo(merged)) {
      setError(t('hcm.cfErrNoInput'));
      setParsed(null);
      return;
    }
    setError('');
    setParsed(merged);
    setCurrent(null);
    pushHistory(text || fmt(merged));
  };

  // P2-1: 分诊列表点击某条 → 载入主解析区（定位/查看/查询/给AI 都基于它）
  const selectBlock = (b: LocInfo) => {
    setParsed(b);
    setCurrent(null);
    setError('');
  };

  // P2-1: 合并所有定位块生成给 AI 的批量排查上下文，并逐条存案例
  const genAiMulti = async () => {
    if (blocks.length <= 1) return;
    setAiBusy(true);
    setError('');
    try {
      setAi(buildAiMultiContext(blocks, selectedGw?.name || t('hcm.cfErrGatewayDefault'), targetArg || selectedGw?.server_url || ''));
      // 顺带把每条存成案例，让案例库自增长
      for (const b of blocks) {
        try { await saveCase(b); } catch { /* 非关键 */ }
      }
    } finally {
      setAiBusy(false);
    }
  };

  const viewObject = () => {
    if (!parsed?.objectId) return;
    // 走 ?hcm-model=<对象ID> 分支：渲染 HCM 面板并触发对象浏览器自动定位到该对象。
    // 不能带 &hcm-detail=1 —— 那会渲染 HcmModelDetail 且把对象ID当模型名，打不开正确页。
    const u = `/web/?hcm-model=${encodeURIComponent(parsed.objectId)}`;
    window.open(u, '_blank', 'width=1280,height=860');
  };

  const queryCurrent = async () => {
    if (!parsed?.model || !parsed?.objectId || !parsed?.field) {
      setError(t('hcm.cfErrNeedModelIdField'));
      return;
    }
    if (!token.trim()) {
      setError(t('hcm.cfErrTokenMissing'));
      return;
    }
    setLoading(true);
    setError('');
    setErrKind('');
    try {
      const rec = await hcmDirect<Record<string, any>>(
        effectiveToken, 'hcm.model.get', { id: parsed.objectId }, parsed.model, targetArg
      );
      const v = rec.data?.[parsed.field];
      setCurrent({ value: mask(v), present: !(v === null || v === undefined || v === '') });
    } catch (e: any) {
      setCurrent(null);
      const c = classifyError(e);
      setErrKind(c.kind);
      setError(`${c.title}：${c.hint}`);
    } finally {
      setLoading(false);
    }
  };

  // ④ 批量字段体检：逐个对象拉 hcm.model.get，检查字段是否有值
  const runBatch = async () => {
    const model = batch.model.trim();
    const fields = batch.fields.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
    const ids = batch.ids.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
    if (!model || !fields.length || !ids.length) {
      setError(t('hcm.cfErrNoInput'));
      return;
    }
    if (!token.trim()) {
      setError(t('hcm.cfErrTokenMissing'));
      return;
    }
    const useIds = ids.slice(0, BATCH_MAX);
    setBatchRunning(true);
    setError('');
    setBatchRows([]);
    const rows: { id: string; field: string; value: string; present: boolean }[] = [];
    try {
      for (const id of useIds) {
        try {
          const rec = await hcmDirect<Record<string, any>>(
            effectiveToken, 'hcm.model.get', { id }, model, targetArg
          );
          for (const f of fields) {
            const v = rec.data?.[f];
            rows.push({
              id, field: f, value: mask(v),
              present: !(v === null || v === undefined || v === ''),
            });
          }
        } catch (e: any) {
          for (const f of fields) {
            rows.push({ id, field: f, value: `ERR: ${String(e?.message || e).slice(0, 60)}`, present: false });
          }
        }
      }
      setBatchRows(rows);
    } finally {
      setBatchRunning(false);
    }
  };

  // P2-3: 批量体检结果导出 CSV（带 BOM 供 Excel 正确识别中文）。missingOnly 仅导出空值/异常行。
  const exportCsv = (missingOnly: boolean) => {
    if (batchRows.length === 0) return;
    const rows = missingOnly ? batchRows.filter((r) => !r.present) : batchRows;
    if (rows.length === 0) return;
    const header = ['对象ID', '字段', '当前值', '状态', '修复建议'];
    const lines = rows.map((r) => [
      r.id, r.field, r.value,
      r.present ? '有值' : '空/异常',
      r.present ? '' : `补 ${r.field}（参考其它对象取值）`,
    ].map((c) => `"${String(c).replace(/"/g, '""')}"`).join(','));
    const csv = '﻿' + [header.join(','), ...lines].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `hcm-batch-${missingOnly ? 'missing' : 'all'}-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const copy = async (val: string, key: string) => {
    try {
      await navigator.clipboard.writeText(val);
      setCopied(key);
      setTimeout(() => setCopied(''), 1500);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  };

  const errInfo = lookupErrcode(parsed?.errcode);
  // P3-2: 基础设施错误码（如达梦 -70028）走独立词典，正数业务码查不到它
  const infraInfo = lookupInfraErrcode(parsed?.dbCode);
  const batchMissing = batchRows.filter((r) => !r.present).length;

  // ⑥ Jira 工单：把定位结论转成 issue
  const createJira = async () => {
    if (!parsed) return;
    if (!jira.projectKey.trim()) {
      setError(t('hcm.cfErrJiraNeedProject'));
      return;
    }
    const summary = `[HCM云函数错误] ${parsed.model ?? '?'}#${parsed.objectId ?? '?'} 字段 ${parsed.field ?? '?'}`;
    const lines = [
      fmt(parsed),
      '',
      `模型: ${parsed.model ?? '?'}`,
      `对象ID: ${parsed.objectId ?? '?'}`,
      `字段: ${parsed.field ?? '?'}`,
      `字段值(报错时): ${mask(parsed.value)}`,
      `阶段: ${parsed.stage ?? '?'}`,
      parsed.errcode ? `业务错误码: ${parsed.errcode}${errInfo ? ` (${errInfo.name})` : ''}` : '',
      parsed.errorCode ? `错误号(服务端日志索引): ${parsed.errorCode}` : '',
      current ? `当前字段值: ${current.value}（${current.present ? '有值' : '空/不存在'}）` : '',
      parsed.message ? `原因: ${parsed.message}` : '',
    ].filter(Boolean);
    setJiraBusy(true);
    setError('');
    try {
      const res = await fetch('/api/jira/issue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_key: jira.projectKey.trim(),
          summary: summary.slice(0, 200),
          description: lines.join('\n'),
          issuetype: jira.issuetype.trim() || '任务',
        }),
      });
      const data = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      if (!res.ok) {
        const detail = typeof data?.detail === 'string' ? data.detail : JSON.stringify(data);
        throw new Error(detail || `HTTP ${res.status}`);
      }
      setJiraResult({ key: data.key, url: data.url });
    } catch (e: any) {
      setJiraResult(null);
      setError(String(e?.message || e));
    } finally {
      setJiraBusy(false);
    }
  };

  // 一键给 AI：优先请求后端聚合诊断上下文（词典 + Wiki 片段 + Token + 案例），
  // 后端不可用时仍回退到本地上下文，保证原有复制/下载流程不中断。
  const genAi = async () => {
    setAiBusy(true);
    setError('');
    const baseOpts = {
      raw: text,
      p: parsed,
      cur: current,
      gwName: selectedGw?.name || t('hcm.cfErrGatewayDefault'),
      gwUrl: targetArg || selectedGw?.server_url || '',
      errName: errInfo?.name,
      errMeaning: errInfo?.meaning,
      errFix: errInfo?.fix,
    };
    try {
      const res = await fetch('/api/cf/diagnose-context', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          server_url: targetArg || selectedGw?.server_url || '',
          token: effectiveToken,
          model: parsed?.model || manual.model,
          object_id: parsed?.objectId || manual.objectId,
          field: parsed?.field || manual.field,
          max_docs: 3,
          max_chars: 1500,
          case_limit: 5,
        }),
      });
      const data = await res.json().catch(() => ({} as DiagnosisContext));
      if (!res.ok || data?.ok === false) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${res.status}`);
      }
      setAi(buildAiContext({ ...baseOpts, diagnosis: data as DiagnosisContext }));
      // P1-3: 顺带把本次定位存成案例（best-effort，失败不阻塞 AI 上下文），
      // 让 similarCases / feedback_metrics 随时间自增长，闭环真正闭合。
      try { await saveCase(); } catch { /* 非关键，忽略 */ }
    } catch (e) {
      // 诊断聚合是增强能力，后端暂不可用不应阻断本地 AI 上下文。
      setAi(buildAiContext(baseOpts));
      setError(`后端诊断上下文暂不可用，已使用本地上下文：${String((e as any)?.message || e)}`);
    } finally {
      setAiBusy(false);
    }
  };

  const downloadAi = () => {
    if (!ai) return;
    const blob = new Blob([ai], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `hcm-cf-error-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // 反馈闭环反哺：扫描 diagnosis_feedback.jsonl，把人工确认的根因写回词典/路由索引。
  // apply=false 仅预览提案；apply=true 先备份再回写。
  async function runFeedbackLearn(apply: boolean) {
    setLearnBusy(true);
    setLearnMsg('');
    setLearnDetail(null);
    try {
      const res = await fetch('/api/cf/cases/feedback-learn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apply }),
      });
      const d = await res.json().catch(() => ({} as any));
      if (!res.ok || d?.ok === false) {
        throw new Error(d?.detail || d?.error || `HTTP ${res.status}`);
      }
      setLearnDetail(d);
      const c = d?.applied_changes || {};
      const parts = [
        `样本 ${d?.sample_count ?? 0} 个`,
        d?.applied ? '已回写' : '仅预览',
      ];
      if (d?.applied) {
        parts.push(
          `新增词典 ${((c.errdict_new as string[]) || []).length} 条`,
          `更新词典 ${((c.errdict_updated as string[]) || []).length} 条`,
          `新增路由 ${((c.route_new as string[]) || []).length} 条`,
        );
      }
      setLearnMsg(parts.join(' · '));
    } catch (e: any) {
      setLearnMsg(`${t('hcm.cfErrLearnFail')}: ${e?.message || e}`);
    } finally {
      setLearnBusy(false);
    }
  }

  // 把本次定位结果存成案例（写入案例库，供 similarCases / 反馈闭环使用）。
  // 返回保存的文件名，供后续反馈引用；失败返回 ''。可传入指定 LocInfo（多块分诊逐条存）。
  const saveCase = async (srcIn?: LocInfo): Promise<string> => {
    const src = srcIn || parsed;
    if (!src) { setCaseMsg(t('hcm.cfErrNoInput')); return ''; }
    const eInfo = lookupErrcode(src.errcode);
    setCaseBusy(true);
    setCaseMsg('');
    try {
      const content = [
        '## HCM 云函数错误定位案例',
        '',
        `- 时间: ${new Date().toLocaleString('zh-CN')}`,
        `- 网关: ${selectedGw?.name || t('hcm.cfErrGatewayDefault')}`,
        `- 模型: ${src.model ?? '?'}`,
        `- 对象ID: ${src.objectId ?? '?'}`,
        `- 字段: ${src.field ?? '?'}`,
        `- 字段值(报错时,脱敏): ${mask(src.value)}`,
        src.stage ? `- 阶段: ${src.stage}` : '',
        src.errcode ? `- 业务错误码: ${src.errcode}${eInfo ? ` (${eInfo.name})` : ''}` : '',
        src.errorCode ? `- 错误号: ${src.errorCode}` : '',
        src.message ? `- 原因: ${src.message}` : '',
        current ? `- 当前字段值: ${current.value}（${current.present ? '有值' : '空/不存在'}）` : '',
        '',
        '### 定位文本',
        '```',
        fmt(src),
        '```',
      ].filter(Boolean).join('\n');
      const res = await fetch('/api/cf/cases/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content,
          errcode: String(src.errcode ?? ''),
          log_type: '',
          source: 'panel',
        }),
      });
      const d = await res.json().catch(() => ({} as any));
      if (!res.ok || d?.ok === false) throw new Error(d?.detail || d?.error || `HTTP ${res.status}`);
      setCaseFile(d.filename || '');
      setCaseMsg(`${t('hcm.cfErrSaveCaseOk')}: ${d.filename || ''}`);
      return d.filename || '';
    } catch (e: any) {
      setCaseMsg(`${t('hcm.cfErrSaveCase')} ${t('hcm.cfErrLearnFail')}: ${e?.message || e}`);
      return '';
    } finally {
      setCaseBusy(false);
    }
  };

  // 反馈诊断结果（correct/partially_correct/wrong），写回 diagnosis_feedback.jsonl 驱动闭环反哺。
  // 若尚未保存案例，先自动存案例再关联反馈。
  const submitFeedback = async () => {
    let file = caseFile;
    if (!file) file = await saveCase();
    if (!file) { setFbMsg(t('hcm.cfErrNeedCase')); return; }
    setFbBusy(true);
    setFbMsg('');
    try {
      const res = await fetch('/api/cf/cases/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          case_file: file,
          result: fbResult,
          actual_root_cause: fbRootCause.trim(),
          fix_applied: null,
          notes: fbNotes.trim(),
          source: 'panel',
        }),
      });
      const d = await res.json().catch(() => ({} as any));
      if (!res.ok || d?.ok === false) throw new Error(d?.detail || d?.error || `HTTP ${res.status}`);
      setFbMsg(t('hcm.cfErrFeedbackOk'));
      setFbRootCause('');
      setFbNotes('');
    } catch (e: any) {
      setFbMsg(`${t('hcm.cfErrFeedbackSubmit')} ${t('hcm.cfErrLearnFail')}: ${e?.message || e}`);
    } finally {
      setFbBusy(false);
    }
  };

  // ---- Token 健康度 + 一键重新登录 ---------------------------------------- //
  // HCM token 默认仅 2 小时有效（hcm_cloud.context_expire_seconds）。过期后服务端
  // 解析不出会话，接口内部异常被兜底成 17003（而非标准的 51006）——这是 17003 最常见真因。
  // 服务端时钟可能比本机快，年龄可能为负，显示时按 0 处理。
  const tokenAge = useMemo(() => tokenAgeHours(effectiveToken), [effectiveToken]);
  const tokenExpired = isTokenLikelyExpired(effectiveToken);
  const gwUrl = selectedGw?.server_url || '';
  const isTokenErr = parsed?.errcode === 17003 || parsed?.errcode === 51006;

  async function relogin() {
    if (!gwUrl) { setReloginMsg(t('hcm.cfErrReloginNoGw')); return; }
    setReloginBusy(true);
    setReloginMsg('');
    try {
      const res = await fetch('/api/cf/refresh-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ server_url: gwUrl, proxy: '' }),
      });
      const d = await res.json().catch(() => ({}) as any);
      if (!res.ok || !d?.ok) {
        throw new Error(d?.detail || d?.error || `HTTP ${res.status}`);
      }
      if (d.token) {
        setGwTokenOverride(d.token);          // 立即生效（effectiveToken 优先取它）
        localStorage.setItem(LS_TOKEN, d.token); // 并持久化，供其它面板复用
      }
      setReloginMsg(t('hcm.cfErrReloginOk'));
      setErrKind('');
    } catch (e: any) {
      setReloginMsg(`${t('hcm.cfErrReloginFail')}: ${e?.message || e}`);
    } finally {
      setReloginBusy(false);
    }
  }

  return (
    <div className="hcm-detail-page hcm-cf-err">
      <div className="hcm-cf-err-head">
        <h3>{t('hcm.cfErrTitle')}</h3>
        <p className="hcm-cf-err-hint">{t('hcm.cfErrHint')}</p>
      </div>

      {/* P3-3: 后端健康检查 —— 连不上时直接给启动命令，避免界面「不可用」却无提示 */}
      {backendDown && (
        <div className="hcm-cf-err-backend-down">
          <strong>{t('hcm.cfErrBackendDown')}</strong>
          <div className="hcm-cf-err-backend-hint">{t('hcm.cfErrBackendHint')}</div>
          <code className="hcm-mono">python3 api/server.py</code>
          <button className="btn btn-xs" onClick={() => copy('python3 api/server.py', 'be')}>
            {copied === 'be' ? t('hcm.copied') : t('hcm.cfErrCopyCode')}
          </button>
        </div>
      )}

      <div className="hcm-cf-err-input">
        {/* 网关选择：默认用配置的 proxy_target；当其 502 不可达时，可切到其它可达的 HCM 部署 */}
        <div className="hcm-cf-err-gateway">
          <label className="hcm-config-label">
            {t('hcm.cfErrGateway')}
            <select className="hcm-input" value={gwKey} onChange={(e) => { setGwKey(e.target.value); setGwAutoName(''); setErrKind(''); }}>
              {gateways.length === 0 && <option value="hcm_proxy">{t('hcm.cfErrGatewayDefault')}</option>}
              {gateways.map((g) => (
                <option key={g.key} value={g.key}>
                  {g.name}（{g.server_url}）{g.has_preset_token ? ' · 预设token' : ' · 需填token'}
                </option>
              ))}
            </select>
          </label>
          {gwAutoName && (
            <span className="hcm-cf-err-gwauto">{t('hcm.cfErrGatewayAuto')}{gwAutoName}</span>
          )}
          {selectedGw && !selectedGw.has_preset_token && (
            <input className="hcm-input" placeholder={t('hcm.cfErrGatewayToken')}
              value={gwTokenOverride} onChange={(e) => setGwTokenOverride(e.target.value)} />
          )}
        </div>

        {/* Token 健康度：HCM token 默认仅 2 小时有效，过期后查询会报 17003 */}
        <div className={`hcm-cf-err-token${tokenExpired ? ' is-expired' : ''}`}>
          <div className="hcm-cf-err-token-row">
            <span className="hcm-cf-err-token-label">{t('hcm.cfErrTokenHealth')}</span>
            <span className="hcm-cf-err-token-val">
              {tokenAge === null
                ? t('hcm.cfErrTokenUnknown')
                : `${t('hcm.cfErrTokenAge', { h: (tokenAge < 0 ? 0 : tokenAge).toFixed(1) })}`
                  + (tokenExpired ? ` · ${t('hcm.cfErrTokenExpired')}` : '')}
            </span>
            <button className="btn btn-xs" onClick={relogin} disabled={reloginBusy || !gwUrl}>
              {reloginBusy ? t('hcm.cfErrReloginBusy') : t('hcm.cfErrRelogin')}
            </button>
          </div>
          <p className="hcm-cf-err-token-hint">
            {t('hcm.cfErrTokenTtlHint', { h: HCM_TOKEN_TTL_HOURS })}
          </p>
          {reloginMsg && <p className="hcm-cf-err-token-msg">{reloginMsg}</p>}
        </div>

        <label className="hcm-config-label">
          {t('hcm.cfErrPaste')}
          <textarea
            className="hcm-cf-err-textarea"
            rows={3}
            placeholder="[定位] model=employee id=5841977 field=id_card value=null stage=field_read || 身份证号为空，无法继续"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        <div className="hcm-cf-err-manual">
          <input className="hcm-input" placeholder={t('hcm.cfErrManualModel')} value={manual.model}
            onChange={(e) => setManual({ ...manual, model: e.target.value })} />
          <input className="hcm-input" placeholder={t('hcm.cfErrManualId')} value={manual.objectId}
            onChange={(e) => setManual({ ...manual, objectId: e.target.value })} />
          <input className="hcm-input" placeholder={t('hcm.cfErrManualField')} value={manual.field}
            onChange={(e) => setManual({ ...manual, field: e.target.value })} />
        </div>
        <button className="btn btn-sm btn-primary" onClick={locate}>{t('hcm.cfErrParse')}</button>
      </div>

      {error && (
        <div className={`hcm-cf-err-error${errKind ? ' hcm-cf-err-error--' + errKind : ''}`}>
          {error}
          {errKind === 'gw' && gateways.length > 1 && (
            <div className="hcm-cf-err-gwtip">{t('hcm.cfErrGatewayTip')}</div>
          )}
          {/* 17003/51006：实测绝大多数是真·token 过期，直接给一键重登入口 */}
          {isTokenErr && (
            <div className="hcm-cf-err-gwtip">
              {t('hcm.cfErrTokenErrTip')}
              <button className="btn btn-xs" onClick={relogin} disabled={reloginBusy || !gwUrl}>
                {reloginBusy ? t('hcm.cfErrReloginBusy') : t('hcm.cfErrRelogin')}
              </button>
            </div>
          )}
        </div>
      )}

      {/* P2-1 多错误批量分诊：粘贴含多个 [定位] 块时，逐条列出并支持定位/给AI */}
      {blocks.length > 1 && (
        <div className="hcm-cf-err-triage">
          <div className="hcm-cf-err-section-title">{t('hcm.cfErrTriageTitle')}（{blocks.length}）</div>
          <p className="hcm-cf-err-hint">{t('hcm.cfErrTriageHint')}</p>
          <ul className="hcm-cf-err-triage-list">
            {blocks.map((b, i) => (
              <li key={i} className={parsed === b ? 'active' : ''}>
                <button className="hcm-cf-err-triage-item" onClick={() => selectBlock(b)}>
                  <span className="hcm-mono">#{i + 1}</span>
                  <span className="hcm-mono">{b.model ?? '?'}#{b.objectId ?? '?'}</span>
                  <span className="hcm-mono">{b.field ?? '?'}</span>
                  {b.errcode ? <span className="hcm-mono">err {b.errcode}</span> : null}
                </button>
                <button className="btn btn-xs" onClick={() => selectBlock(b)}>{t('hcm.cfErrTriageLocate')}</button>
              </li>
            ))}
          </ul>
          <button className="btn btn-sm" onClick={genAiMulti} disabled={aiBusy}>
            {aiBusy ? t('hcm.loading') : t('hcm.cfErrTriageAllAi')}
          </button>
        </div>
      )}

      {/* ② 错误码词典 */}
      {errInfo && (
        <div className="hcm-cf-err-dict">
          <div className="hcm-loc-card">
            <span className="hcm-loc-k">{t('hcm.cfErrDictCode')}</span>
            <span className="hcm-loc-v hcm-mono">{parsed?.errcode} · {errInfo.name}</span>
          </div>
          <div className="hcm-loc-card">
            <span className="hcm-loc-k">{t('hcm.cfErrDictMeaning')}</span>
            <span className="hcm-loc-v">{errInfo.meaning}</span>
          </div>
          <div className="hcm-loc-card hcm-loc-card--msg">
            <span className="hcm-loc-k">{t('hcm.cfErrDictFix')}</span>
            <span className="hcm-loc-v">{errInfo.fix}</span>
          </div>
        </div>
      )}

      {parsed && (
        <div className="hcm-cf-err-result">
          {/* P3-4: 错误类别 + 通用错误特征。无 [定位] 埋点的错误（如达梦 -70028）
              也能一眼看出「是什么类别、哪个异常、哪个错误码、哪行代码抛出」 */}
          {(parsed.kind && parsed.kind !== 'unknown') || parsed.exception || parsed.dbCode !== undefined || parsed.trace ? (
            <div className="hcm-cf-err-kindbar">
              <span className={`hcm-cf-err-kind hcm-cf-err-kind--${parsed.kind || 'unknown'}`}>
                {ERR_KIND_GUIDE[parsed.kind || 'unknown'].label}
              </span>
              {parsed.exception && <span className="hcm-mono">{parsed.exception}</span>}
              {parsed.dbCode !== undefined && <span className="hcm-mono">CODE:{parsed.dbCode}</span>}
              {parsed.trace && <span className="hcm-mono hcm-cf-err-trace">{parsed.trace}</span>}
            </div>
          ) : null}
          <div className="hcm-loc-card">
            <span className="hcm-loc-k">{t('hcm.cfErrObject')}</span>
            <span className="hcm-loc-v hcm-mono">
              {parsed.model ?? '?'} #{parsed.objectId ?? '?'}
              {parsed.objectId && (
                <button className="btn btn-xs" onClick={viewObject}>{t('hcm.cfErrViewObject')}</button>
              )}
            </span>
          </div>
          <div className="hcm-loc-card">
            <span className="hcm-loc-k">{t('hcm.cfErrField')}</span>
            <span className="hcm-loc-v hcm-mono">{parsed.field ?? '?'}</span>
          </div>
          <div className="hcm-loc-card">
            <span className="hcm-loc-k">{t('hcm.cfErrValue')}</span>
            <span className="hcm-loc-v hcm-mono">{mask(parsed.value)}</span>
          </div>
          {parsed.stage && (
            <div className="hcm-loc-card">
              <span className="hcm-loc-k">{t('hcm.cfErrStage')}</span>
              <span className="hcm-loc-v hcm-mono">{parsed.stage}</span>
            </div>
          )}
          {parsed.errorCode && (
            <div className="hcm-loc-card">
              <span className="hcm-loc-k">{t('hcm.cfErrCode')}</span>
              <span className="hcm-loc-v hcm-mono">{parsed.errorCode}
                <button className="btn btn-xs" onClick={() => copy(parsed.errorCode!, 'code')}>
                  {copied === 'code' ? t('hcm.copied') : t('hcm.cfErrCopyCode')}
                </button>
              </span>
            </div>
          )}
          {parsed.message && (
            <div className="hcm-loc-card hcm-loc-card--msg">
              <span className="hcm-loc-k">{t('hcm.cfErrMsg')}</span>
              <span className="hcm-loc-v">{parsed.message}</span>
            </div>
          )}
          <div className="hcm-cf-err-actions">
            {parsed.model && parsed.objectId && parsed.field && (
              <button className="btn btn-sm btn-primary" onClick={queryCurrent} disabled={loading}>
                {loading ? t('hcm.loading') : t('hcm.cfErrQueryCurrent')}
              </button>
            )}
            <button className="btn btn-sm" onClick={() => copy(fmt(parsed), 'loc')}>
              {copied === 'loc' ? t('hcm.copied') : t('hcm.cfErrCopyLoc')}
            </button>
          </div>

          {current && (
            <div className="hcm-cf-err-current">
              <div className="hcm-loc-card">
                <span className="hcm-loc-k">{t('hcm.cfErrCurrentValue')}</span>
                <span className="hcm-loc-v hcm-mono">{current.value}</span>
              </div>
              <div className={`hcm-cf-err-present ${current.present ? 'ok' : 'bad'}`}>
                {current.present ? t('hcm.cfErrFieldPresent') : t('hcm.cfErrFieldMissing')}
              </div>
            </div>
          )}
        </div>
      )}

      {/* P3-4: 错误分类与排查路径 —— 让非业务类错误（DB/网络/Token）也有可执行指引，
          而不是只显示「无有效信息」把人卡住 */}
      {parsed && (parsed.kind || parsed.dbCode !== undefined || parsed.exception) && (
        <div className="hcm-cf-err-guide">
          <div className="hcm-cf-err-section-title">{t('hcm.cfErrGuideTitle')}</div>
          {infraInfo && (
            <div className="hcm-cf-err-dict-card">
              <div className="hcm-cf-err-dict-name">
                {infraInfo.name} · <span className="hcm-mono">{parsed.dbCode}</span>
              </div>
              <div className="hcm-cf-err-dict-meaning">{infraInfo.meaning}</div>
              <div className="hcm-cf-err-dict-fix">{infraInfo.fix}</div>
            </div>
          )}
          <div className="hcm-cf-err-guide-label">
            {t('hcm.cfErrGuideKind')}：
            <span className={`hcm-cf-err-kind hcm-cf-err-kind--${parsed.kind || 'unknown'}`}>
              {ERR_KIND_GUIDE[parsed.kind || 'unknown'].label}
            </span>
          </div>
          <ol className="hcm-cf-err-guide-steps">
            {ERR_KIND_GUIDE[parsed.kind || 'unknown'].steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
      )}

      {/* 一键给 AI：生成排查上下文，复制/下载 .md 直接交给 AI 分析 */}
      <div className="hcm-cf-err-ai">
        <div className="hcm-cf-err-section-title">{t('hcm.cfErrAiTitle')}</div>
        <div className="hcm-cf-err-ai-actions">
          <button className="btn btn-sm btn-primary" onClick={genAi} disabled={aiBusy}>
            {aiBusy ? t('hcm.loading') : t('hcm.cfErrAiGen')}
          </button>
          {ai && (
            <>
              <button className="btn btn-sm" onClick={() => copy(ai, 'ai')}>
                {copied === 'ai' ? t('hcm.copied') : t('hcm.cfErrAiCopy')}
              </button>
              <button className="btn btn-sm" onClick={downloadAi}>{t('hcm.cfErrAiDownload')}</button>
            </>
          )}
        </div>
        {ai && (
          <details className="hcm-cf-err-ai-preview" open>
            <summary>{t('hcm.cfErrAiPreview')}</summary>
            <pre>{ai}</pre>
          </details>
        )}
      </div>

      {/* 案例库 & 反馈闭环（写入侧）：存案例 + 反馈诊断结果，喂给 similarCases / feedback-learn */}
      {parsed && (
        <div className="hcm-cf-err-learn">
          <div className="hcm-cf-err-section-title">{t('hcm.cfErrCaseTitle')}</div>
          <p className="hcm-cf-err-hint">{t('hcm.cfErrCaseHint')}</p>
          <div className="hcm-cf-err-ai-actions">
            <button className="btn btn-sm" onClick={() => saveCase()} disabled={caseBusy}>
              {caseBusy ? t('hcm.cfErrSaveCaseBusy') : t('hcm.cfErrSaveCase')}
            </button>
            {caseFile && (
              <span className="hcm-cf-err-learn-msg">{t('hcm.cfErrSaveCaseOk')}: {caseFile}</span>
            )}
          </div>
          {caseMsg && !caseFile && <div className="hcm-cf-err-learn-msg">{caseMsg}</div>}

          <div className="hcm-cf-err-manual">
            <select className="hcm-input" value={fbResult} onChange={(e) => setFbResult(e.target.value)}>
              <option value="correct">{t('hcm.cfErrFbCorrect')}</option>
              <option value="partially_correct">{t('hcm.cfErrFbPartial')}</option>
              <option value="wrong">{t('hcm.cfErrFbWrong')}</option>
              <option value="unknown">{t('hcm.cfErrFbUnknown')}</option>
            </select>
            <input className="hcm-input" placeholder={t('hcm.cfErrFeedbackRootCause')} value={fbRootCause}
              onChange={(e) => setFbRootCause(e.target.value)} />
            <input className="hcm-input hcm-input-wide" placeholder={t('hcm.cfErrFeedbackNotes')} value={fbNotes}
              onChange={(e) => setFbNotes(e.target.value)} />
            <button className="btn btn-sm btn-primary" onClick={submitFeedback} disabled={fbBusy}>
              {fbBusy ? t('hcm.cfErrFeedbackBusy') : t('hcm.cfErrFeedbackSubmit')}
            </button>
          </div>
          {fbMsg && <div className="hcm-cf-err-learn-msg">{fbMsg}</div>}
        </div>
      )}

      {/* 反馈闭环反哺：把人工确认的诊断结果写回词典 / 路由索引，形成自我修正闭环 */}
      <div className="hcm-cf-err-learn">
        <div className="hcm-cf-err-section-title">{t('hcm.cfErrLearnTitle')}</div>
        <p className="hcm-cf-err-hint">{t('hcm.cfErrLearnHint')}</p>
        <div className="hcm-cf-err-ai-actions">
          <button className="btn btn-sm" onClick={() => runFeedbackLearn(false)} disabled={learnBusy}>
            {learnBusy ? t('hcm.loading') : t('hcm.cfErrLearnPreview')}
          </button>
          <button className="btn btn-sm btn-primary" onClick={() => runFeedbackLearn(true)} disabled={learnBusy}>
            {t('hcm.cfErrLearnApply')}
          </button>
        </div>
        {learnMsg && <div className="hcm-cf-err-learn-msg">{learnMsg}</div>}
        {learnDetail?.proposal_path && (
          <div className="hcm-cf-err-learn-msg hcm-cf-err-learn-path">
            {t('hcm.cfErrLearnProposal')}: {learnDetail.proposal_path}
          </div>
        )}
      </div>

      {/* ⑥ Jira 工单 */}
      {parsed && (
        <div className="hcm-cf-err-jira">
          <div className="hcm-cf-err-section-title">{t('hcm.cfErrJiraTitle')}</div>
          <div className="hcm-cf-err-manual">
            <input className="hcm-input" placeholder={t('hcm.cfErrJiraProject')} value={jira.projectKey}
              onChange={(e) => setJira({ ...jira, projectKey: e.target.value })} />
            <input className="hcm-input" placeholder={t('hcm.cfErrJiraType')} value={jira.issuetype}
              onChange={(e) => setJira({ ...jira, issuetype: e.target.value })} />
            <button className="btn btn-sm btn-primary" onClick={createJira} disabled={jiraBusy}>
              {jiraBusy ? t('hcm.loading') : t('hcm.cfErrJiraCreate')}
            </button>
          </div>
          {jiraResult?.url && (
            <div className="hcm-cf-err-jira-ok">
              {t('hcm.cfErrJiraCreated')}:{' '}
              <a href={jiraResult.url} target="_blank" rel="noreferrer">{jiraResult.key}</a>
            </div>
          )}
        </div>
      )}

      {/* ④ 批量字段体检 */}
      <div className="hcm-cf-err-batch">
        <div className="hcm-cf-err-section-title">{t('hcm.cfErrBatchTitle')}</div>
        <div className="hcm-cf-err-manual">
          <input className="hcm-input" placeholder={t('hcm.cfErrBatchModel')} value={batch.model}
            onChange={(e) => setBatch({ ...batch, model: e.target.value })} />
          <input className="hcm-input hcm-input-wide" placeholder={t('hcm.cfErrBatchFields')} value={batch.fields}
            onChange={(e) => setBatch({ ...batch, fields: e.target.value })} />
        </div>
        <label className="hcm-config-label">
          {t('hcm.cfErrBatchIds')}
          <textarea className="hcm-cf-err-textarea" rows={2} value={batch.ids}
            onChange={(e) => setBatch({ ...batch, ids: e.target.value })} />
        </label>
        <button className="btn btn-sm btn-primary" onClick={runBatch} disabled={batchRunning}>
          {batchRunning ? t('hcm.loading') : t('hcm.cfErrBatchRun')}
        </button>

        {batchRows.length > 0 && (
          <>
            <div className="hcm-cf-err-batch-summary">
              {t('hcm.cfErrBatchSummary')}: {batchRows.length} · {t('hcm.cfErrBatchMissing')}: {batchMissing}
            </div>
            <div className="hcm-cf-err-table-wrap">
              <table className="hcm-cf-err-table">
                <thead>
                  <tr>
                    <th>{t('hcm.cfErrBatchColId')}</th>
                    <th>{t('hcm.fKey')}</th>
                    <th>{t('hcm.cfErrBatchColValue')}</th>
                    <th>{t('hcm.cfErrBatchColState')}</th>
                  </tr>
                </thead>
                <tbody>
                  {batchRows.map((r, i) => (
                    <tr key={`${r.id}-${r.field}-${i}`} className={r.present ? '' : 'bad'}>
                      <td className="hcm-mono">{r.id}</td>
                      <td className="hcm-mono">{r.field}</td>
                      <td className="hcm-mono">{r.value}</td>
                      <td>{r.present ? t('hcm.cfErrBatchOk') : t('hcm.cfErrBatchEmpty')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="hcm-cf-err-batch-actions">
              <button className="btn btn-sm" onClick={() => exportCsv(false)}>{t('hcm.cfErrBatchExportCsv')}</button>
              <button className="btn btn-sm" onClick={() => exportCsv(true)} disabled={batchMissing === 0}>
                {t('hcm.cfErrBatchExportCsvMissing')}（{batchMissing}）
              </button>
            </div>
          </>
        )}
      </div>

      {/* ③ 最近错误历史本 */}
      <div className="hcm-cf-err-history">
        <div className="hcm-cf-err-section-title">
          {t('hcm.cfErrHistory')}
          {history.length > 0 && (
            <button
              className="btn btn-xs"
              onClick={() => {
                setHistory([]);
                try { localStorage.removeItem(LS_HISTORY); } catch { /* 忽略 */ }
              }}
            >
              {t('hcm.cfErrHistoryClear')}
            </button>
          )}
        </div>
        {history.length === 0 ? (
          <div className="hcm-cf-err-hint">{t('hcm.cfErrHistoryEmpty')}</div>
        ) : (
          <ul className="hcm-cf-err-history-list">
            {history.map((h) => (
              <li key={`${h.ts}`}>
                <button className="hcm-cf-err-history-item" onClick={() => setText(h.text)}>
                  <span className="hcm-mono hcm-cf-err-history-ts">
                    {new Date(h.ts).toLocaleString()}
                  </span>
                  <span className="hcm-cf-err-history-text">{h.text.slice(0, 90)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* P2-4 改造工具前端化：上传云函数 .py → 审计/预览 diff/应用下载，全部浏览器内完成 */}
      <div className="hcm-cf-err-retrofit">
        <div className="hcm-cf-err-section-title">{t('hcm.cfErrRetrofitTitle')}</div>
        <p className="hcm-cf-err-hint">{t('hcm.cfErrRetrofitHint')}</p>
        <div className="hcm-cf-err-manual">
          <label className="hcm-config-label hcm-cf-err-upload">
            {t('hcm.cfErrRetrofitUpload')}
            <input type="file" accept=".py,text/x-python" onChange={onRetroFile} />
          </label>
          {retroName && <span className="hcm-cf-err-token-msg">{retroName}</span>}
          <select className="hcm-input" value={retroMode} onChange={(e) => setRetroMode(e.target.value as any)}>
            <option value="audit">{t('hcm.cfErrRetrofitModeAudit')}</option>
            <option value="diff">{t('hcm.cfErrRetrofitModeDiff')}</option>
            <option value="apply">{t('hcm.cfErrRetrofitModeApply')}</option>
          </select>
          <label className="hcm-config-label hcm-cf-err-redact">
            <input type="checkbox" checked={retroRedact} onChange={(e) => setRetroRedact(e.target.checked)} />
            {t('hcm.cfErrRetrofitRedact')}
          </label>
          <button className="btn btn-sm btn-primary" onClick={runRetrofit} disabled={retroBusy}>
            {retroBusy ? t('hcm.loading') : t('hcm.cfErrRetrofitRun')}
          </button>
        </div>

        {retroMsg && <div className="hcm-cf-err-learn-msg">{retroMsg}</div>}

        {retroMode === 'audit' && retroReport && (
          <div className="hcm-cf-err-table-wrap">
            {retroReport.risks?.length ? (
              <table className="hcm-cf-err-table">
                <thead>
                  <tr>
                    <th>{t('hcm.cfErrRiskLevel')}</th>
                    <th>{t('hcm.cfErrRiskType')}</th>
                    <th>{t('hcm.cfErrRiskLine')}</th>
                    <th>{t('hcm.cfErrRiskMsg')}</th>
                  </tr>
                </thead>
                <tbody>
                  {retroReport.risks.map((r: any, i: number) => (
                    <tr key={i} className={r.severity === 'high' ? 'bad' : ''}>
                      <td>{r.severity}</td>
                      <td className="hcm-mono">{r.type}</td>
                      <td className="hcm-mono">{r.line ?? '-'}</td>
                      <td>{r.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="hcm-cf-err-hint">{t('hcm.cfErrRetrofitNoRisk')}</div>
            )}
            {retroReport.classes?.length > 0 && (
              <div className="hcm-cf-err-learn-msg">class: {retroReport.classes.join(', ')}</div>
            )}
          </div>
        )}

        {retroMode === 'diff' && (
          <div className="hcm-cf-err-retrofit-diff">
            {retroChanged
              ? <pre>{retroDiff}</pre>
              : <div className="hcm-cf-err-hint">{t('hcm.cfErrRetrofitNoChange')}</div>}
          </div>
        )}

        {retroMode === 'apply' && retroNewContent && (
          <div className="hcm-cf-err-retrofit-diff">
            {retroChanged ? (
              <>
                <pre>{retroDiff}</pre>
                <button className="btn btn-sm btn-primary" onClick={downloadRetro}>{t('hcm.cfErrRetrofitApplyDownload')}</button>
              </>
            ) : (
              <div className="hcm-cf-err-hint">{t('hcm.cfErrRetrofitNoChange')}</div>
            )}
          </div>
        )}
      </div>

    </div>
  );
}
