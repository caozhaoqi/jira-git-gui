import { useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
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
          title={t('repo.title')}
        />
        <section className="repo-col repo-col-mid">
          <FileTree />
        </section>
        <div
          className={`repo-resizer${dragging === 'right' ? ' dragging' : ''}`}
          onMouseDown={onMouseDown('right')}
          title={t('repo.title')}
        />
        <section className="repo-col repo-col-right" style={{ width: rightWidth, flexShrink: 0 }}>
          <Preview />
        </section>
      </div>
    </div>
  );
}
