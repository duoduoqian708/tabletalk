import { useEffect, useMemo, useRef, useState } from 'react'
import type { KnowledgeOverview } from '@renderer/api/types'

interface Props {
  overview: KnowledgeOverview
  highlight?: string[] // 候选表（路由预览）
  onSelect?: (table: string | null) => void
}

interface Pos { x: number; y: number }

/** 右区图预览画布：FK/值重叠 过滤开关 + 平移缩放 + 节点选中。 */
export function KnowledgeGraph({ overview, highlight, onSelect }: Props): React.JSX.Element {
  const [showFk, setShowFk] = useState(true)
  const [showOverlap, setShowOverlap] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [pan, setPan] = useState<Pos>({ x: 40, y: 30 })
  const [zoom, setZoom] = useState(1)
  const svgRef = useRef<SVGSVGElement | null>(null)
  const dragRef = useRef<{ start: Pos; pan: Pos; moved: boolean } | null>(null)

  const hl = new Set(highlight ?? [])
  const tableNames = overview.tables.map((t) => t.name)
  const tagged = useMemo(
    () => new Set(overview.tables.filter((t) => t.tags.length > 0).map((t) => t.name)),
    [overview.tables]
  )

  // 圆环布局（随画布大小自适应）
  const nodes = useMemo(() => {
    const W = 900
    const H = 560
    const CX = W / 2
    const CY = H / 2
    const R = Math.min(W, H) / 2 - 70
    const pos: Record<string, Pos> = {}
    tableNames.forEach((n, i) => {
      const a = (i * 2 * Math.PI) / Math.max(1, tableNames.length) - Math.PI / 2
      pos[n] = { x: CX + Math.cos(a) * R, y: CY + Math.sin(a) * R }
    })
    return pos
  }, [tableNames.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  const edges = useMemo(
    () => overview.graph.edges.filter((e) => (e.kind === 'fk' ? showFk : showOverlap)),
    [overview.graph.edges, showFk, showOverlap]
  )

  useEffect(() => {
    onSelect?.(selected)
  }, [selected, onSelect])

  function handleWheel(e: React.WheelEvent): void {
    const f = e.deltaY < 0 ? 1.1 : 0.9
    setZoom((z) => Math.min(3, Math.max(0.3, z * f)))
  }

  function beginDrag(e: React.MouseEvent): void {
    dragRef.current = { start: { x: e.clientX, y: e.clientY }, pan, moved: false }
  }

  function moveDrag(e: React.MouseEvent): void {
    const d = dragRef.current
    if (!d) return
    const dx = e.clientX - d.start.x
    const dy = e.clientY - d.start.y
    if (Math.abs(dx) + Math.abs(dy) > 4) d.moved = true
    if (d.moved) setPan({ x: d.pan.x + dx, y: d.pan.y + dy })
  }

  function endDrag(e: React.MouseEvent): void {
    const d = dragRef.current
    dragRef.current = null
    if (d && !d.moved) {
      // 点击空白 → 取消选中
      const target = e.target as Element
      if (target === svgRef.current || target.closest('.kg2-bg')) setSelected(null)
    }
  }

  function fit(): void {
    setPan({ x: 40, y: 30 })
    setZoom(1)
  }

  const selTable = selected ? overview.tables.find((t) => t.name === selected) : null

  return (
    <div className="kg2">
      <div className="kg2-toolbar">
        <button className="kg2-btn" onClick={fit} title="适应视图">适应</button>
        <button className="kg2-btn" onClick={() => setZoom((z) => Math.min(3, z * 1.2))}>＋</button>
        <button className="kg2-btn" onClick={() => setZoom((z) => Math.max(0.3, z / 1.2))}>－</button>
        <span className="kg2-sep" />
        <button className={`kg2-btn${showFk ? ' on' : ''}`} onClick={() => setShowFk((v) => !v)}>FK</button>
        <button className={`kg2-btn${showOverlap ? ' on' : ''}`} onClick={() => setShowOverlap((v) => !v)}>值重叠</button>
        <span className="kg2-spacer" />
        <span className="kg2-hint mono">{tableNames.length} 表 · {overview.graph.edges.length} 边</span>
      </div>
      <div className="kg2-canvas">
        <svg
          ref={svgRef}
          className="kg2-svg"
          viewBox="0 0 900 560"
          onWheel={handleWheel}
          onMouseDown={beginDrag}
          onMouseMove={moveDrag}
          onMouseUp={endDrag}
        >
          <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
            <rect className="kg2-bg" x={-200} y={-200} width={1300} height={960} />
            {edges.map((e, i) => {
              const a = nodes[e.from]
              const b = nodes[e.to]
              if (!a || !b) return null
              const isOverlap = e.kind === 'overlap'
              const hot = hl.size > 0 && (hl.has(e.from) || hl.has(e.to))
              const isSel = selected !== null && (e.from === selected || e.to === selected)
              const midX = (a.x + b.x) / 2
              const midY = (a.y + b.y) / 2
              return (
                <g key={i}>
                  <line
                    className={`kg2-edge ${e.kind}${hot ? ' hot' : ''}${isSel ? ' sel' : ''}`}
                    x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                  />
                  {isOverlap && (
                    <text x={midX} y={midY - 4} textAnchor="middle" className="kg2-edge-label mono">
                      {e.from_col}↔{e.to_col} · {Math.round((e.weight ?? 0) * 100)}%
                    </text>
                  )}
                </g>
              )
            })}
            {Object.entries(nodes).map(([name, p]) => {
              const isTagged = tagged.has(name)
              const isHl = hl.has(name)
              const isSel = selected === name
              const t = overview.tables.find((x) => x.name === name)
              return (
                <g
                  key={name}
                  className={`kg2-node${isTagged ? ' tagged' : ''}${isHl ? ' hl' : ''}${isSel ? ' sel' : ''}`}
                  onMouseDown={(e) => e.stopPropagation()}
                  onClick={(e) => {
                    e.stopPropagation()
                    setSelected(name)
                  }}
                >
                  <circle cx={p.x} cy={p.y} r={22} />
                  <text x={p.x} y={p.y + 4} textAnchor="middle" className="kg2-label mono">
                    {name.length > 12 ? name.slice(0, 11) + '…' : name}
                  </text>
                  {t?.comment_status === 'draft' && <circle cx={p.x + 22} cy={p.y - 22} r={4} className="kg2-draft-dot" />}
                </g>
              )
            })}
          </g>
        </svg>
        <div className="kg2-legend">
          <span><i className="ld fk" />FK</span>
          <span><i className="ld ov" />值重叠</span>
          <span><i className="ld tg" />已打标签</span>
          {highlight && highlight.length > 0 && <span><i className="ld pa" />路由候选</span>}
        </div>
      </div>
      {selTable && (
        <div className="kg2-inspector">
          <div className="kg2-insp-head">
            <span className="mono" style={{ fontWeight: 600 }}>{selTable.name}</span>
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-faint)' }}>{selTable.column_count} 列</span>
            <button className="kg2-insp-x" onClick={() => setSelected(null)}>✕</button>
          </div>
          <div className="kg2-insp-comment">{selTable.comment || <span className="kg2-none">无注释</span>}</div>
          <div className="kg2-insp-tags">
            {selTable.tags.length === 0 && <span className="kg2-none mono">无标签</span>}
            {selTable.tags.map((tg) => (
              <span key={tg.name} className={`tag-chip ${tg.status}`}>{tg.name}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
