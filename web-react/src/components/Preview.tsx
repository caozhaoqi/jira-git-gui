import { useEffect, useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiGet } from '../api/client';
import type { FileResp } from '../api/types';

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
  const [title, setTitle] = useState('预览');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [maximized, setMaximized] = useState(false);
  const pushLog = useAppStore((s) => s.pushLog);

  useEffect(() => {
    if (!selectedFilePath) {
      setTitle('预览');
      setContent('');
      setError('');
      return;
    }
    let cancelled = false;
    setLoading(true);
    setTitle(`加载中 · ${selectedFilePath}`);
    setError('');
    apiGet<FileResp>(`/api/file?path=${encodeURIComponent(selectedFilePath)}`)
      .then((res) => {
        if (cancelled) return;
        if (res.error) {
          setTitle('错误');
          setContent(res.error);
        } else {
          const { text, isJson } = formatContent(selectedFilePath, res.content || '');
          setTitle(`预览 · ${selectedFilePath}${isJson ? '  (JSON 已格式化)' : ''}`);
          setContent(text);
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setTitle('错误');
        setContent(e.message || '加载失败');
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selectedFilePath]);

  async function copyPath() {
    if (!selectedFilePath) return;
    try {
      await navigator.clipboard.writeText(selectedFilePath);
      pushLog(`已复制路径：${selectedFilePath}`);
    } catch {
      pushLog('复制失败，浏览器可能未授权剪贴板', 'error');
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
            title="最大化 / 还原"
          >
            ⛶
          </button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={copyPath}
            disabled={!selectedFilePath}
            title="复制文件路径"
          >
            📋
          </button>
        </div>
      </div>
      <pre className="code-block preview-content">
        {loading ? '加载中…' : error ? error : content || '选择文件后在此预览'}
      </pre>
    </div>
  );
}
