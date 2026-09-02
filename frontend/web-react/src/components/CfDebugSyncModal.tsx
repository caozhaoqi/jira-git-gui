// 云函数调试：配置同步弹窗（cf_accounts 导入 / 导出 / 复制）。
// 复用 cfdebug 的 listAccounts / copyAccount / exportAccounts / importAccounts。
import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { useT } from '../i18n';
import { cfdebug } from '../api/cfdebug/client';

export function CfDebugSyncModal({
  open,
  onClose,
  onChanged,
}: {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useT();
  const addToast = useAppStore((s) => s.addToast);
  const [items, setItems] = useState<Array<{ index: number; name: string; server_url: string; type: string }>>([]);
  const [importText, setImportText] = useState('');
  const [importMode, setImportMode] = useState<'merge' | 'replace'>('merge');

  const refresh = async () => {
    try {
      const r = await cfdebug.listAccounts();
      setItems(r.items || []);
    } catch (e: any) {
      addToast(t('cfdebug.accountsLoadFail', { msg: e.message }), 'error');
    }
  };

  useEffect(() => {
    if (open) void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const doCopy = async (index: number) => {
    try {
      await cfdebug.copyAccount(index);
      addToast(t('cfdebug.syncCopied'), 'info');
      void refresh();
      onChanged();
    } catch (e: any) {
      addToast(t('cfdebug.syncFail', { msg: e.message }), 'error');
    }
  };

  const doExport = async () => {
    try {
      const r = await cfdebug.exportAccounts();
      const blob = new Blob([JSON.stringify(r.accounts, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'cf_accounts.json';
      a.click();
      URL.revokeObjectURL(url);
      addToast(t('cfdebug.syncExported', { n: r.count }), 'info');
    } catch (e: any) {
      addToast(t('cfdebug.syncFail', { msg: e.message }), 'error');
    }
  };

  const doImport = async () => {
    let arr: unknown;
    try {
      arr = JSON.parse(importText);
      if (!Array.isArray(arr)) arr = [arr];
    } catch {
      addToast(t('cfdebug.syncJsonErr'), 'error');
      return;
    }
    try {
      const r = await cfdebug.importAccounts(arr as Record<string, unknown>[], importMode);
      addToast(t('cfdebug.syncImported', { n: r.count || 0 }), 'info');
      setImportText('');
      void refresh();
      onChanged();
    } catch (e: any) {
      addToast(t('cfdebug.syncFail', { msg: e.message }), 'error');
    }
  };

  if (!open) return null;

  return (
    <div className="cfd-modal-mask" onClick={onClose}>
      <div className="cfd-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cfd-modal-head">
          <span className="section-title">{t('cfdebug.syncTitle')}</span>
          <div className="spacer" />
          <button className="btn btn-sm btn-ghost" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="cfd-modal-body">
          <div className="cfd-sync-actions">
            <button className="btn btn-sm" onClick={() => void doExport()}>
              ⬇ {t('cfdebug.syncExport')}
            </button>
            <span className="cfdebug-hint">{t('cfdebug.syncExportHint')}</span>
          </div>

          <div className="cfd-account-list">
            {items.length === 0 ? (
              <div className="empty-hint">{t('cfdebug.noAccount')}</div>
            ) : (
              items.map((a) => (
                <div className="cfd-account-row" key={a.index}>
                  <span className="cfd-account-name">{a.name}</span>
                  <span className="cfd-account-url" title={a.server_url}>
                    {a.server_url}
                  </span>
                  <span className="cfd-account-type">{a.type}</span>
                  <button className="btn btn-xs" onClick={() => void doCopy(a.index)}>
                    {t('cfdebug.syncCopy')}
                  </button>
                </div>
              ))
            )}
          </div>

          <div className="cfd-sync-import">
            <div className="cfd-sync-import-head">
              <span>{t('cfdebug.syncImport')}</span>
              <select
                className="sel"
                value={importMode}
                onChange={(e) => setImportMode(e.target.value as 'merge' | 'replace')}
              >
                <option value="merge">{t('cfdebug.syncMerge')}</option>
                <option value="replace">{t('cfdebug.syncReplace')}</option>
              </select>
            </div>
            <textarea
              className="cfdebug-kwargs"
              spellCheck={false}
              value={importText}
              placeholder={t('cfdebug.syncImportHint')}
              onChange={(e) => setImportText(e.target.value)}
            />
            <button className="btn btn-sm btn-primary" onClick={() => void doImport()}>
              ⬆ {t('cfdebug.syncImportBtn')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
