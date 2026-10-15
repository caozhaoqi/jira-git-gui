// 「凭据失效 / 配置缺失」类问题必须对用户可见 —— 静态接线守卫。
//
// 背景：这类问题此前往往只写一行日志（或被 catch 静默吞掉），用户看到的是
// 「请选择仓库 / 无可用环境 / 无站点」这种空状态，完全不知道出了什么事。
// 本测试锁定：① 后端把「会话失效」结构化返回；② 前端所有入口都映射到同一个
// 全局告警通道；③ 告警是带 role=alert 的可操作横幅；④ 文案三语齐全。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const app = source('frontend/web-react/src/App.tsx');
const store = source('frontend/web-react/src/store/useAppStore.ts');
const helpers = source('frontend/web-react/src/utils/appAlerts.ts');
const types = source('frontend/web-react/src/api/types.ts');
const routes = source('api/routes_repos.py');

const producer = (f) => source(`frontend/web-react/src/components/${f}`);

// ---------------------------------------------------------------- 后端：结构化标记
assert.match(routes, /"config_missing":\s*"cookie"/,
  'repos 路由要标记「未配置 Cookie」');
assert.match(routes, /_AUTH_ERR_MARKERS\s*=\s*\(/,
  '要有统一的会话失效特征词表');
assert.match(routes, /def _auth_payload\(/,
  '要有统一的会话失效标记构造函数');
assert.match(routes, /"auth_failed":\s*True/,
  '会话失效要回 auth_failed');
// 浏览类接口都要带上标记：否则只有 SSE 送达时才有横幅
for (const [label, pattern] of [
  ['文件树 /api/tree', /"error": tree_err or None,[\s\S]{0,200}?\*\*_auth_payload\(tree_err or ""\)/],
  ['路径直达 /api/tree/resolve', /"elapsed_ms":[\s\S]{0,120}?\*\*_auth_payload\(error or ""\)/],
  ['文件内容 /api/file', /"error": err, \*\*_auth_payload\(err\)/],
  ['提交列表 /api/commits', /"error": str\(ex\), \*\*_auth_payload\(str\(ex\)\)/],
  ['历史版本 /api/file-at-commit', /"error": err, \*\*_auth_payload\(err\)/],
]) {
  assert.match(routes, pattern, `${label} 要带会话失效标记`);
}
// 普通错误不得误报成会话失效（否则会把用户引去重贴 Cookie）
assert.match(routes, /if any\(m in e for m in _AUTH_ERR_MARKERS\)/,
  '只按特征词判定，不能一律当作会话失效');

// ---------------------------------------------------------------- 前端：类型
assert.match(types, /interface AuthFailFields\s*\{[\s\S]*?auth_failed\?: boolean/,
  '要有统一的 auth_failed 字段类型');
for (const iface of ['TreeResp', 'TreeResolveResp', 'FileResp', 'FileAtCommitResp', 'DiffCommitsResp']) {
  assert.match(types, new RegExp(`interface ${iface} extends AuthFailFields`),
    `${iface} 要继承会话失效字段`);
}

// ---------------------------------------------------------------- store：通用告警通道
assert.match(store, /export interface AppAlert\s*\{[\s\S]*?id: string;[\s\S]*?titleKey: string/,
  'store 要有通用告警类型（存 i18n key 而不是译文）');
assert.match(store, /setAlert: \(a: AppAlert\) => void/,
  'store 要暴露 setAlert');
assert.match(store, /clearAlert: \(id: string\) => void/,
  'store 要暴露 clearAlert');
assert.match(store, /alerts: \[a, \.\.\.rest\]/,
  '同 id 重复上报应覆盖而不是堆叠');
// 旧的专用字段应已收敛掉（否则又回到「每类问题各写一段横幅」）
assert.ok(!/cookieExpired:/.test(store), '旧的 cookieExpired 字段应已并入告警通道');
assert.ok(!/jiraCookieMissing:/.test(store), '旧的 jiraCookieMissing 字段应已并入告警通道');
// 连接设置弹窗开关要能被横幅按钮打开
assert.match(store, /openConnect: \(\) => void/, 'store 要暴露 openConnect');

// ---------------------------------------------------------------- 告警构造器：一处定义
for (const id of ['jira-auth', 'jira-config', 'k8s-env', 'kibana-site']) {
  assert.match(helpers, new RegExp(`'${id}'`), `告警 id ${id} 要在 appAlerts 里定义`);
}
assert.match(helpers, /export function applyRepoAlerts\(/,
  '要有 /api/repos 信号的统一映射函数');
assert.match(helpers, /setJiraConfigAlert\(r\.config_missing === 'cookie'\)/,
  'applyRepoAlerts 要映射配置缺失');
assert.match(helpers, /if \(r\.auth_failed\) raiseJiraAuthAlert\(r\.auth_detail\)/,
  'applyRepoAlerts 要映射会话失效');

// ---------------------------------------------------------------- 各入口都要上报
assert.ok((app.match(/applyRepoAlerts\(r\)/g) || []).length >= 2,
  '启动与重试都要映射仓库信号');
assert.match(producer('RepoList.tsx'), /applyRepoAlerts\(res\)/,
  '手动刷新仓库要映射信号');
assert.match(producer('DiffPanel.tsx'), /applyRepoAlerts\(res\)/,
  '差异页拉仓库要映射信号');
assert.match(producer('FileTree.tsx'), /raiseJiraAuthAlert\(res\.auth_detail\)/,
  '文件树会话失效要弹横幅');
assert.match(producer('Preview.tsx'), /raiseJiraAuthAlert\(res\.auth_detail\)/,
  '预览会话失效要弹横幅');
assert.match(producer('ConnectModal.tsx'), /raiseJiraAuthAlert\(res\.cookieWarning\)/,
  'Cookie 校验失败要立刻弹横幅');
assert.match(producer('ConnectModal.tsx'), /setJiraConfigAlert\(false\)/,
  '保存配置要清掉「未配置」提醒');
// Kibana：ok:false 必须被识别（此前被静默吞成「无站点」）
assert.match(producer('kibana/KibanaPanel.tsx'), /if \(d\.ok === false\)[\s\S]{0,400}?raiseKibanaSiteAlert\(/,
  'Kibana 站点加载失败要弹横幅');
assert.match(producer('kibana/KibanaPanel.tsx'), /list\.length === 0[\s\S]{0,200}?raiseKibanaSiteAlert\(/,
  'Kibana 无站点也要给提醒');
assert.match(producer('k8s/K8sPanel.tsx'), /list\.length === 0[\s\S]{0,200}?raiseK8sEnvAlert\(/,
  'K8s 无环境要弹提醒');
assert.match(producer('UnifiedDiagnosisPanel.tsx'), /raiseK8sEnvAlert\(/,
  '诊断面板同样依赖 K8s 环境，缺环境也要提醒（不能只写日志）');

// ---------------------------------------------------------------- 渲染：可操作横幅
assert.match(app, /alerts\.map\(\(a\) => \([\s\S]{0,400}?role="alert"/,
  '告警要渲染成无障碍横幅');
assert.match(app, /a\.actions\.map\(/, '横幅要渲染操作按钮');
assert.match(app, /t\(a\.titleKey\)/, '标题要在渲染时翻译（切语言即时生效）');
assert.match(app, /apiDown && \([\s\S]{0,200}?role="alert"/,
  '本地后端不可达横幅要保持原样');
assert.match(app, /networkWarning && \(/, '上游网络告警要保持原样');

// ---------------------------------------------------------------- i18n：三语齐全
for (const locale of ['zh', 'en', 'ja']) {
  const dict = source(`frontend/web-react/src/i18n/${locale}.ts`);
  for (const key of ['envMissingTitle', 'envMissingDetail']) {
    assert.match(dict, new RegExp(`${key}:`), `${locale} 需要 k8s.${key}`);
  }
  for (const key of ['siteMissingTitle', 'siteMissingDetail']) {
    assert.match(dict, new RegExp(`${key}:`), `${locale} 需要 kibana.${key}`);
  }
  for (const key of ['missingTitle', 'missingDetail', 'expiredTitle', 'expiredDetail']) {
    assert.match(dict, new RegExp(`${key}:`), `${locale} 需要 cookie.${key}`);
  }
}

console.log('Repository/config/auth reminder wiring: passed');
