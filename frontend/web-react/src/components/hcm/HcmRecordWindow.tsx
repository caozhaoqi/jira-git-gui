import { useEffect, useMemo, useState } from 'react';
import { useT } from '../../i18n';
import { useStandaloneTheme } from '../../utils/standaloneTheme';
import { writeClipboardText } from '../../utils/clipboard';

type Payload = { row?: Record<string, unknown>; model?: string; index?: number };

function readPayload(key: string): Payload | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const d = JSON.parse(raw) as Payload;
    sessionStorage.removeItem(key);
    return d;
  } catch { return null; }
}

export function HcmRecordWindow() {
  useStandaloneTheme();
  const { t } = useT();
  const qs = useMemo(() => new URLSearchParams(window.location.search), []);
  const [payload, setPayload] = useState<Payload | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    const key = qs.get('hcm-record-key') || '';
    const snap = key ? readPayload(key) : null;
    if (!snap) { setMissing(true); return; }
    setPayload(snap);
    document.title = `${snap.model || 'Record'} #${(snap.index ?? 0) + 1} · ${t('hcm.dataRecordWindowTitle')}`;
  }, [qs, t]);

  const json = useMemo(() => (payload?.row ? JSON.stringify(payload.row, null, 2) : ''), [payload]);

  if (missing) {
    return (
      <div className="hcm-dt-window">
        <div className="hcm-dt-empty">
          <div className="hcm-dt-empty-icon" aria-hidden="true">▦</div>
          <div>{t('hcm.dataRecordWindowMissing')}</div>
        </div>
      </div>
    );
  }
  if (!payload) return null;

  return (
    <div className="hcm-dt-window">
      <div className="hcm-dt-window-head">
        <span className="hcm-dt-window-title">
          {payload.model || 'Record'} · #{(payload.index ?? 0) + 1}
        </span>
        <span className="spacer" />
        <button
          type="button"
          className="btn btn-sm hcm-dt-act"
          onClick={() => writeClipboardText(json)}
          disabled={!json}
        >
          {t('hcm.dataCopyTsv')}
        </button>
        <button type="button" className="btn btn-sm hcm-dt-act" onClick={() => window.close()}>
          {t('common.close')}
        </button>
      </div>
      <div className="hcm-dt-window-body" style={{ padding: 12, overflow: 'auto' }}>
        <table className="hcm-dt" style={{ width: '100%', tableLayout: 'auto' }}>
          <tbody>
            {Object.entries(payload.row || {}).map(([k, v]) => (
              <tr key={k}>
                <th style={{
                  textAlign: 'left', padding: '8px 12px', fontWeight: 600,
                  color: 'var(--text-dim)', background: 'var(--bg-head)',
                  border: '1px solid var(--border)', verticalAlign: 'top', width: '30%',
                  whiteSpace: 'nowrap',
                }}>{k}</th>
                <td style={{
                  padding: '8px 12px', border: '1px solid var(--border)',
                  wordBreak: 'break-word', whiteSpace: 'pre-wrap', verticalAlign: 'top',
                }}>
                  {v === null || v === undefined
                    ? <span style={{ color: 'var(--text-faint)' }}>—</span>
                    : typeof v === 'object'
                      ? <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12 }}>{JSON.stringify(v, null, 2)}</pre>
                      : String(v)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
