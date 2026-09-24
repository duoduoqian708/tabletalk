import { useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import { IconCheck, IconPlus } from './ui/icons'

interface Props {
  onNew: () => void
}

export function ConnectionMenu({ onNew }: Props): React.JSX.Element {
  const { list, currentId, select } = useConnections()
  const { t } = useI18n()
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
        <span>{current ? `${current.name} · ${current.dialect}` : t('conn.menu.notConnected')}</span>
        <span className="chev">▾</span>
      </button>
      {open && (
        <div className="menu">
          <div className="m-title">{t('conn.menu.title')}</div>
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
                {c.read_only ? ' · ' + t('conn.readOnly') : ''}
              </span>
              {c.id === currentId && <span className="m-check"><IconCheck size={9} /></span>}
            </button>
          ))}
          <div className="sep" />
          <button className="mi" onClick={() => { setOpen(false); onNew() }}>
            <span style={{ width: 7 }} />
            <span><IconPlus size={9} /> {t('conn.menu.newConnection')}</span>
          </button>
        </div>
      )}
    </div>
  )
}
