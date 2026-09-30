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

  // 最大化后必须能用 Esc 退出（此前只能再点一次同一个图标，键盘用户尤其难发现）
  useEffect(() => {
    if (!maximized) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        setMaximized(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [maximized]);

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
            aria-label={maximized ? t('file.exitMaximize') : t('file.maximize')}
          >
            ⛶
          </button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={copyPath}
            disabled={!selectedFilePath}
            title={t('file.copyPath', { path: selectedFilePath || '' })}
            aria-label={t('file.copyPathLabel')}
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
