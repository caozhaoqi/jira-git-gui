import { useCallback, useMemo, useSyncExternalStore } from 'react';
import { useAppStore } from '../store/useAppStore';
import type { Dict, Locale, MessageKey } from './types';
import { DEFAULT_LOCALE, LOCALE_STORAGE_KEY } from './types';
import { zh } from './zh';

// 默认语言静态加载（启动兜底 + 缺失 key 回退）；en/ja 按需动态 import 拆包
const DICTS: Partial<Record<Locale, Dict>> = { 'zh-CN': zh };

// 模块级 currentLocale 直接从 localStorage 恢复（与 store 初始 locale 同源同 key）。
// ⚠️ 必须在模块求值时就对齐，不能依赖 store 启动后调用 setLocale：
// client.ts 也 import 本模块，求值顺序 App → client → i18n → (循环) store，
// store 模块体会先于本模块体执行——若把初始化放在 store 顶层调用 setLocale，
// 这里尚未求值的 `let currentLocale` 处于 TDZ，会抛 ReferenceError 白屏。
let currentLocale: Locale = (() => {
  try {
    const v = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (v === 'zh-CN' || v === 'en-US' || v === 'ja-JP') return v;
  } catch {
    /* ignore */
  }
  return DEFAULT_LOCALE;
})();

// 字典异步加载完成的订阅源：useT 用它触发重渲染，让异步加载完的字典立即生效
let dictVersion = 0;
const dictListeners = new Set<() => void>();

function subscribeDict(onChange: () => void): () => void {
  dictListeners.add(onChange);
  return () => { dictListeners.delete(onChange); };
}

function getDictVersion(): number {
  return dictVersion;
}

function notifyDictChanged(): void {
  dictVersion += 1;
  dictListeners.forEach((cb) => cb());
}

const loaders: Partial<Record<Locale, Promise<void>>> = {};

function ensureDict(locale: Locale): void {
  if (locale === 'zh-CN' || DICTS[locale] || loaders[locale]) return;
  const loader = locale === 'en-US'
    ? import('./en').then((m) => { DICTS['en-US'] = m.en; })
    : import('./ja').then((m) => { DICTS['ja-JP'] = m.ja; });
  loaders[locale] = loader
    .then(() => notifyDictChanged())
    .catch((e) => {
      console.error(`[i18n] 加载 ${locale} 字典失败，回退中文:`, e);
      delete loaders[locale]; // 允许下次重试
    });
}

// 启动恢复：若持久化语言非默认（zh-CN），模块求值时即触发字典动态加载。
// ⚠️ 否则 reload 后 currentLocale=en-US 但 en 字典未加载，t() 全部回退中文，
// 表现为「切英文→刷新→界面变回中文」（下拉框却显示 en-US）。
if (currentLocale !== 'zh-CN') ensureDict(currentLocale);

export function setLocale(locale: Locale): void {
  currentLocale = locale;
  ensureDict(locale);
}

// 按点路径解析嵌套字典
function lookup(dict: Dict, key: MessageKey): string | undefined {
  const parts = key.split('.');
  let node: Dict | string = dict;
  for (const p of parts) {
    if (typeof node === 'string') return undefined;
    const next = (node as Dict)[p];
    if (next === undefined) return undefined;
    node = next as Dict | string;
  }
  return typeof node === 'string' ? node : undefined;
}

function interpolate(tpl: string, vars?: Record<string, string | number>): string {
  if (!vars) return tpl;
  return tpl.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name: string) =>
    vars[name] !== undefined ? String(vars[name]) : `{{${name}}}`
  );
}

// 纯函数翻译（供非组件模块 / 兜底使用）。
// 目标语言字典尚未加载完成时回退到默认语言（zh-CN）。
export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  const val = (DICTS[currentLocale] ? lookup(DICTS[currentLocale] as Dict, key) : undefined)
    ?? lookup(zh, key);
  if (val === undefined) return key; // 缺失 key 回退为 key 本身（便于发现遗漏）
  return interpolate(val, vars);
}

// 组件内 hook：自动跟随 store.locale 热切换，并订阅字典异步加载完成事件
// 注意：必须 memo 化返回值，否则每次渲染都产生新的 `t` 引用，
// 导致依赖 `[t]` 的 useEffect 在每次渲染后都重新执行（典型症状：
// 连接设置里切到 Cookie 又被后端返回的 mode 强制弹回 PAT）。
export function useT(): {
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  locale: Locale;
} {
  const locale = useAppStore((s) => s.locale);
  // 订阅字典加载完成：en/ja 异步就绪后触发一次重渲染，让新字典立即生效
  useSyncExternalStore(subscribeDict, getDictVersion);
  // 纯函数 t() 内部按调用时的全局 currentLocale 解析，因此回调可稳定为 []。
  const translate = useCallback(
    (key: MessageKey, vars?: Record<string, string | number>) => t(key, vars),
    [],
  );
  return useMemo(() => ({ locale, t: translate }), [locale, translate]);
}
