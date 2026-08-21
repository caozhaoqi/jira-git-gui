import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost } from '../api/client';

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

const DEFAULT_IPS = ['73.2.3.27', '73.2.192.1', '83.0.16.1'];
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
  const [ifaces, setIfaces] = useState<NetIface[]>([]);
  const [defaultGw, setDefaultGw] = useState('');
  const [loadErr, setLoadErr] = useState('');
  const [lanDevice, setLanDevice] = useState('');
  const [wanDevice, setWanDevice] = useState('');
  const [services, setServices] = useState<Service[]>([]);
  const [ips, setIps] = useState<string[]>(DEFAULT_IPS);
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

  const loadIfaces = useCallback(async () => {
    setLoadErr('');
    try {
      const r = await apiGet<IfacesResp>('/api/clash/interfaces');
      if (!r.ok) {
        setLoadErr(r.error || '检测失败');
        return;
      }
      const list = r.interfaces || [];
      setIfaces(list);
      setDefaultGw(r.default_gateway || '');
      setServices(r.services || []);
      // 局域网口：只选「有 IP 且 active」的有线接口（USB 网卡插上才有 IP）；
      // 没有有 IP 的有线口则留空，让用户插线刷新后选择（避免默认选到无 IP 接口）
      const lan = list.find((x) => x.kind === 'lan' && x.status === 'active' && !!x.ip);
      if (lan) setLanDevice((v) => v || lan.device);
      const gw = r.default_gateway;
      if (gw) setWanDevice((v) => v || gw);
    } catch (e: any) {
      setLoadErr(e.message || '接口检测失败');
    }
  }, []);

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
      // 默认选 ClashX 当前加载的配置（selectConfigName），否则第一个非 base 订阅
      const cur = list.find((x) => x.path.endsWith(r.current_config || '__none__'));
      const pick = cur || list.find((x) => !x.base) || list[0];
      if (pick && !cfgPath) setCfgPath(pick.path);
    } catch {
      setCfgCands([]);
    }
  }, [cfgPath]);

  useEffect(() => {
    loadIfaces();
    loadProxy();
    probeCfgPath();
  }, [loadIfaces, loadProxy, probeCfgPath]);

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
        next[ip] = { ok: false, ip, error: e.message || '检查失败' };
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
        setGenErr(r.error || '生成失败');
        setGen(null);
        return;
      }
      setGen(r);
    } catch (e: any) {
      setGenErr(e.message || '生成失败');
    }
  };

  const copy = async (key: 'clash_rules' | 'route_add' | 'route_del') => {
    const t = gen?.[key];
    if (!t) return;
    const okc = await copyText(t);
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
      setApplyLog({ ok: false, error: e.message || '应用失败' });
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
      setApplyLog({ ok: false, error: e.message || '撤销失败' });
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
      setDiag([{ key: 'err', label: '诊断失败', ok: false, detail: e.message || '未知错误' }]);
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
        setPatchLog(`✗ ${r.error || '写入失败'}`);
      } else {
        setPatchLog(
          `✓ 已写入全部 ${r.total} 个配置：更新 ${r.updated}，已存在 ${r.skipped}${r.failed?.length ? `，失败 ${r.failed.join('、')}` : ''}`
        );
      }
    } catch (e: any) {
      setPatchLog(`✗ ${e.message || '写入失败'}`);
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
          : `✓ 已将「${r.wan_service}」提到服务顺序第一。${r.route || ''}`
      );
    } catch (e: any) {
      setFixMsg(`✗ ${e.message || '修复失败'}`);
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
          <h2 className="section-title">网络接口</h2>
          <button className="btn btn-sm btn-ghost" onClick={() => { loadIfaces(); loadProxy(); }}>
            🔄 重新检测
          </button>
        </div>
        {loadErr && <div className="clash-err">{loadErr}</div>}
        {ifaces.length === 0 && !loadErr && <div className="empty-hint">正在检测网络接口…</div>}
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
                    <span className="clash-tag">默认出口</span>
                  )}
                </div>
                <div className="clash-iface-meta">
                  <span className="mono">{it.ip || '无 IP'}</span>
                  <span className="muted">{active ? '已启用' : '未启用'}</span>
                </div>
                <div className="clash-iface-radio">
                  <label title="指定 IP 应走的局域网接口（有线直连）">
                    <input
                      type="radio"
                      name="lan-device"
                      checked={lanDevice === it.device}
                      onChange={() => setLanDevice(it.device)}
                    />
                    局域网(直连)口
                  </label>
                  <label title="其他流量出口（通常为 WiFi）">
                    <input
                      type="radio"
                      name="wan-device"
                      checked={wanDevice === it.device}
                      onChange={() => setWanDevice(it.device)}
                    />
                    其他流量口
                  </label>
                </div>
              </div>
            );
          })}
        </div>
        <div className="clash-hint">
          💡 选择规则：指定内网 IP →「局域网(直连)口」（如 USB 以太网 en5 / 有线 en0）；
          其余流量 →「其他流量口」（WiFi）。若 Clash 正在运行，其 7890/7891/7897 端口如下：
          {proxyPorts?.map((p) => (
            <span key={p.port} className={`clash-proxy-port ${p.open ? 'open' : ''}`}>
              {p.port}:{p.open ? '在听' : '关闭'}
            </span>
          ))}
        </div>
        {services.length > 0 && (
          <div className="clash-svc">
            <div className="clash-svc-label">网络服务顺序：</div>
            <div className="clash-svc-order">
              {services.map((s, i) => (
                <span
                  key={s.name}
                  className={`clash-svc-item ${s.disabled ? 'off' : ''} ${/ethernet|usb|lan/i.test(s.name) ? 'lan' : 'wifi'}`}
                  title={`设备 ${s.device || '-'}${s.disabled ? '（已禁用）' : ''}`}
                >
                  {i + 1}. {s.name.replace(/ \(.*$/, '')}
                  {s.disabled ? ' · 停用' : ''}
                </span>
              ))}
            </div>
            {needFix && (
              <div className="clash-svc-warn">
                ⚠️ 有线网卡排位在 Wi-Fi 前面：插上网线后 macOS 会把默认路由切到有线网卡，
                <b>外网（微信图片等）会全部走不通</b>（文字消息走代理还能收）。建议一键修复：
                <button className="btn btn-sm btn-primary" onClick={doFixServiceOrder} disabled={fixing}>
                  {fixing ? '修复中…' : '🛠 一键修复（Wi-Fi 优先）'}
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
          <h2 className="section-title">指定 IP（走局域网直连）</h2>
          <button className="btn btn-sm btn-primary" onClick={checkAll} disabled={checking || !ips.length}>
            {checking ? '检查中…' : '🔍 检查连通性'}
          </button>
        </div>
        <div className="clash-ip-input">
          <input
            className="input"
            placeholder="如 73.2.3.27 或 1.2.3.4:8080，回车添加"
            value={newIp}
            onChange={(e) => setNewIp(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addIp()}
          />
          <button className="btn" onClick={addIp} disabled={!newIp.trim()}>
            ＋ 添加
          </button>
          <button className="btn btn-ghost" onClick={() => setIps(DEFAULT_IPS)} title="恢复默认三个内网 IP">
            恢复默认
          </button>
        </div>
        <div className="clash-ip-tags">
          {ips.length === 0 && <span className="muted">暂无 IP，请添加。</span>}
          {ips.map((ip) => {
            const c = checks[ip];
            return (
              <div key={ip} className="clash-ip-tag">
                <span className="clash-ip-name">{ip}</span>
                {c && (
                  <span className={`clash-ip-check ${c.reachable ? 'ok' : 'bad'}`}>
                    {c.reachable ? `HTTP ${c.http_code}` : '不可达'}
                    {c.route_iface ? ` · 经 ${c.route_iface}` : ''}
                  </span>
                )}
                <button className="clash-ip-del" onClick={() => removeIp(ip)} title="移除">
                  ×
                </button>
              </div>
            );
          })}
        </div>
        <div className="clash-hint">
          💡 「检查连通性」会对每个 IP 查询当前系统路由接口并做一次 3 秒 HTTP 探测；
          添加路由后再次检查，若「经 en5/usb 以太网」且 HTTP 200，即分流成功。
        </div>
      </div>

      {/* ===== 一键应用（自动执行） ===== */}
      <div className="card-soft clash-card">
        <div className="panel-header">
          <h2 className="section-title">一键应用（自动添加）</h2>
          <div className="clash-apply-btns">
            <button
              className="btn btn-primary"
              onClick={doApply}
              disabled={applying || !ips.length || !lanDevice}
              title="弹系统授权框后自动执行 route add，并把 DIRECT 规则写入 Clash 配置"
            >
              {applying ? '执行中…' : '🚀 一键应用'}
            </button>
            <button
              className="btn btn-ghost"
              onClick={doRevert}
              disabled={applying || !ips.length}
              title="删除自动添加的路由与 Clash 规则"
            >
              ↩ 一键撤销
            </button>
          </div>
        </div>
        <div className="clash-cfg-path">
          <label>
            Clash 配置文件路径（自动写入 rules，可选）
            <input
              className="input"
              placeholder="如 ~/.config/clash/config.yaml"
              value={cfgPath}
              onChange={(e) => setCfgPath(e.target.value)}
            />
          </label>
          <button className="btn btn-sm btn-ghost" onClick={probeCfgPath} title="扫描常见 Clash 客户端配置位置">
            🔍 探测路径
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
                {c.path.endsWith(cfgCurrent) ? ' · 当前加载' : ''}
                {c.base ? ' · 模板' : ''}
              </button>
            ))}
          </div>
        )}
        <div className="clash-cfg-actions">
          <button className="btn btn-sm" onClick={doPatchAll} disabled={patchingAll || !ips.length} title="把规则写入 ~/.config/clash 下所有配置，无论 ClashX 切换哪个都生效">
            {patchingAll ? '写入中…' : '📦 写入所有订阅配置'}
          </button>
          <button className="btn btn-sm btn-ghost" onClick={probeCfgPath} title="重新扫描配置">
            🔍 重新探测
          </button>
          {patchLog && <span className="clash-patch-msg">{patchLog}</span>}
        </div>
        <div className="clash-hint">
          ⚠️ ClashX 实际加载的是菜单里选中的配置（当前为「{cfgCurrent || '未知'}」），config.yaml 只是基础模板。
          写入后请在 ClashX 菜单栏图标 → 配置 → 重新选择（或切换走再切回）使其重载，否则经 Clash 访问内网 IP 仍会 502。
        </div>
        <div className="clash-diag-row">
          <button className="btn btn-sm" onClick={doDiagnose} disabled={diaging}>
            {diaging ? '诊断中…' : '🩺 全面诊断（流量检测）'}
          </button>
          {diag && (
            <span className="clash-diag-summary">
              {diag.filter((x) => x.ok === true).length}/{diag.filter((x) => x.ok !== null).length} 项正常
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
                <b>路由（{okCount}/{applyLog.route.results?.length || 0} 成功）：</b>
                <div className="clash-apply-results">
                  {(applyLog.route.results || []).map((r) => (
                    <span key={r.ip} className={`clash-ip-check ${r.ok ? 'ok' : 'bad'}`}>
                      {r.ip} {r.ok ? '✓ 已添加' : '✗ 失败'}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {applyLog.clash && (
              <div className="clash-apply-part">
                <b>Clash 配置：</b>
                <span className={applyLog.clash.error ? 'clash-err' : ''}>
                  {applyLog.clash.error ||
                    (applyLog.clash.skipped
                      ? applyLog.clash.skipped
                      : applyLog.clash.updated
                      ? `已写入 ${applyLog.clash.added} 条规则${applyLog.clash.backup ? `（备份：${applyLog.clash.backup}）` : ''}`
                      : '未修改')}
                </span>
              </div>
            )}
            {!applyLog.route && !applyLog.clash && !applyLog.error && (
              <div className="clash-hint">执行完成，无输出。</div>
            )}
            {applyLog.hint && <div className="clash-hint">{applyLog.hint}</div>}
          </div>
        )}
        <div className="clash-hint">
          ⚠️ 点击「一键应用」会弹出 macOS 系统授权框（输入开机密码一次），自动执行
          <code>route add</code>；Clash 规则直接写入配置文件（先自动备份）。若弹窗失败（如无图形会话），
          请改用上方「生成配置」手动执行。
        </div>
      </div>

      {/* ===== 生成配置 ===== */}
      <div className="card-soft clash-card">
        <div className="panel-header">
          <h2 className="section-title">生成配置</h2>
          <button className="btn btn-primary" onClick={doGenerate}>
            ⚙ 生成 Clash 规则 + 路由命令
          </button>
        </div>
        {genErr && <div className="clash-err">{genErr}</div>}
        {!gen && <div className="empty-hint">选择接口与 IP 后，点击「生成」。</div>}
        {gen && (
          <div className="clash-gen">
            <div className="clash-gen-block">
              <div className="clash-gen-head">
                <b>① Clash 规则（粘贴到配置 rules: 顶部）</b>
                <button className="btn btn-sm btn-ghost" onClick={() => copy('clash_rules')}>
                  {copied === 'clash_rules' ? '✓ 已复制' : '📋 复制'}
                </button>
              </div>
              <pre className="clash-code">{gen.clash_rules}</pre>
            </div>
            <div className="clash-gen-block">
              <div className="clash-gen-head">
                <b>② macOS 路由命令（终端执行，需 sudo）</b>
                <button className="btn btn-sm btn-ghost" onClick={() => copy('route_add')}>
                  {copied === 'route_add' ? '✓ 已复制' : '📋 复制'}
                </button>
              </div>
              <pre className="clash-code">{gen.route_add}</pre>
            </div>
            {gen.route_del && (
              <div className="clash-gen-block">
                <div className="clash-gen-head">
                  <b>③ 撤销命令</b>
                  <button className="btn btn-sm btn-ghost" onClick={() => copy('route_del')}>
                    {copied === 'route_del' ? '✓ 已复制' : '📋 复制'}
                  </button>
                </div>
                <pre className="clash-code">{gen.route_del}</pre>
              </div>
            )}
            <div className="clash-hint">{gen.hint}</div>
          </div>
        )}
      </div>
    </div>
  );
}
