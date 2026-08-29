import { useEffect, useMemo, useState } from 'react';
import { useT } from '../../i18n';
import { HcmApiError } from '../../api/hcm/client';
import { hcmDirect } from '../../api/hcm/direct';
import {
  extractErrcode, lookupErrcode,
  HCM_TOKEN_TTL_HOURS, tokenAgeHours, isTokenLikelyExpired,
} from '../../api/hcm/errDict';

const LS_TOKEN = 'hcm.token';
const LS_HISTORY = 'hcm.cfErrHistory';
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
  return info;
}

function hasAnyInfo(i: LocInfo | null): boolean {
  if (!i) return false;
  return Boolean(i.model || i.objectId || i.field || i.errorCode || i.errcode);
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

  // 网关选择：默认用后端 proxy_target；当该网关 502 不可达时，可临时切到其它可达的 HCM 部署。
  interface Gateway { key: string; name: string; server_url: string; source: string; has_preset_token: boolean }
  const [gateways, setGateways] = useState<Gateway[]>([]);
  const [gwKey, setGwKey] = useState('hcm_proxy');
  const [gwTokenOverride, setGwTokenOverride] = useState('');
  const selectedGw = useMemo(() => gateways.find((g) => g.key === gwKey), [gateways, gwKey]);
  const targetArg = selectedGw?.server_url || ''; // 空 → 后端用默认 proxy_target
  const effectiveToken = gwTokenOverride.trim() || token; // 非预设网关需自带 token

  useEffect(() => {
    fetch('/api/hcm/envs')
      .then((r) => r.json())
      .then((d) => { if (Array.isArray(d?.envs)) setGateways(d.envs); })
      .catch(() => { /* 取不到环境列表时回退到默认网关 */ });
  }, []);

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

  // ⑤ 粘贴即解析：输入变化（含粘贴）时自动尝试定位，无需点按钮
  useEffect(() => {
    const fromText = parseLoc(text);
    if (hasAnyInfo(fromText)) {
      setParsed((prev) => ({ ...(prev || {}), ...fromText }));
      setCurrent(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

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

  const viewObject = () => {
    if (!parsed?.objectId) return;
    const u = `/web/?hcm-model=${encodeURIComponent(parsed.objectId)}&hcm-detail=1`;
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

      <div className="hcm-cf-err-input">
        {/* 网关选择：默认用配置的 proxy_target；当其 502 不可达时，可切到其它可达的 HCM 部署 */}
        <div className="hcm-cf-err-gateway">
          <label className="hcm-config-label">
            {t('hcm.cfErrGateway')}
            <select className="hcm-input" value={gwKey} onChange={(e) => { setGwKey(e.target.value); setErrKind(''); }}>
              {gateways.length === 0 && <option value="hcm_proxy">{t('hcm.cfErrGatewayDefault')}</option>}
              {gateways.map((g) => (
                <option key={g.key} value={g.key}>
                  {g.name}（{g.server_url}）{g.has_preset_token ? ' · 预设token' : ' · 需填token'}
                </option>
              ))}
            </select>
          </label>
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
    </div>
  );
}
