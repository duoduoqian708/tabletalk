import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { NODE_H, NODE_W, useGraph, type GraphNode } from '@renderer/store/graph'
import { useKnowledge } from '@renderer/store/knowledge'
import { useSchema } from '@renderer/store/schema'
import { useConnections } from '@renderer/store/connections'
import { useResults } from '@renderer/store/results'
import { useUi } from '@renderer/store/ui'
import { previewTable } from '@renderer/api/schema'
import { NodeInspector } from './NodeInspector'

const MODES: { key: 'browse' | 'build' | 'govern'; label: string }[] = [
  { key: 'browse', label: '浏览' },
  { key: 'build', label: '搭查' },
  { key: 'govern', label: '治理' }
]

interface MenuState {
  x: number
  y: number
  table: string
}

export function GraphCanvas(): React.JSX.Element {
  const { nodes, edges, bounds, viewport, mode, selected, seq, build, setMode, select, setViewport } = useGraph()
  const schema = useSchema((s) => s.data)
  const currentId = useConnections((s) => s.currentId)
  const selectTable = useSchema((s) => s.selectTable)
  const push = useResults((s) => s.push)
  const setMainView = useUi((s) => s.setMainView)
  const setAskDraft = useUi((s) => s.setAskDraft)
  const overview = useKnowledge((s) => s.overview)
  const loadOverview = useKnowledge((s) => s.load)

  const wrapRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ sx: number; sy: number; tx: number; ty: number } | null>(null)
  const [menu, setMenu] = useState<MenuState | null>(null)

  // 连接就绪 → 拉知识概览（供领域色/注释状态）
  useEffect(() => {
    if (currentId) void loadOverview(currentId)
  }, [currentId, loadOverview])

  // schema / overview 变化 → 重建图
  useEffect(() => {
    if (schema) build(schema, overview)
  }, [schema, overview, build])

  // 构建/挂载/内容变化 → fit 全览
  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el || nodes.length === 0) return
    const w = el.clientWidth
    const h = el.clientHeight
    const bw = bounds.maxX - bounds.minX
    const bh = bounds.maxY - bounds.minY
    const scale = Math.min((w - 80) / bw, (h - 80) / bh, 1.25)
    const tx = (w - bw * scale) / 2 - bounds.minX * scale
    const ty = (h - bh * scale) / 2 - bounds.minY * scale
    setViewport({ scale, tx, ty })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq, nodes.length, bounds.minX, bounds.minY, bounds.maxX, bounds.maxY])

  if (!schema || nodes.length === 0) {
    return (
      <div className="gcanvas">
        <div className="g-empty">
          <div className="kicker">no graph</div>
          <div className="big">图谱待生成</div>
          <div className="hint">
            {currentId
              ? '正在读取表结构与外键关系…'
              : '先连接一个数据库，图谱会自动生成。'}
          </div>
        </div>
      </div>
    )
  }

  const edgeHit = (e: { from: string; to: string }): boolean =>
    selected !== null && (e.from === selected || e.to === selected)

  function screenPos(n: GraphNode): { x: number; y: number } {
    return { x: n.x * viewport.scale + viewport.tx, y: n.y * viewport.scale + viewport.ty }
  }

  async function openData(name: string): Promise<void> {
    if (!currentId) return
    selectTable(name)
    try {
      const p = await previewTable(currentId, name)
      push({
        title: `schema · ${name}`,
        name,
        headers: p.columns,
        types: p.types,
        rows: p.rows,
        meta: '结构预览'
      })
    } catch {
      /* 预览失败静默 */
    }
    setMainView('table')
  }

  function askAi(name: string): void {
    setAskDraft(`@${name} `)
    setMainView('table') // 停靠栏常驻，无需切换；保留以防窄屏
  }

  function onBgMouseDown(e: React.MouseEvent): void {
    if (e.button !== 0) return
    select(null)
    setMenu(null)
    dragRef.current = { sx: e.clientX, sy: e.clientY, tx: viewport.tx, ty: viewport.ty }
    document.body.classList.add('g-panning')
  }

  function onBgMouseMove(e: React.MouseEvent): void {
    const d = dragRef.current
    if (!d) return
    setViewport({ ...viewport, tx: d.tx + (e.clientX - d.sx), ty: d.ty + (e.clientY - d.sy) })
  }

  function endDrag(): void {
    dragRef.current = null
    document.body.classList.remove('g-panning')
  }

  function onWheel(e: React.WheelEvent): void {
    const el = wrapRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const mx = e.clientX - rect.left
    const my = e.clientY - rect.top
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
    const next = Math.min(3, Math.max(0.2, viewport.scale * factor))
    const k = next / viewport.scale
    setViewport({
      scale: next,
      tx: mx - (mx - viewport.tx) * k,
      ty: my - (my - viewport.ty) * k
    })
  }

  function fit(): void {
    const el = wrapRef.current
    if (!el || nodes.length === 0) return
    const w = el.clientWidth
    const h = el.clientHeight
    const bw = bounds.maxX - bounds.minX
    const bh = bounds.maxY - bounds.minY
    const scale = Math.min((w - 80) / bw, (h - 80) / bh, 1.25)
    setViewport({
      scale,
      tx: (w - bw * scale) / 2 - bounds.minX * scale,
      ty: (h - bh * scale) / 2 - bounds.minY * scale
    })
  }

  const selNode = nodes.find((n) => n.id === selected) ?? null
  const selPos = selNode ? screenPos(selNode) : null

  return (
    <div
      className="gcanvas"
      ref={wrapRef}
      onMouseDown={onBgMouseDown}
      onMouseMove={onBgMouseMove}
      onMouseUp={endDrag}
      onMouseLeave={endDrag}
      onWheel={onWheel}
      onContextMenu={(e) => e.preventDefault()}
    >
      <svg className="g-svg" viewBox={`0 0 ${wrapRef.current?.clientWidth ?? 800} ${wrapRef.current?.clientHeight ?? 480}`}>
        <defs>
          <pattern id="g-grid" width="26" height="26" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1" fill="rgba(255,255,255,0.05)" />
          </pattern>
          <filter id="g-glow" x="-60%" y="-60%" width="220%" height="220%">
            <feDropShadow dx="0" dy="0" stdDeviation="7" flood-color="#7c8cff" flood-opacity="0.5" />
          </filter>
        </defs>
        <g transform={`translate(${viewport.tx},${viewport.ty}) scale(${viewport.scale})`}>
          <rect
            x={bounds.minX - 60}
            y={bounds.minY - 60}
            width={bounds.maxX - bounds.minX + 120}
            height={bounds.maxY - bounds.minY + 120}
            fill="url(#g-grid)"
          />
          {edges.map((e, i) => {
            const a = nodes.find((n) => n.id === e.from)
            const b = nodes.find((n) => n.id === e.to)
            if (!a || !b) return null
            const ax = a.x + NODE_W
            const ay = a.y + NODE_H / 2
            const bx = b.x
            const by = b.y + NODE_H / 2
            const hot = edgeHit(e)
            const mx = (ax + bx) / 2
            const my = (ay + by) / 2
            return (
              <g key={i}>
                <path
                  d={`M ${ax},${ay} Q ${mx},${my + 18} ${bx},${by}`}
                  fill="none"
                  stroke={hot ? 'rgba(124,140,255,0.6)' : 'rgba(255,255,255,0.12)'}
                  strokeWidth={hot ? 1.4 : 1}
                />
                {e.label && (
                  <text
                    x={mx}
                    y={my + 10}
                    textAnchor="middle"
                    fontSize={9}
                    fill={hot ? '#aeb6ff' : '#5b6472'}
                    stroke="#0a0b0e"
                    strokeWidth={3}
                    paintOrder="stroke"
                    style={{ fontFamily: 'var(--font-mono)' }}
                  >
                    {e.label}
                  </text>
                )}
              </g>
            )
          })}
          {nodes.map((n) => {
            const isSel = n.id === selected
            const dim = mode === 'govern' && n.annotated && !n.pending
            return (
              <g
                key={n.id}
                transform={`translate(${n.x},${n.y})`}
                className={`g-node${isSel ? ' sel' : ''}${n.pending ? ' pend' : ''}${dim ? ' dim' : ''}`}
                style={{ cursor: 'pointer' }}
                onMouseDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation()
                  select(n.id)
                }}
                onDoubleClick={(e) => {
                  e.stopPropagation()
                  void openData(n.name)
                }}
                onContextMenu={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  const rect = wrapRef.current!.getBoundingClientRect()
                  setMenu({ x: e.clientX - rect.left, y: e.clientY - rect.top, table: n.name })
                  select(n.id)
                }}
              >
                {isSel && <rect x={-4} y={-4} width={NODE_W + 8} height={NODE_H + 8} rx={10} fill="rgba(124,140,255,0.08)" />}
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={8}
                  fill={isSel ? '#161826' : '#14161d'}
                  stroke={isSel ? '#7c8cff' : n.pending ? 'rgba(246,173,85,0.5)' : n.annotated ? n.color : 'rgba(255,255,255,0.18)'}
                  strokeWidth={isSel ? 1.6 : 1}
                  strokeDasharray={n.annotated || isSel ? undefined : '4 3'}
                  filter={isSel ? 'url(#g-glow)' : undefined}
                />
                <circle cx={14} cy={17} r={3.5} fill={n.color} />
                <text x={26} y={21} fontSize={12} fontWeight={600} fill={isSel ? '#ffffff' : '#e8eaf0'} style={{ fontFamily: 'var(--font-sans)' }}>
                  {n.name}
                </text>
                <text x={26} y={37} fontSize={9} fill={n.pending ? '#e8b64c' : '#8b93a3'} style={{ fontFamily: 'var(--font-sans)' }}>
                  {n.domain ?? '无标签'} · {n.fields} 字段 · {n.annotated ? '✓' : n.pending ? '⚠ 待确认' : '✗ 未注释'}
                </text>
              </g>
            )
          })}
        </g>
      </svg>

      {/* 模式切换 */}
      <div className="g-mode">
        {MODES.map((m) => (
          <button
            key={m.key}
            className={`g-mode-btn${mode === m.key ? ' on' : ''}`}
            onClick={() => setMode(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>
      <div className="g-hint">
        单击 = 检查器 · <b>双击 = 打开数据</b> · 右键 = 更多
      </div>

      {/* 图例 */}
      <div className="g-legend">
        {Array.from(new Set(nodes.filter((n) => n.domain).map((n) => n.domain))).map((d) => (
          <span key={d}>
            <i style={{ background: nodes.find((n) => n.domain === d)!.color }} />
            {d}
          </span>
        ))}
        {nodes.some((n) => !n.domain) && (
          <span>
            <i style={{ background: '#8b93a3' }} />
            无标签
          </span>
        )}
        <span className="dim">┈ 虚线 = 待注释</span>
      </div>

      {/* 缩放 */}
      <div className="g-zoom">
        <button onClick={() => setViewport({ ...viewport, scale: Math.min(3, viewport.scale * 1.2) })}>＋</button>
        <span>{Math.round(viewport.scale * 100)}%</span>
        <button onClick={() => setViewport({ ...viewport, scale: Math.max(0.2, viewport.scale / 1.2) })}>－</button>
        <button onClick={fit} title="全览">⤢</button>
      </div>

      {/* 检查器 */}
      {selNode && selPos && (
        <NodeInspector
          node={selNode}
          style={{ left: Math.min(selPos.x + NODE_W + 12, (wrapRef.current?.clientWidth ?? 800) - 250), top: Math.max(selPos.y, 8) }}
          onOpenData={() => void openData(selNode.name)}
          onAskAi={() => askAi(selNode.name)}
          onClose={() => select(null)}
        />
      )}

      {/* 右键菜单 */}
      {menu && (
        <div
          className="g-menu"
          style={{ left: Math.min(menu.x, (wrapRef.current?.clientWidth ?? 800) - 160), top: menu.y }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          <button onClick={() => { setMenu(null); void openData(menu.table) }}>▤ 打开数据</button>
          <button onClick={() => { setMenu(null); askAi(menu.table) }}>◎ 问 AI 这张表</button>
          {mode === 'govern' && (
            <button disabled title="P5 实现">✓ 治理操作…</button>
          )}
          <button onClick={() => setMenu(null)}>取消</button>
        </div>
      )}
    </div>
  )
}
