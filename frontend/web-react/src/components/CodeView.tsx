import { useMemo, type ReactNode } from 'react';
import { useT } from '../i18n';

/**
 * 代码视图（对齐网关调试页 View 下拉的「Code」项）：
 * - 行号 + 轻量语法高亮（JSON / HTML / 纯文本自动识别，截断的 JSON 也按 JSON 高亮）
 * - 搜索词命中高亮（<mark>）+ 命中行数统计
 *
 * 高亮为自研 tokenizer（不引 highlight.js）：token 边界明确，搜索命中只包
 * **文本 token**，不会误伤 class 属性名；同时避免 dangerouslySetInnerHTML。
 * 色板与 K8s 文件查看的 hljs 主题同源（One Dark），见 global.css 的 .codeview-*。
 */

interface CodeViewProps {
  text: string;
  /** 搜索词（空 = 不高亮） */
  search?: string;
}

type Mode = 'json' | 'html' | 'plain';

function detectMode(text: string): Mode {
  const t = text.trimStart();
  if (t.startsWith('{') || t.startsWith('[')) return 'json';
  if (/^<[!a-zA-Z]/.test(t)) return 'html';
  return 'plain';
}

interface Tok {
  text: string;
  cls?: string;
}

/** JSON 行 token 化：键（带冒号的字符串）/ 字符串 / 数字 / 关键字 / 标点 */
function tokenizeJsonLine(line: string): Tok[] {
  const out: Tok[] = [];
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|(\btrue\b|\bfalse\b|\bnull\b)|([{}\[\],:])/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push({ text: line.slice(last, m.index) });
    if (m[1] !== undefined) {
      if (m[2] !== undefined) {
        out.push({ text: m[1], cls: 'key' });
        out.push({ text: m[2] });
      } else {
        out.push({ text: m[1], cls: 'str' });
      }
    } else if (m[3] !== undefined) {
      out.push({ text: m[3], cls: 'num' });
    } else if (m[4] !== undefined) {
      out.push({ text: m[4], cls: 'kw' });
    } else if (m[5] !== undefined) {
      out.push({ text: m[5], cls: 'punct' });
    }
    last = m.index + m[0].length;
  }
  if (last < line.length) out.push({ text: line.slice(last) });
  return out;
}

/** 单个标签 `<meta charset="utf-8">` → 标点/标签名/属性/等号/属性值 */
function tokenizeTag(tag: string): Tok[] {
  const toks: Tok[] = [{ text: tag.startsWith('</') ? '</' : '<', cls: 'punct' }];
  const inner = tag.replace(/^<\/?/, '').replace(/>$/, '');
  const nameM = inner.match(/^[a-zA-Z_:][\w:.-]*/);
  if (nameM) toks.push({ text: nameM[0], cls: 'tag' });
  const rest = inner.slice(nameM ? nameM[0].length : 0);
  const re = /([a-zA-Z_:][\w:.-]*)(=)("[^"]*"|'[^']*')?|("[^"]*")/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(rest)) !== null) {
    if (m[1] !== undefined) {
      toks.push({ text: ' ' + m[1], cls: 'attr' });
      if (m[2] !== undefined) {
        toks.push({ text: m[2], cls: 'punct' });
        if (m[3] !== undefined) toks.push({ text: m[3], cls: 'str' });
      }
    } else if (m[4] !== undefined) {
      toks.push({ text: m[4], cls: 'str' });
    }
  }
  toks.push({ text: '>', cls: 'punct' });
  return toks;
}

/** HTML 行 token 化：注释 / doctype / 标签 / 文本 */
function tokenizeHtmlLine(line: string): Tok[] {
  const out: Tok[] = [];
  const re = /(<!--[\s\S]*?-->)|(<![^>]+>)|(<\/?[a-zA-Z][^>]*>)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push({ text: line.slice(last, m.index) });
    const whole = m[0];
    if (m[1] !== undefined) out.push({ text: whole, cls: 'comment' });
    else if (m[2] !== undefined) out.push({ text: whole, cls: 'doctype' });
    else out.push(...tokenizeTag(whole));
    last = m.index + whole.length;
  }
  if (last < line.length) out.push({ text: line.slice(last) });
  return out;
}

/** 搜索命中包裹（仅切分文本 token，class 名不会误命中） */
function markSearch(text: string, q: string, keyBase: string): ReactNode[] {
  if (!q) return [text];
  const lower = text.toLowerCase();
  const ql = q.toLowerCase();
  const parts: ReactNode[] = [];
  let i = 0;
  let k = 0;
  for (;;) {
    const idx = lower.indexOf(ql, i);
    if (idx < 0) {
      if (i < text.length) parts.push(text.slice(i));
      break;
    }
    if (idx > i) parts.push(text.slice(i, idx));
    parts.push(<mark key={`${keyBase}-m${k++}`}>{text.slice(idx, idx + q.length)}</mark>);
    i = idx + q.length;
  }
  return parts;
}

export function CodeView({ text, search = '' }: CodeViewProps) {
  const { t } = useT();
  const q = search.trim();
  const mode = useMemo(() => detectMode(text), [text]);
  const lines = useMemo(() => text.split('\n'), [text]);
  const hitLines = useMemo(
    () => (q ? lines.reduce((n, l) => n + (l.toLowerCase().includes(q.toLowerCase()) ? 1 : 0), 0) : 0),
    [lines, q],
  );

  return (
    <div className="codeview">
      {q && (
        <div className="codeview-hits">
          {hitLines ? t('hcm.apiCodeHits', { n: hitLines }) : t('hcm.apiCodeNoHit')}
        </div>
      )}
      <div className="codeview-body">
        {lines.map((line, i) => (
          <div className="codeview-line" key={i}>
            <span className="codeview-ln" aria-hidden="true">{i + 1}</span>
            <span className="codeview-text">
              {(mode === 'json'
                ? tokenizeJsonLine(line)
                : mode === 'html'
                  ? tokenizeHtmlLine(line)
                  : [{ text: line } as Tok]
              ).map((tok, ti) => (
                <span key={ti} className={tok.cls ? `codeview-${tok.cls}` : undefined}>
                  {markSearch(tok.text, q, `${i}-${ti}`)}
                </span>
              ))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
