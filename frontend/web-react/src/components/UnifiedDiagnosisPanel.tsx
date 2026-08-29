import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost } from '../api/client';
import { useAppStore } from '../store/useAppStore';
import type { K8sEnvsResp, K8sEnv } from '../api/types';

// --- 类型 ---

interface UnifiedSummary {
  root_cause?: string;
  confidence?: number;
  status?: string;
  reasons?: string[];
  checks_to_run?: string[];
  infrastructure_status?: string;
  cross_reference?: string;
  cf_diagnosis?: { root_cause?: string; confidence?: number };
  k8s_abnormal_count?: number;
  k8s_warning_count?: number;
}

interface AbnormalPod {
  name: string;
  namespace?: string;
  phase?: string;
  restarts?: number;
  node?: string;
  pattern_match?: {
    name?: string;
    meaning?: string;
    common_causes?: string[];
    diagnose_steps?: string[];
  };
}

interface WarningEvent {
  type?: string;
  reason?: string;
  message?: string;
  object_kind?: string;
  object_name?: string;
  last_seen?: string;
  meaning?: string;
  pattern_info?: { name?: string; meaning?: string };
}

interface CrashLog {
  pod?: string;
  namespace?: string;
  log?: string;
  log_type?: string;
  phase?: string;
  restarts?: number;
}

interface PatternMatch {
  pod?: string;
  phase?: string;
  pattern?: string;
  meaning?: string;
  common_causes?: string[];
  diagnose_steps?: string[];
}

interface K8sDiagnosis {
  available?: boolean;
  env?: string;
  error?: string;
  abnormal_pods?: AbnormalPod[];
  warning_events?: WarningEvent[];
  crash_logs?: CrashLog[];
  top_consumers?: { name?: string; cpu?: string; memory?: string }[];
  pattern_matches?: PatternMatch[];
}

interface CfDiagnosis {
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
    snippets?: { file?: string; section?: string; content?: string }[];
    matched_patterns?: { title?: string; hit_keywords?: string[] }[];
  };
  tokenHealth?: { age_hours?: number | null; expired?: boolean | null; hint?: string };
  similarCases?: { file?: string; title?: string; score?: number }[];
  sourceEvidence?: {
    terms?: string[];
    hits?: { file?: string; score?: number; hits?: { line?: number; matched?: string[]; excerpt?: string }[] }[];
  };
  logMatches?: { matches?: { file?: string; id?: string | number; create_time?: string; score?: number; message?: string }[] };
  evidenceBundle?: { confidence?: string; references?: any[]; hints?: string[] };
  referenceError?: {
    matched?: { name?: string; status_code?: number; errmsg?: string }[];
    coverage_percent?: number;
    verified_coverage_percent?: number;
    inferred_coverage_percent?: number;
    verified_errdict_count?: number;
    inferred_errdict_count?: number;
    source_error_code_count?: number;
  };
}

interface UnifiedDiagnosisResult {
  ok?: boolean;
  unified_summary?: UnifiedSummary;
  cf_diagnosis?: CfDiagnosis;
  k8s_diagnosis?: K8sDiagnosis;
  aiPrompt?: string;
  diagnosed_at?: string;
}

// --- 组件 ---

export function UnifiedDiagnosisPanel() {
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);

  const [errorText, setErrorText] = useState('');
  const [k8sEnvs, setK8sEnvs] = useState<K8sEnv[]>([]);
  const [k8sEnv, setK8sEnv] = useState('');
  const [k8sNamespace, setK8sNamespace] = useState('');
  const [k8sPodFilter, setK8sPodFilter] = useState('');
  const [serverUrl, setServerUrl] = useState('');
  const [token, setToken] = useState(localStorage.getItem('hcm.token') || '');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<UnifiedDiagnosisResult | null>(null);
  const [activeSection, setActiveSection] = useState<'summary' | 'cf' | 'k8s' | 'prompt'>('summary');

  // 加载 K8s 环境列表
  const loadEnvs = useCallback(async () => {
    try {
      const d = await apiGet<K8sEnvsResp>('/api/k8s/env');
      const list = d.environments || [];
      setK8sEnvs(list);
      const cur = d.current || (list[0] && list[0].name) || '';
      setK8sEnv(cur);
    } catch (ex: any) {
      pushLog(`加载 K8s 环境失败：${ex.message}`, 'error');
    }
  }, [pushLog]);

  // 加载 CF 账号列表（获取 server_url）
  const loadCfAccounts = useCallback(async () => {
    try {
      const d = await apiGet<{ accounts: { name: string; server_url: string }[] }>('/api/cf/accounts');
      const accounts = d.accounts || [];
      if (accounts.length > 0 && !serverUrl) {
        setServerUrl(accounts[0].server_url);
      }
    } catch {
      // 静默忽略
    }
  }, [serverUrl]);

  useEffect(() => {
    loadEnvs();
    loadCfAccounts();
  }, [loadEnvs, loadCfAccounts]);

  // 诊断
  const doDiagnose = async () => {
    if (!errorText.trim()) {
      addToast('请先粘贴错误文本', 'warn');
      return;
    }
    setLoading(true);
    setResult(null);
    try {
      const d = await apiPost<UnifiedDiagnosisResult>('/api/diagnose', {
        text: errorText,
        server_url: serverUrl,
        token: token,
        k8s_env: k8sEnv,
        k8s_namespace: k8sNamespace,
        k8s_pod_filter: k8sPodFilter,
        k8s_tail: 100,
      });
      setResult(d);
      pushLog(`【统一诊断】完成：${d.unified_summary?.root_cause || 'UNKNOWN'}（置信度 ${((d.unified_summary?.confidence || 0) * 100).toFixed(0)}%）`);
    } catch (ex: any) {
      pushLog(`诊断失败：${ex.message}`, 'error');
      addToast(`诊断失败：${ex.message}`, 'error');
    } finally {
      setLoading(false);
    }
  };

  // 复制 AI 提示词
  const copyPrompt = () => {
    if (!result?.aiPrompt) return;
    navigator.clipboard.writeText(result.aiPrompt).then(() => {
      addToast('已复制 AI 诊断上下文', 'success');
    });
  };

  const summary = result?.unified_summary;
  const cf = result?.cf_diagnosis;
  const k8s = result?.k8s_diagnosis;

  const infraStatusColor = (s?: string) => {
    if (!s || s === 'healthy') return '#4caf50';
    if (s === 'unknown') return '#9e9e9e';
    return '#f44336';
  };

  const confidenceColor = (c?: number) => {
    if (!c) return '#9e9e9e';
    if (c >= 0.8) return '#f44336';
    if (c >= 0.6) return '#ff9800';
    return '#9e9e9e';
  };

  return (
    <div className="unified-diag-panel" style={{ padding: '16px', height: '100%', overflow: 'auto' }}>
      {/* 输入区 */}
      <div className="card-soft" style={{ marginBottom: '12px', padding: '16px' }}>
        <h3 style={{ margin: '0 0 12px 0' }}>CF + K8s 统一诊断</h3>
        <p style={{ color: '#666', fontSize: '13px', margin: '0 0 12px 0' }}>
          粘贴云函数错误文本，选择 K8s 环境，一键获取应用层 + 基础设施层联合诊断。
          AI 可同时看到 CF 错误解析和 K8s Pod/事件/日志证据，快速判断是代码问题还是环境问题。
        </p>

        <textarea
          className="ta"
          placeholder="粘贴错误文本（如 [定位] model=Xxx id=xxx field=xxx value=xxx stage=xxx || 原因描述，或纯 error_code）"
          value={errorText}
          onChange={(e) => setErrorText(e.target.value)}
          style={{ width: '100%', minHeight: '80px', marginBottom: '12px', fontFamily: 'monospace' }}
        />

        <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <label className="field-inline" style={{ flex: '1 1 200px' }}>
            K8s 环境
            <select className="sel" value={k8sEnv} onChange={(e) => setK8sEnv(e.target.value)}>
              <option value="">不诊断 K8s</option>
              {k8sEnvs.map((e) => (
                <option key={e.name} value={e.name}>{e.label || e.name} ({e.name})</option>
              ))}
            </select>
          </label>
          <label className="field-inline" style={{ flex: '1 1 150px' }}>
            命名空间
            <input className="inp" placeholder="留空=默认" value={k8sNamespace}
              onChange={(e) => setK8sNamespace(e.target.value)} />
          </label>
          <label className="field-inline" style={{ flex: '1 1 150px' }}>
            Pod 过滤
            <input className="inp" placeholder="Pod 名称（模糊）" value={k8sPodFilter}
              onChange={(e) => setK8sPodFilter(e.target.value)} />
          </label>
          <label className="field-inline" style={{ flex: '1 1 200px' }}>
            HCM 网关
            <input className="inp" placeholder="server_url" value={serverUrl}
              onChange={(e) => setServerUrl(e.target.value)} />
          </label>
          <label className="field-inline" style={{ flex: '1 1 200px' }}>
            Token
            <input className="inp" placeholder="HCM Token（可选）" value={token}
              onChange={(e) => setToken(e.target.value)} />
          </label>
        </div>

        <div style={{ marginTop: '12px', display: 'flex', gap: '8px' }}>
          <button className="btn btn-primary" onClick={doDiagnose} disabled={loading}>
            {loading ? '诊断中…' : '一键诊断'}
          </button>
          <button className="btn btn-ghost" onClick={() => { setErrorText(''); setResult(null); }}>
            清空
          </button>
        </div>
      </div>

      {/* 结果区 */}
      {result && (
        <div className="card-soft" style={{ padding: '16px' }}>
          {/* 子标签 */}
          <div className="k8s-subtabs" style={{ marginBottom: '12px' }}>
            {[
              { key: 'summary' as const, label: '联合结论' },
              { key: 'cf' as const, label: `CF 诊断${cf?.summary ? '' : ''}` },
              { key: 'k8s' as const, label: `K8s 诊断${k8s?.available ? ` (${k8s.abnormal_pods?.length || 0}异常)` : ''}` },
              { key: 'prompt' as const, label: 'AI 提示词' },
            ].map((st) => (
              <button
                key={st.key}
                className={'k8s-subtab' + (activeSection === st.key ? ' active' : '')}
                onClick={() => setActiveSection(st.key)}
              >
                {st.label}
              </button>
            ))}
          </div>

          {/* 联合结论 */}
          {activeSection === 'summary' && summary && (
            <div className="diag-summary">
              <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap', marginBottom: '12px' }}>
                <div style={{ background: '#f5f5f5', padding: '8px 16px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '12px', color: '#999' }}>候选根因</div>
                  <div style={{ fontSize: '16px', fontWeight: 'bold', color: confidenceColor(summary.confidence) }}>
                    {summary.root_cause || 'UNKNOWN'}
                  </div>
                </div>
                <div style={{ background: '#f5f5f5', padding: '8px 16px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '12px', color: '#999' }}>置信度</div>
                  <div style={{ fontSize: '16px', fontWeight: 'bold', color: confidenceColor(summary.confidence) }}>
                    {((summary.confidence || 0) * 100).toFixed(0)}%
                  </div>
                </div>
                <div style={{ background: '#f5f5f5', padding: '8px 16px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '12px', color: '#999' }}>基础设施状态</div>
                  <div style={{ fontSize: '16px', fontWeight: 'bold', color: infraStatusColor(summary.infrastructure_status) }}>
                    {summary.infrastructure_status || 'unknown'}
                  </div>
                </div>
                <div style={{ background: '#f5f5f5', padding: '8px 16px', borderRadius: '6px' }}>
                  <div style={{ fontSize: '12px', color: '#999' }}>K8s 异常</div>
                  <div style={{ fontSize: '16px', fontWeight: 'bold', color: (summary.k8s_abnormal_count || 0) > 0 ? '#f44336' : '#4caf50' }}>
                    {summary.k8s_abnormal_count || 0} Pod / {summary.k8s_warning_count || 0} 事件
                  </div>
                </div>
              </div>

              {summary.cross_reference && (
                <div style={{
                  background: 'var(--warn-bg, #fff3cd)', border: '1px solid #ffc107',
                  borderRadius: '6px', padding: '10px 14px', marginBottom: '12px',
                }}>
                  <strong>交叉参考：</strong> {summary.cross_reference}
                </div>
              )}

              {summary.reasons && summary.reasons.length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>判断依据</h4>
                  <ul style={{ margin: 0, paddingLeft: '20px' }}>
                    {summary.reasons.map((r, i) => (
                      <li key={i} style={{ marginBottom: '4px' }}>{r}</li>
                    ))}
                  </ul>
                </div>
              )}

              {summary.checks_to_run && summary.checks_to_run.length > 0 && (
                <div>
                  <h4 style={{ margin: '0 0 6px 0' }}>建议排查步骤</h4>
                  <ol style={{ margin: 0, paddingLeft: '20px' }}>
                    {summary.checks_to_run.map((c, i) => (
                      <li key={i} style={{ marginBottom: '4px' }}>{c}</li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          )}

          {/* CF 诊断详情 */}
          {activeSection === 'cf' && cf && (
            <div className="diag-cf-detail">
              {cf.summary && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>CF 根因判断</h4>
                  <div style={{ display: 'flex', gap: '12px' }}>
                    <span>根因: <strong>{cf.summary.root_cause || 'UNKNOWN'}</strong></span>
                    <span>置信度: <strong style={{ color: confidenceColor(cf.summary.confidence) }}>
                      {((cf.summary.confidence || 0) * 100).toFixed(0)}%
                    </strong></span>
                  </div>
                </div>
              )}

              {cf.parsed && Object.keys(cf.parsed).filter(k => cf.parsed![k]).length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>解析信息</h4>
                  <table style={{ borderCollapse: 'collapse', fontSize: '13px' }}>
                    <tbody>
                      {Object.entries(cf.parsed).filter(([, v]) => v !== null && v !== undefined && v !== '')
                        .map(([k, v]) => (
                          <tr key={k}>
                            <td style={{ padding: '2px 12px 2px 0', color: '#999' }}>{k}</td>
                            <td style={{ padding: '2px 0', fontFamily: 'monospace' }}>{String(v)}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}

              {cf.errDict && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>错误码词典</h4>
                  <div style={{ background: '#f5f5f5', padding: '8px 12px', borderRadius: '4px' }}>
                    <div><strong>{cf.errDict.name}</strong></div>
                    <div style={{ color: '#666' }}>{cf.errDict.meaning}</div>
                    <div style={{ color: '#1565c0' }}>修复: {cf.errDict.fix}</div>
                  </div>
                </div>
              )}

              {cf.tokenHealth && cf.tokenHealth.age_hours !== null && cf.tokenHealth.age_hours !== undefined && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>Token 健康度</h4>
                  <div style={{ color: cf.tokenHealth.expired ? '#f44336' : '#4caf50' }}>
                    {cf.tokenHealth.hint}
                  </div>
                </div>
              )}

              {cf.sourceEvidence && cf.sourceEvidence.hits && cf.sourceEvidence.hits.length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>源码证据 ({cf.sourceEvidence.hits.length} 个文件)</h4>
                  {cf.sourceEvidence.hits.slice(0, 3).map((hit, i) => (
                    <div key={i} style={{ background: '#f5f5f5', padding: '6px 10px', borderRadius: '4px', marginBottom: '4px' }}>
                      <div style={{ fontFamily: 'monospace', fontSize: '12px' }}>{hit.file} (score: {hit.score})</div>
                      {hit.hits && hit.hits.slice(0, 2).map((h, j) => (
                        <div key={j} style={{ fontSize: '12px', color: '#666', marginLeft: '12px' }}>
                          L{h.line}: {h.matched?.join(', ')}
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              )}

              {cf.logMatches && cf.logMatches.matches && cf.logMatches.matches.length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>日志关联 ({cf.logMatches.matches.length} 条)</h4>
                  {cf.logMatches.matches.slice(0, 5).map((m, i) => (
                    <div key={i} style={{ fontSize: '12px', color: '#666', marginBottom: '2px' }}>
                      {m.file} id={m.id} time={m.create_time} score={m.score}: {m.message?.slice(0, 100)}
                    </div>
                  ))}
                </div>
              )}

              {cf.similarCases && cf.similarCases.length > 0 && (
                <div style={{ marginBottom: '12px' }}>
                  <h4 style={{ margin: '0 0 6px 0' }}>相似案例 ({cf.similarCases.length} 条)</h4>
                  {cf.similarCases.map((c, i) => (
                    <div key={i} style={{ fontSize: '12px', color: '#666' }}>
                      {c.file} (score: {c.score})
                    </div>
                  ))}
                </div>
              )}

              {cf.evidenceBundle && cf.evidenceBundle.references && cf.evidenceBundle.references.length > 0 && (
                <div>
                  <h4 style={{ margin: '0 0 6px 0' }}>证据包 ({cf.evidenceBundle.references.length} 条引用)</h4>
                  {cf.evidenceBundle.hints && cf.evidenceBundle.hints.length > 0 && (
                    <ul style={{ fontSize: '13px', color: '#1565c0' }}>
                      {cf.evidenceBundle.hints.map((h, i) => <li key={i}>{h}</li>)}
                    </ul>
                  )}
                </div>
              )}
            </div>
          )}

          {/* K8s 诊断详情 */}
          {activeSection === 'k8s' && k8s && (
            <div className="diag-k8s-detail">
              {!k8s.available ? (
                <div style={{ color: '#999', padding: '12px' }}>
                  K8s 诊断不可用: {k8s.error || '未指定环境'}
                </div>
              ) : (
                <>
                  {/* 异常 Pod */}
                  {k8s.abnormal_pods && k8s.abnormal_pods.length > 0 ? (
                    <div style={{ marginBottom: '12px' }}>
                      <h4 style={{ margin: '0 0 6px 0' }}>异常 Pod ({k8s.abnormal_pods.length})</h4>
                      {k8s.abnormal_pods.map((pod, i) => (
                        <div key={i} style={{ background: '#fff3cd', border: '1px solid #ffc107', borderRadius: '4px', padding: '8px 12px', marginBottom: '6px' }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                            <strong style={{ fontFamily: 'monospace' }}>{pod.name}</strong>
                            <span style={{ color: pod.phase !== 'Running' ? '#f44336' : '#ff9800' }}>
                              {pod.phase} (restarts: {pod.restarts})
                            </span>
                          </div>
                          <div style={{ fontSize: '12px', color: '#999' }}>
                            ns={pod.namespace} node={pod.node}
                          </div>
                          {pod.pattern_match && (
                            <div style={{ marginTop: '4px', fontSize: '13px' }}>
                              <strong>{pod.pattern_match.name}</strong>: {pod.pattern_match.meaning}
                              {pod.pattern_match.common_causes && pod.pattern_match.common_causes.length > 0 && (
                                <ul style={{ margin: '4px 0 0 0', paddingLeft: '20px' }}>
                                  {pod.pattern_match.common_causes.slice(0, 3).map((c, j) => <li key={j}>{c}</li>)}
                                </ul>
                              )}
                              {pod.pattern_match.diagnose_steps && pod.pattern_match.diagnose_steps.length > 0 && (
                                <ol style={{ margin: '4px 0 0 0', paddingLeft: '20px', color: '#1565c0' }}>
                                  {pod.pattern_match.diagnose_steps.slice(0, 3).map((s, j) => <li key={j}>{s}</li>)}
                                </ol>
                              )}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div style={{ color: '#4caf50', marginBottom: '12px' }}>
                      ✓ 所有 Pod 状态正常（Running/Succeeded）
                    </div>
                  )}

                  {/* Warning 事件 */}
                  {k8s.warning_events && k8s.warning_events.length > 0 && (
                    <div style={{ marginBottom: '12px' }}>
                      <h4 style={{ margin: '0 0 6px 0' }}>Warning 事件 ({k8s.warning_events.length})</h4>
                      {k8s.warning_events.slice(0, 10).map((ev, i) => (
                        <div key={i} style={{ fontSize: '12px', marginBottom: '4px', borderBottom: '1px solid #eee', paddingBottom: '4px' }}>
                          <span style={{ color: '#f44336', fontWeight: 'bold' }}>{ev.reason}</span>
                          {' '}
                          <span style={{ color: '#999' }}>[{ev.last_seen}]</span>
                          {' '}
                          <span>{ev.object_kind}/{ev.object_name}</span>
                          <div>{ev.message?.slice(0, 200)}</div>
                          {ev.meaning && <div style={{ color: '#1565c0' }}>→ {ev.meaning}</div>}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* 崩溃日志 */}
                  {k8s.crash_logs && k8s.crash_logs.length > 0 && (
                    <div style={{ marginBottom: '12px' }}>
                      <h4 style={{ margin: '0 0 6px 0' }}>异常容器日志</h4>
                      {k8s.crash_logs.map((cl, i) => (
                        <div key={i} style={{ marginBottom: '8px' }}>
                          <div style={{ fontSize: '12px', color: '#999' }}>
                            Pod <strong style={{ fontFamily: 'monospace' }}>{cl.pod}</strong>
                            {' '}({cl.log_type === 'current' ? '当前日志' : '上一轮日志'}, phase={cl.phase}, restarts={cl.restarts})
                          </div>
                          <pre style={{
                            background: '#1e1e1e', color: '#d4d4d4', padding: '8px 12px',
                            borderRadius: '4px', fontSize: '11px', overflow: 'auto',
                            maxHeight: '200px', whiteSpace: 'pre-wrap',
                          }}>
                            {cl.log}
                          </pre>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* 资源 Top */}
                  {k8s.top_consumers && k8s.top_consumers.length > 0 && (
                    <div>
                      <h4 style={{ margin: '0 0 6px 0' }}>资源占用 Top</h4>
                      <table style={{ borderCollapse: 'collapse', fontSize: '13px' }}>
                        <thead>
                          <tr style={{ borderBottom: '2px solid #ddd' }}>
                            <th style={{ padding: '4px 12px 4px 0', textAlign: 'left' }}>Pod</th>
                            <th style={{ padding: '4px 12px 4px 0' }}>CPU</th>
                            <th style={{ padding: '4px' }}>Memory</th>
                          </tr>
                        </thead>
                        <tbody>
                          {k8s.top_consumers.slice(0, 5).map((r, i) => (
                            <tr key={i} style={{ borderBottom: '1px solid #eee' }}>
                              <td style={{ padding: '4px 12px 4px 0', fontFamily: 'monospace' }}>{r.name}</td>
                              <td style={{ padding: '4px 12px 4px 0' }}>{r.cpu}</td>
                              <td style={{ padding: '4px' }}>{r.memory}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* AI 提示词 */}
          {activeSection === 'prompt' && result.aiPrompt && (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                <h4 style={{ margin: 0 }}>AI 诊断提示词</h4>
                <button className="btn btn-ghost btn-sm" onClick={copyPrompt}>复制</button>
              </div>
              <pre style={{
                background: '#1e1e1e', color: '#d4d4d4', padding: '12px',
                borderRadius: '6px', fontSize: '12px', overflow: 'auto',
                maxHeight: '500px', whiteSpace: 'pre-wrap',
              }}>
                {result.aiPrompt}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
