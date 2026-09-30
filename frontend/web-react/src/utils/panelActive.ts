import { createContext, useContext } from 'react';

/**
 * 「当前面板是否可见」上下文。
 *
 * 背景：App 的面板策略是「访问过的页签永久挂载、非当前页只 display:none」，
 * 这样切页签不会丢失终端会话/表单输入等本地状态。代价是**隐藏面板里的定时器
 * 仍在按原频率触发**（K8s 事件/资源 Top 的 10s 轮询、Kibana 日志流、日志查看器），
 * 会在用户早已切走的情况下持续打后端。浏览器只会节流「整个标签页不可见」的定时器，
 * 对「同一文档内的隐藏子树」不做任何节流。
 *
 * 因此各面板的轮询统一用 `usePanelActive()` 判断，隐藏时暂停。
 */
export const PanelActiveContext = createContext<boolean>(true);

/** 当前面板是否为激活页签（隐藏面板返回 false）。 */
export function usePanelActive(): boolean {
  return useContext(PanelActiveContext);
}
