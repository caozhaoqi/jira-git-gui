import { useAppStore } from '../store/useAppStore';
import type { Dict, Locale, MessageKey } from './types';
import { DEFAULT_LOCALE } from './types';
import { zh } from './zh';
import { en } from './en';
import { ja } from './ja';

const DICTS: Record<Locale, Dict> = { 'zh-CN': zh, 'en-US': en, 'ja-JP': ja };

let currentLocale: Locale = DEFAULT_LOCALE;

export function setLocale(locale: Locale): void {
  currentLocale = locale;
}

export function getLocale(): Locale {
  return currentLocale;
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

// 纯函数翻译（供非组件模块 / 兜底使用）
export function t(key: MessageKey, vars?: Record<string, string | number>): string {
  const val = lookup(DICTS[currentLocale], key) ?? lookup(DICTS[DEFAULT_LOCALE], key);
  if (val === undefined) return key; // 缺失 key 回退为 key 本身（便于发现遗漏）
  return interpolate(val, vars);
}

// 组件内 hook：自动跟随 store.locale 热切换
export function useT(): {
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  locale: Locale;
} {
  const locale = useAppStore((s) => s.locale);
  return {
    locale,
    t: (key: MessageKey, vars?: Record<string, string | number>) => t(key, vars),
  };
}
