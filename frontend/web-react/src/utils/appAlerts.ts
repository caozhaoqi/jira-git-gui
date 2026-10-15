// 全局状态告警的**统一构造器**。
//
// 为什么集中在这里：同一类问题（Jira 会话失效 / Cookie 未配置 / K8s 无环境 /
// Kibana 无站点）会在多个入口被检出——启动拉仓库、手动刷新、切页签、切窗口焦点、
// 连接设置保存、文件树/预览请求失败…… 如果每个调用点各自拼一遍横幅文案与按钮，
// 很容易漏掉某一个入口，出现「A 入口会弹提醒、B 入口不弹」的不一致。
// 因此：**id、标题、按钮一律在这里定义一次**，调用点只负责「报告条件」。
//
// ⚠️ 文案存 i18n key 而非译文：横幅在渲染时才 t()，切换语言即时生效。
import { useAppStore } from '../store/useAppStore';
import type { ReposResp } from '../api/types';

/** Jira 远端会话失效（跳转登录页 / 401 / 403）——文件树、预览、差异对比都会跟着坏。 */
export const ALERT_JIRA_AUTH = 'jira-auth';
/** Jira Cookie 压根没配置（不是过期，是缺配置）。 */
export const ALERT_JIRA_CONFIG = 'jira-config';
/** K8s 一个环境都没配置（面板只能显示「无可用环境」）。 */
export const ALERT_K8S_ENV = 'k8s-env';
/** Kibana 站点列表拉不到 / 一个站点都没配。 */
export const ALERT_KIBANA_SITE = 'kibana-site';

/**
 * 远端会话失效告警：常驻直到用户处理，不自动消失。
 * 后端可能重复广播（SSE + 各接口回包兜底），setAlert 幂等覆盖。
 */
export function raiseJiraAuthAlert(detail?: string): void {
  const { setAlert, clearAlert, openConnect, setTab } = useAppStore.getState();
  setAlert({
    id: ALERT_JIRA_AUTH,
    titleKey: 'cookie.expiredTitle',
    detailKey: 'cookie.expiredDetail',
    detail: (detail || '').trim() || undefined,
    actions: [
      {
        labelKey: 'cookie.reLogin',
        primary: true,
        onClick: () => { openConnect(); clearAlert(ALERT_JIRA_AUTH); },
      },
      {
        labelKey: 'cookie.cloneLocal',
        onClick: () => { setTab('repo'); clearAlert(ALERT_JIRA_AUTH); },
      },
      { labelKey: 'cookie.dismiss', onClick: () => clearAlert(ALERT_JIRA_AUTH) },
    ],
  });
}

export function clearJiraAuthAlert(): void {
  useAppStore.getState().clearAlert(ALERT_JIRA_AUTH);
}

/**
 * 把 ``/api/repos`` 回包里的「配置缺失 / 会话失效」信号映射成告警。
 * **所有拉仓库列表的入口都应调它**（启动、重试、手动刷新、切到差异页），
 * 否则就会出现「A 入口弹提醒、B 入口不弹」的不一致。
 *
 * ⚠️ 这里**不**在成功时清除 jira-auth 提醒：``/api/repos`` 有 10 分钟缓存，
 * 命中缓存时并没有真正联网，「成功」并不能证明 Cookie 已恢复；拿它清提醒会把
 * 仍然失效的会话误报成正常。清除只由用户动作触发（重贴 Cookie / 忽略）。
 */
export function applyRepoAlerts(
  r: Pick<ReposResp, 'config_missing' | 'auth_failed' | 'auth_detail'>,
): void {
  setJiraConfigAlert(r.config_missing === 'cookie');
  if (r.auth_failed) raiseJiraAuthAlert(r.auth_detail);
}

/** 报告「Jira Cookie 是否缺失」。false 表示已配置（清掉提醒）。 */
export function setJiraConfigAlert(missing: boolean): void {
  const { setAlert, clearAlert, openConnect } = useAppStore.getState();
  if (!missing) {
    clearAlert(ALERT_JIRA_CONFIG);
    return;
  }
  setAlert({
    id: ALERT_JIRA_CONFIG,
    titleKey: 'cookie.missingTitle',
    detailKey: 'cookie.missingDetail',
    actions: [
      {
        labelKey: 'app.connectSettings',
        primary: true,
        onClick: () => { openConnect(); clearAlert(ALERT_JIRA_CONFIG); },
      },
      { labelKey: 'cookie.dismiss', onClick: () => clearAlert(ALERT_JIRA_CONFIG) },
    ],
  });
}

/** K8s 无可用环境：提醒 + 直达「管理环境」。onManage 由 K8s 面板提供（弹窗是它的局部状态）。 */
export function raiseK8sEnvAlert(detail: string | undefined, onManage: () => void): void {
  const { setAlert, clearAlert } = useAppStore.getState();
  setAlert({
    id: ALERT_K8S_ENV,
    titleKey: 'k8s.envMissingTitle',
    detailKey: 'k8s.envMissingDetail',
    detail: (detail || '').trim() || undefined,
    actions: [
      {
        labelKey: 'k8s.manageEnv',
        primary: true,
        onClick: () => { onManage(); clearAlert(ALERT_K8S_ENV); },
      },
      { labelKey: 'cookie.dismiss', onClick: () => clearAlert(ALERT_K8S_ENV) },
    ],
  });
}

export function clearK8sEnvAlert(): void {
  useAppStore.getState().clearAlert(ALERT_K8S_ENV);
}

/** Kibana 无站点 / 站点列表加载失败。onManage 由 Kibana 面板提供（打开站点管理窗口）。 */
export function raiseKibanaSiteAlert(detail: string | undefined, onManage: () => void): void {
  const { setAlert, clearAlert } = useAppStore.getState();
  setAlert({
    id: ALERT_KIBANA_SITE,
    titleKey: 'kibana.siteMissingTitle',
    detailKey: 'kibana.siteMissingDetail',
    detail: (detail || '').trim() || undefined,
    actions: [
      {
        labelKey: 'kibana.manageSites',
        primary: true,
        onClick: () => { onManage(); clearAlert(ALERT_KIBANA_SITE); },
      },
      { labelKey: 'cookie.dismiss', onClick: () => clearAlert(ALERT_KIBANA_SITE) },
    ],
  });
}

export function clearKibanaSiteAlert(): void {
  useAppStore.getState().clearAlert(ALERT_KIBANA_SITE);
}
