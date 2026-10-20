/** K8s 多环境展示的共享纯函数：类型徽标 class / 悬浮文案 / i18n key。
 *  原在 K8sPanel.tsx 内部，快照等子 tab 也要用，提取至此避免复制。 */

export function envTagClass(name: string): string {
  const v = (name || '').toLowerCase();
  if (v.includes('prod') || v === 'prd' || v.includes('生产')) return 'k8s-env-tag env-prod';
  if (v.includes('test') || v.includes('uat') || v.includes('stag') || v.includes('测试') || v.includes('预发')) return 'k8s-env-tag env-test';
  if (v.includes('dev') || v.includes('开发') || v.includes('development')) return 'k8s-env-tag env-dev';
  return 'k8s-env-tag env-other';
}

export function envTagText(envs: { name: string; label?: string }[], cur: string): string {
  const e = envs.find((x) => x.name === cur);
  return e ? `${e.label || e.name} (${e.name})` : cur || '—';
}

/** 环境「类型」标签：下拉框已经显示了环境名，这里只补充下拉框表达不了的风险等级
    （生产/测试/开发），避免同一行里把同一个环境名显示两遍。 */
export function envKindKey(name: string): string {
  const v = (name || '').toLowerCase();
  if (v.includes('prod') || v === 'prd' || v.includes('生产')) return 'k8s.envKind.prod';
  if (v.includes('test') || v.includes('uat') || v.includes('stag') || v.includes('测试') || v.includes('预发')) return 'k8s.envKind.test';
  if (v.includes('dev') || v.includes('开发') || v.includes('development')) return 'k8s.envKind.dev';
  return 'k8s.envKind.other';
}

/** 当前环境是否「连不上」：既没配 kubeconfig 也没配 SSH 跳板，
 *  后端 kubectl 只能连 localhost，必然失败。 */
export function envUnreachable(env: { kubeconfig?: string; ssh_host?: string } | undefined | null): boolean {
  if (!env) return true;
  return !env.kubeconfig && !env.ssh_host;
}
