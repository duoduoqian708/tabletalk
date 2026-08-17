import { useMemo } from 'react'
import type { KnowledgeOverview } from '@renderer/api/types'

interface Props {
  overview: KnowledgeOverview
  highlight?: string[] // 候选表（路由预览）
}

export function KnowledgeGraph({ overview, highlight }: Props): React.JSX.Element {
  const fkEdges = overview.graph.edges.filter((e) => e.kind === 'fk')
  const tableNames = overview.tables.map((t) => t.name)
  const tagged = new Set(overview.tables.filter((t) => t.tags.length > 0).map((t) => t.name))
  const hl = new Set(highlight ?? [])

  const { nodes, edges } = useMemo(() => {
    const W = 520
    const H = 360
    const CX = W / 2
    const CY = H / 2
    const R = Math.min(W, H) / 2 - 46
    const names = tableNames
    const pos: Record<string, [number, number]> = {}
    names.forEach((n, i) => {
      const a = (i * 2 * Math.PI) / Math.max(1, names.length) - Math.PI / 2
      pos[n] = [CX + Math.cos(a) * R, CY + Math.sin(a) * R]
    })
    return { nodes: pos, edges: fkEdges }
  }, [tableNames.join(','), fkEdges.length]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="kg-wrap">
      <svg className="kg-svg" viewBox="0 0 520 360">
        {edges.map((e, i) => {
          const a = nodes[e.from]
          const b = nodes[e.to]
          if (!a || !b) return null
          const hot = (hl.has(e.from) || hl.has(e.to)) && (hl.size > 0)
          return (
            <line
              key={i}
              className={`kg-edge${hot ? ' hot' : ''}`}
              x1={a[0]}
              y1={a[1]}
              x2={b[0]}
              y2={b[1]}
            />
          )
        })}
        {Object.entries(nodes).map(([name, [x, y]]) => {
          const isTagged = tagged.has(name)
          const isHl = hl.has(name)
          return (
            <g key={name} className={`kg-node${isTagged ? ' tagged' : ''}${isHl ? ' hl' : ''}`}>
              <title>{name}{overview.tables.find((t) => t.name === name)?.tags.length ? ` · 标签: ${overview.tables.find((t) => t.name === name)?.tags.map((t) => t.name).join('/')}` : ''}</title>
              <circle cx={x} cy={y} r={20} />
              <text x={x} y={y + 4} textAnchor="middle" className="kg-label">
                {name.length > 10 ? name.slice(0, 9) + '…' : name}
              </text>
            </g>
          )
        })}
      </svg>
      <div className="kg-legend">
        <span><i className="ld on" />已打标签</span>
        <span><i className="ld dim" />外键</span>
        {highlight && highlight.length > 0 && <span><i className="ld path" />路由候选</span>}
      </div>
    </div>
  )
}
