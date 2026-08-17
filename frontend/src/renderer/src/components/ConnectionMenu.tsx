import { useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'

interface Props {
  onNew: () => void
}

export function ConnectionMenu({ onNew }: Props): React.JSX.Element {
  const { list, currentId, select } = useConnections()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const current = list.find((c) => c.id === currentId)

  useEffect(() => {
    const onDoc = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  return (
    <div className="dd" ref={ref}>
      <button className="dd-trigger" onClick={() => setOpen((o) => !o)}>
        <span className="sd" />
        <span>{current ? `${current.name} · ${current.dialect}` : '未连接'}</span>
        <span className="chev">▾</span>
      </button>
      {open && (
        <div className="menu">
          <div className="m-title">连接</div>
          {list.map((c) => (
            <button
              key={c.id}
              className="mi"
              onClick={() => {
                select(c.id)
                setOpen(false)
              }}
            >
              <span className="sd" style={{ background: c.id === currentId ? 'var(--teal)' : 'var(--ink-faint)' }} />
              <span>{c.name}</span>
              <span className="m-sub">
                {c.dialect}
                {c.read_only ? ' · 只读' : ''}
              </span>
              {c.id === currentId && <span className="m-check">✓</span>}
            </button>
          ))}
          <div className="sep" />
          <button className="mi" onClick={() => { setOpen(false); onNew() }}>
            <span style={{ width: 7 }} />
            <span>＋ 新建连接</span>
          </button>
        </div>
      )}
    </div>
  )
}
