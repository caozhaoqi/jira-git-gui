import { useEffect, useMemo, useState } from 'react';
import { apiGet } from '../api/client';
import { useT } from '../i18n';

interface ApiDocEndpoint {
  method: string;
  methods: string[];
  path: string;
  summary: string;
  params: string[];
}

interface ApiDocGroup {
  key: string;
  title: string;
  description: string;
  count: number;
  endpoints: ApiDocEndpoint[];
}

interface ApiDoc {
  title: string;
  version: string;
  base_url: string;
  generated_at: string;
  total: number;
  groups: ApiDocGroup[];
}

const METHOD_COLORS: Record<string, string> = {
  GET: 'var(--success)',
  POST: 'var(--primary)',
  DELETE: 'var(--danger)',
  PUT: 'var(--warn)',
};

export function SettingsPanel() {
  const { t } = useT();
  const [doc, setDoc] = useState<ApiDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [copiedPath, setCopiedPath] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    apiGet<ApiDoc>('/api/docs')
      .then((d) => {
        if (alive) {
          setDoc(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (alive) setError(e?.message || String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const filtered = useMemo(() => {
    if (!doc) return [];
    const q = query.trim().toLowerCase();
    if (!q) return doc.groups;
    return doc.groups
      .map((g) => ({
        ...g,
        endpoints: g.endpoints.filter(
          (e) =>
            e.path.toLowerCase().includes(q) ||
            e.summary.toLowerCase().includes(q) ||
            e.params.some((p) => p.toLowerCase().includes(q))
        ),
      }))
      .filter((g) => g.endpoints.length > 0);
  }, [doc, query]);

  const copyCurl = async (e: ApiDocEndpoint) => {
    const cmd = `curl "${location.origin}${e.path}"${e.method !== 'GET' ? ` -X ${e.method}` : ''}`;
    try {
      await navigator.clipboard.writeText(cmd);
      setCopiedPath(e.path);
      setTimeout(() => setCopiedPath((p) => (p === e.path ? null : p)), 1500);
    } catch {
      /* 忽略剪贴板失败 */
      console.error('Failed to copy to clipboard');
    }
  };

  return (
    <div className="settings-panel">
      <header className="settings-head">
        <div>
          <h2>{t('settings.title')}</h2>
          <p className="settings-sub">{t('settings.apiDocsDesc')}</p>
        </div>
      </header>

      <section className="settings-section">
        <div className="settings-section-head">
          <h3>📡 {t('settings.apiDocs')}</h3>
          {doc && (
            <span className="settings-meta">
              {t('settings.total', { n: doc.total })} ·{' '}
              {t('settings.groupCount', { n: doc.groups.length })} ·{' '}
              {t('settings.generatedAt', { t: doc.generated_at })}
            </span>
          )}
        </div>

        {loading && <div className="settings-loading">{t('settings.loading')}</div>}
        {error && <div className="settings-error">⚠ {error}</div>}

        {doc && !loading && (
          <>
            <input
              className="settings-search"
              placeholder={t('settings.searchPlaceholder')}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />

            {filtered.length === 0 && (
              <div className="settings-empty">{t('settings.noResult')}</div>
            )}

            <div className="apidoc-groups">
              {filtered.map((g) => (
                <div key={g.key} className="apidoc-group">
                  <div className="apidoc-group-head">
                    <span className="apidoc-group-title">{g.title}</span>
                    <span className="apidoc-group-desc">{g.description}</span>
                    <span className="apidoc-group-count">{g.endpoints.length}</span>
                  </div>
                  <table className="apidoc-table">
                    <tbody>
                      {g.endpoints.map((e) => (
                        <tr key={e.path}>
                          <td className="apidoc-method">
                            <span
                              className="apidoc-badge"
                              style={{
                                color: METHOD_COLORS[e.method] || 'var(--primary)',
                                borderColor: METHOD_COLORS[e.method] || 'var(--primary)',
                              }}
                            >
                              {e.method}
                            </span>
                          </td>
                          <td className="apidoc-path">
                            <code>{e.path}</code>
                            <div className="apidoc-summary">{e.summary}</div>
                            {e.params.length > 0 && (
                              <div className="apidoc-params">
                                <span className="apidoc-params-label">
                                  {t('settings.params')}:
                                </span>{' '}
                                {e.params.map((p) => (
                                  <code key={p} className="apidoc-param">
                                    {p}
                                  </code>
                                ))}
                              </div>
                            )}
                          </td>
                          <td className="apidoc-actions">
                            <button
                              className="apidoc-btn"
                              onClick={() => copyCurl(e)}
                              title={t('settings.copyCurl')}
                            >
                              {copiedPath === e.path
                                ? t('settings.curlCopied')
                                : t('settings.copyCurl')}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </div>

            <div className="apidoc-footer">
              <a
                className="apidoc-raw"
                href={`${location.origin}/api/docs`}
                target="_blank"
                rel="noreferrer"
              >
                {t('settings.openDocs')}
              </a>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
