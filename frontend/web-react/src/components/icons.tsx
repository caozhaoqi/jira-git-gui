/**
 * 侧栏单色矢量图标（参考 VS Code Activity Bar）。
 *
 * 设计约束：
 * - 全部 16×16 stroke 图标，stroke=currentColor —— 颜色完全跟随所在
 *   .tab 的 color（默认灰 / hover 深 / active 主色），暗色模式免适配；
 * - 替换原 emoji：彩色 emoji 无法随主题着色，且跨平台字形不一致；
 * - path 力求 1-3 笔，小尺寸（13px 渲染）下仍可辨认。
 */
import type { ReactElement } from 'react';

const P: Record<string, string[]> = {
  repo: [
    'M2 4.5A1.5 1.5 0 013.5 3h3l1.5 2h4.5A1.5 1.5 0 0114 6.5v5a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 012 11.5z',
  ],
  diff: [
    'M4 3.5v9',
    'M12 3.5v9',
    'M2 4.5a1.5 1.5 0 104 0 1.5 1.5 0 00-4 0z',
    'M10 11.5a1.5 1.5 0 104 0 1.5 1.5 0 00-4 0z',
    'M6 4.5h3.5A1.5 1.5 0 0111 6v3.5',
  ],
  cf: [
    'M6 2h4',
    'M7 2v4.2L3.6 12a1.5 1.5 0 001.3 2h6.2a1.5 1.5 0 001.3-2L9 6.2V2',
  ],
  cfsync: [
    'M13 8A5 5 0 1111.2 4.2',
    'M13 2v3h-3',
  ],
  cfdebug: [
    'M8 5.5a2.8 2.8 0 012.8 2.8v1.4a2.8 2.8 0 11-5.6 0V8.3A2.8 2.8 0 018 5.5z',
    'M8 5.5V3.5',
    'M5.2 8H3M13 8h-2.2M5.2 11l-1.8 1.4M10.8 11l1.8 1.4',
  ],
  hcm: [
    'M2 3.5h12v9H2z',
    'M2 6.5h12M2 9.5h12M6 6.5v6',
  ],
  hcmapi: [
    'M11.2 2.3a3.4 3.4 0 00-3.6 4.9L2.6 12.2a1 1 0 001.4 1.4l5-5a3.4 3.4 0 004.9-3.6l-2.2 2.2-1.7-1.7z',
  ],
  hcmops: [
    'M2 6.5h12V13H2z',
    'M6 6.5V5a1.5 1.5 0 011.5-1.5h1A1.5 1.5 0 0110 5v1.5',
    'M2 9.5h12',
  ],
  k8s: [
    'M8 1.8l5.2 3v6.4l-5.2 3-5.2-3V4.8z',
    'M8 6a2 2 0 100 4 2 2 0 000-4z',
  ],
  kibana: [
    'M2.5 2.5v11h11',
    'M4.5 10.5l3-3.5 2 2 3.5-4',
  ],
  logs: [
    'M3 4.5h10M3 8h10M3 11.5h6',
  ],
  clash: [
    'M8 2a6 6 0 100 12A6 6 0 008 2z',
    'M2 8h12',
    'M8 2c-2.8 2.8-2.8 9.2 0 12 2.8-2.8 2.8-9.2 0-12',
  ],
  diagnose: [
    'M7 12a5 5 0 100-10 5 5 0 000 10z',
    'M10.8 10.8L14 14',
  ],
  settings: [
    'M8 5.7A2.3 2.3 0 108 10.3 2.3 2.3 0 008 5.7z',
    'M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.6 3.6l1.4 1.4M11 11l1.4 1.4M12.4 3.6L11 5M5 11l-1.4 1.4',
  ],
  groupCF: [
    'M4.5 11.5a2.6 2.6 0 01-.4-5.2 3.6 3.6 0 017-.4 2.7 2.7 0 01-.6 5.6z',
  ],
  groupHCM: [
    'M8 2l6 3-6 3-6-3z',
    'M2 8.5l6 3 6-3',
  ],
  groupSys: [
    'M3 4.5h10M3 8h10M3 11.5h10',
    'M6 4.5a.9.9 0 101.8 0 .9.9 0 00-1.8 0z',
    'M9.5 11.5a.9.9 0 101.8 0 .9.9 0 00-1.8 0z',
  ],
};

export function TabIcon({ name, size = 13 }: { name: string; size?: number }): ReactElement {
  const paths = P[name] || P.repo;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths.map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
