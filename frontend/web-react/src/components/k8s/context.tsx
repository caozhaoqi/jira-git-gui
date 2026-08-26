import { createContext, useContext } from 'react';
import type { K8sEnv } from '../../api/types';
import type { ToastType } from '../../store/useAppStore';

export interface K8sTarget {
  env: string;
  pod: string;
  container: string;
  namespace: string;
}

export interface K8sContextValue {
  envs: K8sEnv[];
  target: K8sTarget;
  setTarget: (t: Partial<K8sTarget>) => void;
  reloadEnvs: () => Promise<void>;
  pushLog: (msg: string, level?: string) => void;
  addToast: (msg: string, type?: ToastType) => void;
  /** 打开「资源描述」弹窗（对应原生 openK8sDescribe），由 K8sPanel 统一承载单例弹窗。 */
  openDescribe: (kind: string, name: string, namespace?: string) => void;
}

export const K8sContext = createContext<K8sContextValue | null>(null);

export function useK8s(): K8sContextValue {
  const c = useContext(K8sContext);
  if (!c) throw new Error('useK8s 必须在 K8sPanel 内部使用');
  return c;
}
