import { useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react';
import { RepoList } from './RepoList';
import { FileTree } from './FileTree';
import { Preview } from './Preview';
import { useT } from '../i18n';

export function RepoPanel() {
  const [leftWidth, setLeftWidth] = useState(280);
  const [rightWidth, setRightWidth] = useState(500);
  const [dragging, setDragging] = useState<null | 'left' | 'right'>(null);
  const draggingRef = useRef<null | 'left' | 'right'>(null);
  const { t } = useT();

  // 键盘调整列宽（此前拖拽条仅鼠标可用，键盘用户完全无法调整布局）
  const onKeyDown = (side: 'left' | 'right') => (e: ReactKeyboardEvent) => {
    const step = e.shiftKey ? 48 : 16;
    const clampL = (v: number) => Math.max(180, Math.min(520, v));
    const clampR = (v: number) => Math.max(300, Math.min(900, v));
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const d = e.key === 'ArrowRight' ? step : -step;
      if (side === 'left') setLeftWidth((w) => clampL(w + d));
      else setRightWidth((w) => clampR(w - d));
    } else if (e.key === 'Home') {
      e.preventDefault();
      if (side === 'left') setLeftWidth(180); else setRightWidth(900);
    } else if (e.key === 'End') {
      e.preventDefault();
      if (side === 'left') setLeftWidth(520); else setRightWidth(300);
    }
  };

  const onMouseDown = (side: 'left' | 'right') => (e: ReactMouseEvent) => {
    e.preventDefault();
    draggingRef.current = side;
    setDragging(side);
    const startX = e.clientX;
    const startLeft = leftWidth;
    const startRight = rightWidth;
    const onMove = (ev: MouseEvent) => {
      if (draggingRef.current === 'left') {
        setLeftWidth(Math.max(180, Math.min(520, startLeft + ev.clientX - startX)));
      } else if (draggingRef.current === 'right') {
        setRightWidth(Math.max(300, Math.min(900, startRight - (ev.clientX - startX))));
      }
    };
    const onUp = () => {
      draggingRef.current = null;
      setDragging(null);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  return (
    <div className="repo-panel">
      <div className="repo-three-col">
        <section className="repo-col repo-col-left" style={{ width: leftWidth, flexShrink: 0 }}>
          <RepoList />
        </section>
        <div
          className={`repo-resizer${dragging === 'left' ? ' dragging' : ''}`}
          onMouseDown={onMouseDown('left')}
          onKeyDown={onKeyDown('left')}
          role="separator"
          aria-orientation="vertical"
          aria-label={t('repo.resizeLeft')}
          aria-valuenow={leftWidth}
          tabIndex={0}
          title={t('repo.resizeHint')}
        />
        <section className="repo-col repo-col-mid">
          <FileTree />
        </section>
        <div
          className={`repo-resizer${dragging === 'right' ? ' dragging' : ''}`}
          onMouseDown={onMouseDown('right')}
          onKeyDown={onKeyDown('right')}
          role="separator"
          aria-orientation="vertical"
          aria-label={t('repo.resizeRight')}
          aria-valuenow={rightWidth}
          tabIndex={0}
          title={t('repo.resizeHint')}
        />
        <section className="repo-col repo-col-right" style={{ width: rightWidth, flexShrink: 0 }}>
          <Preview />
        </section>
      </div>
    </div>
  );
}
