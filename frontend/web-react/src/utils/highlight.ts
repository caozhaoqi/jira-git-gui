import hljs from 'highlight.js/lib/core';
import python from 'highlight.js/lib/languages/python';
import bash from 'highlight.js/lib/languages/bash';
import json from 'highlight.js/lib/languages/json';
import yaml from 'highlight.js/lib/languages/yaml';
import xml from 'highlight.js/lib/languages/xml';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import css from 'highlight.js/lib/languages/css';
import sql from 'highlight.js/lib/languages/sql';
import go from 'highlight.js/lib/languages/go';
import rust from 'highlight.js/lib/languages/rust';
import java from 'highlight.js/lib/languages/java';
import ini from 'highlight.js/lib/languages/ini';
import markdown from 'highlight.js/lib/languages/markdown';
import dockerfile from 'highlight.js/lib/languages/dockerfile';
import nginx from 'highlight.js/lib/languages/nginx';
import plaintext from 'highlight.js/lib/languages/plaintext';

// 按需注册，避免引入整包导致体积过大
hljs.registerLanguage('python', python);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('json', json);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('css', css);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('go', go);
hljs.registerLanguage('rust', rust);
hljs.registerLanguage('java', java);
hljs.registerLanguage('ini', ini);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('dockerfile', dockerfile);
hljs.registerLanguage('nginx', nginx);
hljs.registerLanguage('plaintext', plaintext);

// 文件扩展名 → highlight.js 语言
const EXT_LANG: Record<string, string> = {
  py: 'python', sh: 'bash', bash: 'bash', zsh: 'bash',
  json: 'json', yaml: 'yaml', yml: 'yaml',
  xml: 'xml', html: 'xml', htm: 'xml', svg: 'xml',
  js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', tsx: 'typescript',
  css: 'css', scss: 'css', less: 'css',
  sql: 'sql', go: 'go', rs: 'rust', java: 'java',
  ini: 'ini', cfg: 'ini', conf: 'ini', toml: 'ini', env: 'ini',
  md: 'markdown', mkd: 'markdown', markdown: 'markdown',
  dockerfile: 'dockerfile',
};

// 根据文件名推断语言；返回 { lang, label }
export function langFromName(name: string): { lang: string; label: string } {
  const lower = (name || '').toLowerCase();
  const base = lower.split('/').pop() || '';
  // 特殊文件名（无扩展名）
  if (base === 'dockerfile') return { lang: 'dockerfile', label: 'Dockerfile' };
  if (base === 'makefile') return { lang: 'bash', label: 'Makefile' };
  if (base === 'nginx.conf') return { lang: 'nginx', label: 'Nginx' };
  const ext = base.includes('.') ? base.split('.').pop()! : '';
  const lang = EXT_LANG[ext] || 'plaintext';
  return { lang, label: lang === 'plaintext' ? 'Text' : lang.toUpperCase() };
}

// 尝试把 JSON 文本格式化（缩进 2 空格）；解析失败则原样返回
export function tryPrettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

// 生成高亮后的 HTML
export function highlightCode(text: string, name: string): string {
  const { lang } = langFromName(name);
  try {
    if (lang === 'json') {
      const pretty = tryPrettyJson(text);
      return hljs.highlight(pretty, { language: 'json' }).value;
    }
    if (lang === 'plaintext') {
      return hljs.highlight(text, { language: 'plaintext' }).value;
    }
    // 已知语言直接高亮；未知则自动探测
    if (hljs.getLanguage(lang)) {
      return hljs.highlight(text, { language: lang }).value;
    }
    return hljs.highlightAuto(text).value;
  } catch {
    return escapeHtmlFallback(text);
  }
}

function escapeHtmlFallback(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
