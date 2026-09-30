import { useCallback, useEffect, useMemo, useState } from 'react';
import { useT } from '../../i18n';
import { KibanaFilters, openSiteManagerWindow, useKibanaSites } from '../kibana/KibanaFilters';
import { KibanaExplorer } from '../kibana/KibanaExplorer';
import { DEFAULT_QUERY, type KibanaQuery } from '../kibana/context';

/**
 * K8s 面板里的「系统日志汇总」tab。
 *
 * 思路：K8s 容器日志（pod/namespace/container）已被采集进「系统日志」（Kibana/ES，字段
 * kubernetes.pod_name/container_name/namespace_name），无需新后端即可复用既有检索/聚合能力：
 *   - useKibanaSites 选日志服务器 / 站点（对应目标集群的 ES 索引）
 *   - 「管理站点」打开独立全屏窗口（/?view=kibana-sites）增删改站点（可配置多台服务器的 Kibana 地址）
 *   - KibanaFilters 提供 namespace/container/pod/keyword/级别 等筛选
 *   - KibanaExplorer 左侧按 pod->container->namespace 聚合（带错误红标），右侧 KibanaLogStream 展示日志
 *
 * 这是「历史/汇总」视角，与 K8s 面板其它 tab 的实时 kubectl 日志互补（ES 有采集延迟，非实时 tail）。
 */
export function K8sSysLog() {
  const { t } = useT();
  // includeServers=true：下拉里除手配 Kibana 站点外，还列出 cf_accounts / hcm_whitelist 配置的全部服务器
  const { sites, current, setCurrent, reload } = useKibanaSites(true);
  const [query, setQuery] = useState<KibanaQuery>(DEFAULT_QUERY);
  const [wrap, setWrap] = useState(true);
  // 「查询」按钮此前是空实现；这里用计数驱动 KibanaExplorer 重新拉取
  const [reloadKey, setReloadKey] = useState(0);
  // 只读聚合 tab：默认不自动刷新（ES 采集本身有延迟，非实时 tail）
  const refreshSec = 0;

  // 站点在独立管理窗口里增删改，回到本窗口（获得焦点）时自动刷新列表
  useEffect(() => {
    const onFocus = () => { reload(); };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [reload]);

  const onChange = useCallback((patch: Partial<KibanaQuery>) => {
    setQuery((q) => ({ ...q, ...patch }));
  }, []);

  const currentSite = useMemo(
    () => sites.find((s) => s.name === current),
    [sites, current]
  );

  return (
    <div className="k8s-syslog">
      <div className="k8s-envbar card-soft">
        <label className="field-inline">
          {t('k8s.syslog.site')}
          <select className="sel" value={current} onChange={(e) => setCurrent(e.target.value)}>
            {sites.length === 0 && <option value="">{t('kibana.noSite')}</option>}
            {sites.map((s) => (
              <option key={s.name} value={s.name}>
                {(s.label || s.name) + (s.base_url ? `（${s.base_url}）` : '')}
              </option>
            ))}
          </select>
        </label>
        {/* 「管理站点」打开独立全屏管理窗口：可在此新增 / 切换 / 删除多台服务器的 Kibana 地址 */}
        <button className="btn btn-ghost btn-sm"
                onClick={() => openSiteManagerWindow(t('kibana.manageSites'))}>
          {t('kibana.manageSites')}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={reload} title={t('kibana.reloadSites')}>
          ↻
        </button>
        {currentSite?.base_url && <span className="k8s-env-kc panel-sub">{currentSite.base_url}</span>}
        {/* 本页签查的是采集到 ES/Kibana 的历史日志，与上方 kubectl 的「环境」无关：
            两个下拉框并排很容易让人以为「环境」在这里也应该生效，故显式说明。 */}
        <span className="k8s-syslog-hint panel-sub">{t('k8s.syslog.hint')}</span>
      </div>

      <KibanaFilters
        site={current}
        value={query}
        onChange={onChange}
        onSearch={() => setReloadKey((k) => k + 1)}
      />

      {!current ? (
        <div className="empty-hint k8s-syslog-empty">{t('k8s.syslog.pickSite')}</div>
      ) : (
        <KibanaExplorer
          site={current}
          query={query}
          wrap={wrap}
          onToggleWrap={() => setWrap((w) => !w)}
          refreshSec={refreshSec}
          reloadKey={reloadKey}
        />
      )}
    </div>
  );
}
