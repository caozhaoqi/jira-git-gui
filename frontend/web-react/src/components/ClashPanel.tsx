import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost } from '../api/client';
import { useT } from '../i18n';

interface NetIface {
  device: string;
  port: string;
  kind: 'wifi' | 'lan' | 'other';
  ip: string;
  status: string;
  flags?: string;
}
interface IfacesResp {
  ok?: boolean;
  error?: string;
  interfaces?: NetIface[];
  default_gateway?: string;
  default_gateway_ip?: string;
  services?: Service[];
}
interface Service {
  name: string;
  device: string;
  rank: number;
  disabled: boolean;
}
interface CheckResp {
  ok?: boolean;
  error?: string;
  ip: string;
  route_iface?: string;
  http_code?: string;
  reachable?: boolean;
  raw?: string;
}
interface GenResp {
  ok?: boolean;
  error?: string;
  ips?: string[];
  clash_rules?: string;
  route_add?: string;
  route_del?: string;
  hint?: string;
}
interface ApplyResp {
  ok?: boolean;
  error?: string;
  hint?: string;
  warning?: string;
  route?: { applied?: boolean; results?: { ip: string; ok: boolean; route?: string }[]; output?: string };
  clash?: { updated?: boolean; added?: number; removed?: number; backup?: string; skipped?: string; error?: string };
}
interface CfgCand {
  path: string;
  size: number;
  base: boolean;
  note: string;
}
interface DiagItem {
  key: string;
  label: string;
  ok: boolean | null;
  detail: string;
}

// 默认 IP 兜底（正常从 config/clash_defaults.local.json 经 GET /api/clash/defaults 读取）
const DEFAULT_IPS: string[] = [];
const KIND_LABEL: Record<string, string> = { wifi: 'Wi-Fi', lan: '有线/局域网', other: '其他' };

async function copyText(t: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch {
    return false;
  }
}

export function ClashPanel() {
  const { t } = useT();
  const [ifaces, setIfaces] = useState<NetIface[]>([]);
  const [defaultGw, setDefaultGw] = useState('');
  const [loadErr, setLoadErr] = useState('');
  const [lanDevice, setLanDevice] = useState('');
  const [wanDevice, setWanDevice] = useState('');
  const [services, setServices] = useState<Service[]>([]);
  const [defaultIps, setDefaultIps] = useState<string[]>(DEFAULT_IPS);
  const [ips, setIps] = useState<string[]>([]);
  const [newIp, setNewIp] = useState('');
  const [checks, setChecks] = useState<Record<string, CheckResp>>({});
  const [checking, setChecking] = useState(false);
  const [gen, setGen] = useState<GenResp | null>(null);
  const [genErr, setGenErr] = useState('');
  const [proxyPorts, setProxyPorts] = useState<{ port: number; open: boolean }[] | null>(null);
  const [copied, setCopied] = useState('');
  const [cfgPath, setCfgPath] = useState('');
  const [cfgCands, setCfgCands] = useState<CfgCand[]>([]);
  const [cfgCurrent, setCfgCurrent] = useState('');
  const [applying, setApplying] = useState(false);
  const [applyLog, setApplyLog] = useState<ApplyResp | null>(null);
  const [diag, setDiag] = useState<DiagItem[] | null>(null);
  const [diaging, setDiaging] = useState(false);
  const [fixing, setFixing] = useState(false);
  const [fixMsg, setFixMsg] = useState('');
  const [patchingAll, setPatchingAll] = useState(false);
  const [patchLog, setPatchLog] = useState('');
  const [genOpen, setGenOpen] = useState(false);

  const loadIfaces = useCallback(async () => {
    setLoadErr('');
    try {
      const r = await apiGet<IfacesResp>('/api/clash/interfaces');
      if (!r.ok) {
        setLoadErr(r.error || t('clash.detectFail'));
        return;
      }
      const list = r.interfaces || [];
      setIfaces(list);
      setDefaultGw(r.default_gateway || '');
      setServices(r.services || []);
      const lan = list.find((x) => x.kind === 'lan' && x.status === 'active' && !!x.ip);
      if (lan) setLanDevice((v) => v || lan.device);
      const gw = r.default_gateway;
      if (gw) setWanDevice((v) => v || gw);
    } catch (e: any) {
      setLoadErr(e.message || t('clash.ifaceFail'));
    }
  }, [t]);

  const loadProxy = useCallback(async () => {
    try {
      const r = await apiGet<{ ports?: { port: number; open: boolean }[] }>('/api/clash/proxy-status');
      setProxyPorts(r.ports || null);
    } catch {
      setProxyPorts(null);
    }
  }, []);

  const probeCfgPath = useCallback(async () => {
    try {
      const r = await apiGet<{ paths?: CfgCand[]; current_config?: string }>('/api/clash/config-path');
      const list = r.paths || [];
      setCfgCands(list);
      setCfgCurrent(r.current_config || '');
      const cur = list.find((x) => x.path.endsWith(r.current_config || '__none__'));
      const pick = cur || list.find((x) => !x.base) || list[0];
      if (pick && !cfgPath) setCfgPath(pick.path);
    } catch {
      setCfgCands([]);
    }
  }, [cfgPath]);

  const loadDefaults = useCallback(async () => {
    try {
      const r = await apiGet<{ default_ips?: string[]; lan_device?: string }>('/api/clash/defaults');
      const list = (r.default_ips || []).filter(Boolean);
      if (list.length) {
        setDefaultIps(list);
        setIps(list);
      }
      if (r.lan_device) setLanDevice((v) => v || r.lan_device || '');
    } catch {
      // 接口不可用则维持默认（DEFAULT_IPS 兜底为空，由用户手动添加）
    }
  }, []);

  useEffect(() => {
    loadDefaults();
    loadIfaces();
    loadProxy();
    probeCfgPath();
  }, [loadDefaults, loadIfaces, loadProxy, probeCfgPath]);

  const addIp = () => {
    const v = newIp.trim();
    if (!v) return;
    if (ips.includes(v)) {
      setNewIp('');
      return;
    }
    setIps((a) => [...a, v]);
    setNewIp('');
  };

  const removeIp = (ip: string) => setIps((a) => a.filter((x) => x !== ip));

  const checkAll = async () => {
    setChecking(true);
    const next: Record<string, CheckResp> = {};
    for (const ip of ips) {
      try {
        const r = await apiGet<CheckResp>(`/api/clash/check?ip=${encodeURIComponent(ip)}`);
        next[ip] = r;
      } catch (e: any) {
        next[ip] = { ok: false, ip, error: e.message || t('clash.checkFail') };
      }
      setChecks({ ...next });
    }
    setChecking(false);
  };

  const doGenerate = async () => {
    setGenErr('');
    try {
      const r = await apiPost<GenResp>('/api/clash/generate', {
        ips,
        lan_device: lanDevice,
        wan_device: wanDevice,
      });
      if (!r.ok) {
        setGenErr(r.error || t('clash.genFail'));
        setGen(null);
        return;
      }
      setGen(r);
      setGenOpen(true);
    } catch (e: any) {
      setGenErr(e.message || t('clash.genFail'));
    }
  };

  const copy = async (key: 'clash_rules' | 'route_add' | 'route_del') => {
    const tx = gen?.[key];
    if (!tx) return;
    const okc = await copyText(tx);
    setCopied(okc ? key : '');
    setTimeout(() => setCopied(''), 1500);
  };

  const doApply = async () => {
    setApplying(true);
    setApplyLog(null);
    try {
      const r = await apiPost<ApplyResp>('/api/clash/apply', {
        ips,
        lan_device: lanDevice,
        clash_config_path: cfgPath,
      });
      setApplyLog(r);
    } catch (e: any) {
      setApplyLog({ ok: false, error: e.message || t('clash.applyFail') });
    } finally {
      setApplying(false);
    }
  };

  const doRevert = async () => {
    setApplying(true);
    setApplyLog(null);
    try {
      const r = await apiPost<ApplyResp>('/api/clash/revert', {
        ips,
        lan_device: lanDevice,
        clash_config_path: cfgPath,
      });
      setApplyLog(r);
    } catch (e: any) {
      setApplyLog({ ok: false, error: e.message || t('clash.revertFail') });
    } finally {
      setApplying(false);
    }
  };

  const okCount = applyLog?.route?.results?.filter((r) => r.ok).length ?? 0;

  const doDiagnose = async () => {
    setDiaging(true);
    setDiag(null);
    try {
      const r = await apiPost<{ ok?: boolean; items?: DiagItem[]; error?: string }>(
        '/api/clash/diagnose',
        { ips, clash_config_path: cfgPath }
      );
      setDiag(r.items || []);
    } catch (e: any) {
      setDiag([{ key: 'err', label: t('clash.diagFail'), ok: false, detail: e.message || t('clash.unknownErr') }]);
    } finally {
      setDiaging(false);
    }
  };

  const doPatchAll = async () => {
    setPatchingAll(true);
    setPatchLog('');
    try {
      const r = await apiPost<{ ok?: boolean; error?: string; total?: number; updated?: number; skipped?: number; failed?: string[]; hint?: string }>(
        '/api/clash/patch-all',
        { ips }
      );
      if (!r.ok) {
        setPatchLog(`✗ ${r.error || t('clash.writeFail')}`);
      } else {
        setPatchLog(
          `✓ ${t('clash.patchedAll', { total: r.total ?? 0, updated: r.updated ?? 0, skipped: r.skipped ?? 0 })}${r.failed?.length ? `，${t('clash.patchedFail', { list: (r.failed || []).join('、') })}` : ''}`
        );
      }
    } catch (e: any) {
      setPatchLog(`✗ ${e.message || t('clash.writeFail')}`);
    } finally {
      setPatchingAll(false);
    }
  };

  const doFixServiceOrder = async () => {
    setFixing(true);
    setFixMsg('');
    try {
      const r = await apiPost<{ ok?: boolean; error?: string; wan_service?: string; new_order?: string[]; output?: string; route?: string; hint?: string }>(
        '/api/clash/fix-service-order',
        { wan_device: wanDevice, wan_service: '' }
      );
      setFixMsg(
        r.error
          ? `✗ ${r.error}`
          : `✓ ${t('clash.fixDone', { svc: r.wan_service || '', route: r.route || '' })}`
      );
    } catch (e: any) {
      setFixMsg(`✗ ${e.message || t('clash.fixFail')}`);
    } finally {
      setFixing(false);
      loadIfaces();
    }
  };

  // 检测：有线服务是否排在 Wi-Fi 前面（插线会抢默认路由 → 外网断）
  const wifiRank = services.find((s) => s.name.toLowerCase().includes('wi-fi'))?.rank ?? Infinity;
  const lanBeforeWifi = services.some((s) => !s.disabled && s.rank < wifiRank && /ethernet|usb|lan/i.test(s.name));
  const defaultHijacked = !!defaultGw && services.some(
    (s) => s.device === defaultGw && /ethernet|usb|lan/i.test(s.name)
  );
  const needFix = lanBeforeWifi || defaultHijacked;

  return (
    <div className="clash-panel">
      {/* ===== 网络接口 ===== */}
      <div className="card-soft clash-card">
        <div className="panel-header">
          <h2 className="section-title">{t('clash.ifaceTitle')}</h2>
          <button className="btn btn-sm btn-ghost" onClick={() => { loadIfaces(); loadProxy(); }}>
            🔄 {t('clash.redetect')}
          </button>
        </div>
        {loadErr && <div className="clash-err">{loadErr}</div>}
        {ifaces.length === 0 && !loadErr && <div className="empty-hint">{t('clash.detecting')}</div>}
        <div className="clash-iface-list">
          {ifaces.map((it) => {
            const active = it.status === 'active';
            return (
              <div key={it.device} className={`clash-iface ${active ? '' : 'off'}`}>
                <div className="clash-iface-head">
                  <span className={`status-dot ${active ? 'ok' : ''}`} />
                  <b className="clash-iface-dev">{it.device}</b>
                  <span className="clash-iface-kind">{KIND_LABEL[it.kind] || it.kind}</span>
                  <span className="clash-iface-port">{it.port}</span>
                  {it.device === defaultGw && (
                    <span className="clash-tag">{t('clash.defaultOut')}</span>
                  )}
                </div>
                <div className="clash-iface-meta">
                  <span className="mono">{it.ip || t('clash.noIp')}</span>
                  <span className="muted">{active ? t('clash.enabled') : t('clash.disabled')}</span>
                </div>
                <div className="clash-iface-radio">
                  <label title={t('clash.lanTip')}>
                    <input
                      type="radio"
                      name="lan-device"
                      checked={lanDevice === it.device}
                      onChange={() => setLanDevice(it.device)}
                    />
                    {t('clash.lanPort')}
                  </label>
                  <label title={t('clash.wanTip')}>
                    <input
                      type="radio"
                      name="wan-device"
                      checked={wanDevice === it.device}
                      onChange={() => setWanDevice(it.device)}
                    />
                    {t('clash.wanPort')}
                  </label>
                </div>
              </div>
            );
          })}
        </div>
        <div className="clash-hint">
          💡 {t('clash.ruleHint')}
          {proxyPorts?.map((p) => (
            <span key={p.port} className={`clash-proxy-port ${p.open ? 'open' : ''}`}>
              {p.port}:{p.open ? t('clash.listening') : t('clash.closed')}
            </span>
          ))}
        </div>
        {services.length > 0 && (
          <div className="clash-svc">
            <div className="clash-svc-label">{t('clash.svcOrder')}</div>
            <div className="clash-svc-order">
              {services.map((s, i) => (
                <span
                  key={s.name}
                  className={`clash-svc-item ${s.disabled ? 'off' : ''} ${/ethernet|usb|lan/i.test(s.name) ? 'lan' : 'wifi'}`}
                  title={`${t('clash.device')} ${s.device || '-'}${s.disabled ? `（${t('clash.disabled')}）` : ''}`}
                >
                  {i + 1}. {s.name.replace(/ \(.*$/, '')}
                  {s.disabled ? ` · ${t('clash.disabled')}` : ''}
                </span>
              ))}
            </div>
            {needFix && (
              <div className="clash-svc-warn">
                ⚠️ {t('clash.svcWarn')}
                <b>{t('clash.outerNetWarn')}</b>{t('clash.outerNetWarnTail')}
                <button className="btn btn-sm btn-primary" onClick={doFixServiceOrder} disabled={fixing}>
                  {fixing ? t('clash.fixing') : t('clash.fixOneClick')}
                </button>
                {fixMsg && <div className="clash-svc-fixmsg">{fixMsg}</div>}
              </div>
            )}
          </div>
        )}
      </div>

      {/* ===== 指定 IP 列表 ===== */}
      <div className="card-soft clash-card">
        <div className="panel-header">
          <h2 className="section-title">{t('clash.ipTitle')}</h2>
          <button className="btn btn-sm btn-primary" onClick={checkAll} disabled={checking || !ips.length}>
            {checking ? t('clash.checking') : t('clash.checkConn')}
          </button>
        </div>
        <div className="clash-ip-input">
          <input
            className="input"
            placeholder={t('clash.ipPlaceholder')}
            value={newIp}
            onChange={(e) => setNewIp(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addIp()}
          />
          <button className="btn" onClick={addIp} disabled={!newIp.trim()}>
            ＋ {t('clash.add')}
          </button>
          <button className="btn btn-ghost" onClick={() => setIps(defaultIps.length ? defaultIps : DEFAULT_IPS)} title={t('clash.restoreDefault')}>
            {t('clash.restoreDefault')}
          </button>
        </div>
        <div className="clash-ip-tags">
          {ips.length === 0 && <span className="muted">{t('clash.noIpYet')}</span>}
          {ips.map((ip) => {
            const c = checks[ip];
            return (
              <div key={ip} className="clash-ip-tag">
                <span className="clash-ip-name">{ip}</span>
                {c && (
                  <span className={`clash-ip-check ${c.reachable ? 'ok' : 'bad'}`}>
                    {c.reachable ? `HTTP ${c.http_code}` : t('clash.unreachable')}
                    {c.route_iface ? ` · ${t('clash.via')} ${c.route_iface}` : ''}
                  </span>
                )}
                <button className="clash-ip-del" onClick={() => removeIp(ip)} title={t('clash.remove')}>
                  ×
                </button>
              </div>
            );
          })}
        </div>
        <div className="clash-hint">
          💡 {t('clash.checkHint')}
        </div>
      </div>

      {/* ===== 一键应用（自动执行） ===== */}
      <div className="card-soft clash-card">
        <div className="panel-header">
          <h2 className="section-title">{t('clash.applyTitle')}</h2>
          <div className="clash-apply-btns">
            <button
              className="btn btn-primary"
              onClick={doApply}
              disabled={applying || !ips.length || !lanDevice}
              title={t('clash.applyTip')}
            >
              {applying ? t('clash.running') : t('clash.applyOneClick')}
            </button>
            <button
              className="btn btn-ghost"
              onClick={doRevert}
              disabled={applying || !ips.length}
              title={t('clash.revertTip')}
            >
              {t('clash.revert')}
            </button>
          </div>
        </div>
        <div className="clash-cfg-path">
          <label>
            {t('clash.cfgPathLabel')}
            <input
              className="input"
              placeholder={t('clash.cfgPathPlaceholder')}
              value={cfgPath}
              onChange={(e) => setCfgPath(e.target.value)}
            />
          </label>
          <button className="btn btn-sm btn-ghost" onClick={probeCfgPath} title={t('clash.probePath')}>
            🔍 {t('clash.probe')}
          </button>
        </div>
        {cfgCands.length > 0 && (
          <div className="clash-cfg-cands">
            {cfgCands.map((c) => (
              <button
                key={c.path}
                className={`clash-cfg-cand ${c.base ? 'base' : ''} ${cfgPath === c.path ? 'sel' : ''} ${
                  c.path.endsWith(cfgCurrent) ? 'current' : ''
                }`}
                onClick={() => setCfgPath(c.path)}
                title={c.note}
              >
                {c.path.replace(/^.*\/\.config\/clash\//, '')}
                {c.path.endsWith(cfgCurrent) ? ` · ${t('clash.currentLoaded')}` : ''}
                {c.base ? ` · ${t('clash.template')}` : ''}
              </button>
            ))}
          </div>
        )}
        <div className="clash-cfg-actions">
          <button className="btn btn-sm" onClick={doPatchAll} disabled={patchingAll || !ips.length} title={t('clash.patchAllTip')}>
            {patchingAll ? t('clash.writing') : t('clash.patchAll')}
          </button>
          <button className="btn btn-sm btn-ghost" onClick={probeCfgPath} title={t('clash.reprobe')}>
            🔍 {t('clash.reprobe')}
          </button>
          {patchLog && <span className="clash-patch-msg">{patchLog}</span>}
        </div>
        <div className="clash-hint">
          ⚠️ {t('clash.cfgHint', { current: cfgCurrent || t('clash.unknown') })}
        </div>
        <div className="clash-diag-row">
          <button className="btn btn-sm" onClick={doDiagnose} disabled={diaging}>
            {diaging ? t('clash.diagging') : t('clash.diagnose')}
          </button>
          {diag && (
            <span className="clash-diag-summary">
              {diag.filter((x) => x.ok === true).length}/{diag.filter((x) => x.ok !== null).length} {t('clash.diagOk')}
            </span>
          )}
        </div>
        {diag && (
          <div className="clash-diag">
            {diag.map((it) => (
              <div key={it.key} className={`clash-diag-item ${it.ok ? 'ok' : it.ok === null ? 'na' : 'bad'}`}>
                <span className="clash-diag-mark">{it.ok ? '✓' : it.ok === null ? '–' : '✗'}</span>
                <span className="clash-diag-label">{it.label}</span>
                <span className="clash-diag-detail">{it.detail}</span>
              </div>
            ))}
          </div>
        )}
        {applyLog && (
          <div className="clash-apply-log">
            {applyLog.error && <div className="clash-err">{applyLog.error}</div>}
            {applyLog.warning && <div className="clash-svc-warn">{applyLog.warning}</div>}
            {applyLog.route && (
              <div className="clash-apply-part">
                <b>{t('clash.routePart', { ok: okCount, total: applyLog.route.results?.length || 0 })}</b>
                <div className="clash-apply-results">
                  {(applyLog.route.results || []).map((r) => (
                    <span key={r.ip} className={`clash-ip-check ${r.ok ? 'ok' : 'bad'}`}>
                      {r.ip} {r.ok ? t('clash.added') : t('clash.failed')}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {applyLog.clash && (
              <div className="clash-apply-part">
                <b>{t('clash.clashPart')}</b>
                <span className={applyLog.clash.error ? 'clash-err' : ''}>
                  {applyLog.clash.error ||
                    (applyLog.clash.skipped
                      ? applyLog.clash.skipped
                      : applyLog.clash.updated
                      ? t('clash.clashUpdated', { n: applyLog.clash.added ?? 0, backup: applyLog.clash.backup || '' })
                      : t('clash.clashUnchanged'))}
                </span>
              </div>
            )}
            {!applyLog.route && !applyLog.clash && !applyLog.error && (
              <div className="clash-hint">{t('clash.doneNoOutput')}</div>
            )}
            {applyLog.hint && <div className="clash-hint">{applyLog.hint}</div>}
          </div>
        )}
        <div className="clash-hint">
          ⚠️ {t('clash.applyHint')}
        </div>
      </div>

      {/* ===== 生成配置 ===== */}
      <div className="card-soft clash-card">
        <div className="panel-header">
          <h2 className="section-title">{t('clash.genTitle')}</h2>
          <div className="clash-apply-btns">
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => setGenOpen((v) => !v)}
              title={genOpen ? t('k8s.snapshot.collapseCfg') : t('k8s.snapshot.expandCfg')}
            >
              {genOpen ? '▾ ' : '▸ '}
              {genOpen ? t('k8s.snapshot.collapseCfg') : t('k8s.snapshot.expandCfg')}
            </button>
            <button className="btn btn-primary" onClick={doGenerate}>
              ⚙ {t('clash.generate')}
            </button>
          </div>
        </div>
        {genOpen && (
          <>
            {genErr && <div className="clash-err">{genErr}</div>}
            {!gen && <div className="empty-hint">{t('clash.genHint')}</div>}
            {gen && (
              <div className="clash-gen">
            <div className="clash-gen-block">
              <div className="clash-gen-head">
                <b>{t('clash.step1')}</b>
                <button className="btn btn-sm btn-ghost" onClick={() => copy('clash_rules')}>
                  {copied === 'clash_rules' ? t('common.copied') : `📋 ${t('common.copy')}`}
                </button>
              </div>
              <pre className="clash-code">{gen.clash_rules}</pre>
            </div>
            <div className="clash-gen-block">
              <div className="clash-gen-head">
                <b>{t('clash.step2')}</b>
                <button className="btn btn-sm btn-ghost" onClick={() => copy('route_add')}>
                  {copied === 'route_add' ? t('common.copied') : `📋 ${t('common.copy')}`}
                </button>
              </div>
              <pre className="clash-code">{gen.route_add}</pre>
            </div>
            {gen.route_del && (
              <div className="clash-gen-block">
                <div className="clash-gen-head">
                  <b>{t('clash.step3')}</b>
                  <button className="btn btn-sm btn-ghost" onClick={() => copy('route_del')}>
                    {copied === 'route_del' ? t('common.copied') : `📋 ${t('common.copy')}`}
                  </button>
                </div>
                <pre className="clash-code">{gen.route_del}</pre>
              </div>
            )}
            <div className="clash-hint">{gen.hint}</div>
          </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
