import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet } from '../api/client';
import type { FileResp } from '../api/types';
import { useT } from '../i18n';

function formatContent(path: string, content: string): { text: string; isJson: boolean } {
  if (!content) return { text: '', isJson: false };
  if (/\.json$/i.test(path) || (content.trim().startsWith('{') && content.trim().endsWith('}'))) {
    try {
      return { text: JSON.stringify(JSON.parse(content), null, 2), isJson: true };
    } catch {
      return { text: content, isJson: false };
    }
  }
  return { text: content, isJson: false };
}

export function Preview() {
  const selectedFilePath = useAppStore((s) => s.selectedFilePath);
  const [content, setContent] = useState('');
  const [title, setTitle] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [maximized, setMaximized] = useState(false);
  const pushLog = useAppStore((s) => s.pushLog);
  const treeLocalDir = useAppStore((s) => s.treeLocalDir);
  const { t } = useT();

  useEffect(() => {
    if (!selectedFilePath) {
      setTitle(t('repo.preview'));
      setContent('');
      setError('');
      return;
    }
    let cancelled = false;
    setLoading(true);
    setTitle(t('file.loadingFile') + selectedFilePath);
    setError('');
    const ld = treeLocalDir.trim();
    const url = ld
      ? `/api/file?path=${encodeURIComponent(selectedFilePath)}&local_dir=${encodeURIComponent(ld)}`
      : `/api/file?path=${encodeURIComponent(selectedFilePath)}`;
    apiGet<FileResp>(url)
      .then((res) => {
        if (cancelled) return;
        if (res.error) {
          setTitle(t('repo.preview'));
          setContent(res.error);
        } else {
          const { text, isJson } = formatContent(selectedFilePath, res.content || '');
          setTitle(
            `${t('repo.preview')} · ${selectedFilePath}${isJson ? t('file.jsonFmt') : ''}`
          );
          setContent(text);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setTitle(t('repo.preview'));
        setContent(e.message || t('file.loadErr'));
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selectedFilePath, t]);

  async function copyPath() {
    if (!selectedFilePath) return;
    try {
      await navigator.clipboard.writeText(selectedFilePath);
      pushLog(t('file.copyPath', { path: selectedFilePath }));
    } catch {
      pushLog(t('file.copyFail'), 'error');
    }
  }

  return (
    <div className={`preview-pane ${maximized ? 'maximized repo-col-fullscreen' : ''}`}>
      <div className="panel-header">
        <h2 className="section-title" title={title}>
          {title}
        </h2>
        <div className="panel-header-actions">
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => setMaximized((v) => !v)}
            title={t('common.more')}
          >
            ⛶
          </button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={copyPath}
            disabled={!selectedFilePath}
            title={t('file.copyPath', { path: selectedFilePath || '' })}
          >
            📋
          </button>
        </div>
      </div>
      <pre className="code-block preview-content">
        {loading
          ? t('common.loading')
          : error
            ? error
            : content || t('repo.noPreview')}
      </pre>
    </div>
  );
}
