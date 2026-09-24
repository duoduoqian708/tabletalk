import { useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '@renderer/store/i18n'

interface TableMeta {
  name: string
  row_count: number
  column_count: number
}

interface Props {
  tables: TableMeta[]
  /** 选中表名后的回调（星图定位 / 打开数据） */
  onPick: (name: string) => void
}

/** D1 搜索定位：工作台工具栏内嵌的表名搜索（从 Graph3D 内抽出，逻辑一致）。 */
export function GraphSearch({ tables, onPick }: Props): React.JSX.Element {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (!s) return []
    const match = tables.filter((tb) => tb.name.toLowerCase().includes(s))
    // 前缀优先排序
    match.sort((a, b) => {
      const ap = a.name.toLowerCase().startsWith(s) ? 0 : 1
      const bp = b.name.toLowerCase().startsWith(s) ? 0 : 1
      if (ap !== bp) return ap - bp
      return a.name.localeCompare(b.name)
    })
    return match.slice(0, 12)
  }, [q, tables])

  useEffect(() => {
    const onDoc = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  function pick(name: string): void {
    setOpen(false)
    setQ('')
    onPick(name)
  }

  return (
    <div className="g3d-search ws-search" ref={ref}>
      <input
        className="g3d-search-input ws-search-input"
        placeholder={t('graph.searchPlaceholder')}
        value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && filtered.length > 0) pick(filtered[0].name)
          else if (e.key === 'Escape') { setOpen(false); (e.target as HTMLInputElement).blur() }
        }}
      />
      {open && q.trim() && (
        <div className="g3d-search-drop ws-search-drop">
          {filtered.length === 0 ? (
            <div className="g3d-search-empty">{t('graph.searchNoResult')}</div>
          ) : (
            filtered.map((tb) => (
              <button
                key={tb.name}
                className="g3d-search-item"
                onMouseDown={(ev) => { ev.preventDefault(); pick(tb.name) }}
              >
                <span className="mono">{tb.name}</span>
                <span className="g3d-search-meta">{t('graph.searchMeta', { rows: tb.row_count, cols: tb.column_count })}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
