import { createContext, useContext } from 'react';
import type { KibanaSite } from '../../api/types';
import type { ToastType } from '../../store/useAppStore';

/** 当前生效的检索条件。子标签（容器视角 / 检索视角）共享同一份，切换标签不丢。 */
export interface KibanaQuery {
  start: string;
  end: string;
  namespace: string;
  container: string;
  app: string;
  host: string;
  pod: string;
  keyword: string;
  excludeKeyword: string;
  levels: string[];
}

export const DEFAULT_QUERY: KibanaQuery = {
  start: 'now-1h',
  end: '',
  namespace: '',
  container: '',
  app: '',
  host: '',
  pod: '',
  keyword: '',
  excludeKeyword: '',
  levels: [],
};

export interface KibanaContextValue {
  sites: KibanaSite[];
  site: string;
  setSite: (name: string) => void;
  reloadSites: () => Promise<void>;
  query: KibanaQuery;
  setQuery: (patch: Partial<KibanaQuery>) => void;
  pushLog: (msg: string, level?: string) => void;
  addToast: (msg: string, type?: ToastType) => void;
  openSiteModal: () => void;
}

export const KibanaContext = createContext<KibanaContextValue | null>(null);

export function useKibana(): KibanaContextValue {
  const c = useContext(KibanaContext);
  if (!c) throw new Error('useKibana 必须在 KibanaPanel 内部使用');
  return c;
}
