// i18n 类型定义
export type Locale = 'zh-CN' | 'en-US' | 'ja-JP';

// 字典结构：嵌套对象，叶子为字符串（可含 {{var}} 插值占位符）
export interface Dict {
  [namespace: string]: Dict | string;
}

// 扁平化后的查找 key（如 "k8s.snapshot.title"）
export type MessageKey = string;

export const LOCALES: { value: Locale; label: string; flag: string }[] = [
  { value: 'zh-CN', label: '中文', flag: '🇨🇳' },
  { value: 'en-US', label: 'English', flag: '🇺🇸' },
  { value: 'ja-JP', label: '日本語', flag: '🇯🇵' },
];

export const DEFAULT_LOCALE: Locale = 'zh-CN';

// localStorage 里存语言的 key（store 与 i18n 共用；i18n 不能 import store——
// 两者存在循环依赖，所以 key 常量放这个零依赖模块里）
export const LOCALE_STORAGE_KEY = 'jgg-locale';
