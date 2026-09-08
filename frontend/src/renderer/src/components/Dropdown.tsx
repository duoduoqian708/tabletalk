import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { IconCheck } from './ui/icons'

export interface DropdownOption {
  value: string
  label: string
  /** 右侧次级说明（可选） */
  hint?: string
}

interface Props {
  value: string
  options: DropdownOption[]
  onChange: (v: string) => void
  /** 打开即聚焦（等价原生 autoFocus） */
  autoFocus?: boolean
  disabled?: boolean
  className?: string
  style?: React.CSSProperties
  placeholder?: string
  /** 弹层展开方向；auto=按视口空间自适应（默认向下，空间不足向上） */
  dropUp?: boolean | 'auto'
  /** 弹层最小宽度=触发器宽；列表项过多时内部滚动 */
  title?: string
  /** 弹层因点击外部/Esc 关闭时回调（选中触发的 onChange 不算） */
  onClose?: () => void
}

/**
 * 平台风格下拉（替代原生 <select>）：触发器 + 自绘弹层，选项列表完全跟随平台主题。
 * 交互契约对齐原生：value 受控、onChange 即时回传、Esc 关闭、↑↓ 移动 + Enter 选中、
 * 点击外部关闭、autoFocus 打开即弹层。
 */
export function Dropdown({ value, options, onChange, autoFocus, disabled, className, style, placeholder, dropUp = 'auto', title, onClose }: Props): React.JSX.Element {
  const [open, setOpen] = useState(!!autoFocus)
  const [up, setUp] = useState(false)
  const [active, setActive] = useState(0)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  const cur = options.find((o) => o.value === value)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false)
        onClose?.()
      }
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open, onClose])

  // 弹层方向：视口下方放不下且上方更宽裕 → 向上
  useLayoutEffect(() => {
    if (!open || dropUp !== 'auto' || !rootRef.current) return
    const r = rootRef.current.getBoundingClientRect()
    const need = Math.min(options.length, 8) * 30 + 12
    setUp(r.bottom + need > window.innerHeight && r.top - need > 0)
  }, [open, dropUp, options.length])

  // 打开时定位当前项
  useEffect(() => {
    if (open) {
      const i = options.findIndex((o) => o.value === value)
      setActive(i >= 0 ? i : 0)
    }
  }, [open, value, options])

  // active 滚动进可视区
  useEffect(() => {
    if (!open || !listRef.current) return
    const el = listRef.current.children[active] as HTMLElement | undefined
    el?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  function commit(v: string): void {
    setOpen(false)
    if (v !== value) onChange(v)
  }

  function onKey(e: React.KeyboardEvent): void {
    if (disabled) return
    if (!open) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        setOpen(true)
      }
      return
    }
    if (e.key === 'Escape') { e.preventDefault(); setOpen(false); onClose?.() }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, options.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)) }
    else if (e.key === 'Enter') { e.preventDefault(); if (options[active]) commit(options[active].value) }
  }

  return (
    <div ref={rootRef} className={`pdd${className ? ` ${className}` : ''}`} style={style} onKeyDown={onKey}>
      <button
        type="button"
        className="pdd-trigger"
        disabled={disabled}
        title={title}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={`pdd-label${cur ? '' : ' ph'}`}>{cur?.label ?? placeholder ?? '—'}</span>
        <span className={`pdd-arrow${open ? ' on' : ''}`}>▾</span>
      </button>
      {open && (
        <div
          ref={listRef}
          className={`pdd-list${up ? ' up' : ''}`}
          role="listbox"
        >
          {options.map((o, i) => (
            <div
              key={o.value}
              role="option"
              aria-selected={o.value === value}
              className={`pdd-item${o.value === value ? ' sel' : ''}${i === active ? ' act' : ''}`}
              onMouseEnter={() => setActive(i)}
              onClick={() => commit(o.value)}
            >
              <span className="pdd-item-label">{o.label}</span>
              {o.hint && <span className="pdd-item-hint mono">{o.hint}</span>}
              {o.value === value && <span className="pdd-check mono"><IconCheck size={9} /></span>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
