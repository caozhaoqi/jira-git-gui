import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';

export interface ComboboxOption {
  value: string;
  label?: string;
}

function normOptions(opts: Array<string | ComboboxOption>): ComboboxOption[] {
  return opts
    .map((o) => (typeof o === 'string' ? { value: o } : o))
    .filter((o) => o.value !== '');
}

interface ComboboxInputProps {
  value: string;
  onChange: (v: string) => void;
  options: Array<string | ComboboxOption>;
  placeholder?: string;
  /** 包一层相对定位容器用的额外类（如 HCM 结构化筛选行的 220px flex 容器）。 */
  wrapperClassName?: string;
  /** 套在内部 <input> 上的额外类（如 .input / .cfc-input / .hcm-struct-field）。 */
  inputClassName?: string;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
  /** 透传给内部 <input> 的 tooltip（原 datalist 输入框常带 title 提示）。 */
  title?: string;
  onBlur?: () => void;
  /** 弹层未选中项时按 Enter 的回调（如触发查询）。选中项时优先回填该选项。 */
  onEnter?: () => void;
}

/**
 * 通用「可手输 + 联想」下拉，替代原生 <datalist>。
 * 原生 datalist 的弹出列表无法被 CSS 定制（以浏览器默认样式渲染，与全站 .sel 下拉断裂）。
 * 弹层复用系统 token（surface/border/shadow/圆角/字号），深浅主题自适应；
 * 支持即时过滤、↑/↓/Enter/Esc 键盘操作、点击外部关闭、ARIA combobox 语义。
 */
export function ComboboxInput({
  value,
  onChange,
  options,
  placeholder,
  wrapperClassName = '',
  inputClassName = '',
  disabled,
  id,
  ariaLabel,
  title,
  onBlur,
  onEnter,
}: ComboboxInputProps) {
  const [open, setOpen] = useState(false);
  const [hiIdx, setHiIdx] = useState(-1);
  const wrapRef = useRef<HTMLDivElement>(null);
  const opts = useMemo(() => normOptions(options), [options]);
  const q = value.trim().toLowerCase();
  const filtered = useMemo(
    () =>
      q
        ? opts.filter(
            (o) => o.value.toLowerCase().includes(q) || (o.label || '').toLowerCase().includes(q)
          )
        : opts,
    [opts, q]
  );

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
    setHiIdx(-1);
  };

  return (
    <div className={`cb-wrap ${wrapperClassName}`} ref={wrapRef}>
      <input
        id={id}
        className={`cb-field ${inputClassName}`}
        value={value}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
          setHiIdx(-1);
        }}
        onFocus={() => setOpen(true)}
        onBlur={onBlur}
        onKeyDown={(e: ReactKeyboardEvent) => {
          if (!open || filtered.length === 0) return;
          if (e.key === 'ArrowDown') {
            e.preventDefault();
            setHiIdx((i) => Math.min(i + 1, filtered.length - 1));
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setHiIdx((i) => Math.max(i - 1, 0));
          } else if (e.key === 'Enter') {
            if (hiIdx >= 0 && filtered[hiIdx]) {
              e.preventDefault();
              pick(filtered[hiIdx].value);
            } else if (onEnter) {
              onEnter();
            }
          } else if (e.key === 'Escape') {
            setOpen(false);
          }
        }}
        placeholder={placeholder}
        spellCheck={false}
        aria-label={ariaLabel}
        title={title}
        role="combobox"
        aria-expanded={open && filtered.length > 0}
        aria-autocomplete="list"
      />
      {open && filtered.length > 0 && (
        <div className="cb-popup" role="listbox">
          {filtered.map((o, i) => (
            <button
              type="button"
              key={o.value}
              role="option"
              aria-selected={i === hiIdx}
              className={`cb-item${i === hiIdx ? ' active' : ''}`}
              onMouseDown={(e) => {
                // preventDefault：避免 input 先失焦把弹层关掉、click 落空
                e.preventDefault();
                pick(o.value);
              }}
              onMouseEnter={() => setHiIdx(i)}
            >
              <span className="cb-key">{o.value}</span>
              {o.label && o.label !== o.value && (
                <span className="cb-label">{o.label}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
