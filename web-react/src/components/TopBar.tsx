import { useState } from 'react';
import { useAppStore } from '../store/useAppStore';
import { apiPost } from '../api/client';

export function TopBar({ onOpenConnect }: { onOpenConnect: () => void }) {
  const status = useAppStore((s) => s.status);
  const theme = useAppStore((s) => s.theme);
  const toggleTheme = useAppStore((s) => s.toggleTheme);
  const qps = useAppStore((s) => s.qps);
  const setQps = useAppStore((s) => s.setQps);
  const pushLog = useAppStore((s) => s.pushLog);
  const addToast = useAppStore((s) => s.addToast);
  const [rate, setRate] = useState(String(qps));

  const credOk = !!(status?.cookie_set || status?.pat_set);

  async function applyRate() {
    const v = Math.max(1, Math.min(50, parseInt(rate, 10) || 6));
    setRate(String(v));
    try {
      await apiPost('/api/rate-limit', { qps: v });
      setQps(v);
      pushLog(`请求速率上限已设为 ${v} 请求/秒`);
    } catch (e: any) {
      addToast(e.message || '设置速率失败', 'error');
    }
  }

  const parts = [
    `模式 ${(status?.mode || '-').toUpperCase()}`,
    `仓库 ${status?.repo_id || '-'}`,
    `分支 ${status?.branch || '(默认)'}`,
    credOk
      ? `凭证已配置${status?.cookie_source ? `(${status.cookie_source})` : ''}`
      : '凭证未配置',
    `速率 ${qps}/秒`,
  ];

  return (
    <header className="topbar">
      <div className="topbar-left">
        <span className="app-title">Jira Git 通用拉取工具</span>
        <span
          className={`status-dot ${credOk ? 'ok' : 'warn'}`}
          title={credOk ? '后端已连接，凭证已配置' : '后端未配置凭证'}
        />
        <span className="status-text">{parts.join(' | ')}</span>
      </div>
      <div className="topbar-right">
        <label className="rate-field" title="对 Jira 服务器的稳态请求速率上限">
          速率
          <input
            type="number"
            min={1}
            max={50}
            value={rate}
            onChange={(e) => setRate(e.target.value)}
            onBlur={applyRate}
            className="spin"
          />
        </label>
        <button className="btn btn-ghost" onClick={onOpenConnect} title="连接设置">
          ⚙ 连接设置
        </button>
        <button
          className="btn btn-icon"
          onClick={toggleTheme}
          title="切换浅色 / 深色主题"
        >
          {theme === 'dark' ? '☀ 主题' : '🌓 主题'}
        </button>
      </div>
    </header>
  );
}
