import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { NODE_H, NODE_W, buildJoinSkeleton, useGraph, type GraphNode } from '@renderer/store/graph'
import { useKnowledge } from '@renderer/store/knowledge'
import { useSchema } from '@renderer/store/schema'
import { useConnections } from '@renderer/store/connections'
import { useResults } from '@renderer/store/results'
import { useUi } from '@renderer/store/ui'
import { previewTable } from '@renderer/api/schema'
import { runQuery } from '@renderer/api/query'
import { toastMsg } from '@renderer/utils/toast'
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
  qid?: string
}

/** 治理模式统计条：知识健康度 + 批量操作。 */
function GovernanceBar(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const overview = useKnowledge((s) => s.overview)
  const { annotateTags, confirmAll } = useKnowledge()
  const [busy, setBusy] = useState(false)
  const draftCount = overview?.draft_count ?? 0
  const tagDraft = overview?.tag_draft_count ?? 0
  const unannotated = overview?.tables.filter((t) => !t.comment && t.comment_status !== 'confirmed').length ?? 0

  async function run(fn: () => Promise<void>): Promise<void> {
    if (!currentId) return
    setBusy(true)
    try {
      await fn()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="g-gov-bar">
      <span className="ggb-item">待确认注释 <b>{draftCount}</b></span>
      <span className="ggb-item">待确认标签 <b>{tagDraft}</b></span>
      <span className="ggb-item">未注释表 <b>{unannotated}</b></span>
      <span className="spacer" />
      <button className="ggb-btn" disabled={busy} onClick={() => void run(() => annotateTags(currentId!))}>AI 生成标签</button>
      <button className="ggb-btn" disabled={busy || (draftCount === 0 && tagDraft === 0)} onClick={() => void run(() => confirmAll(currentId!))}>整库一键确认</button>
    </div>
  )
}

/** 搭查（积木台）工具条：已选表 chips + 生成 JOIN 骨架。 */
function BuildBar({ onGenerate, disabled }: { onGenerate: () => void; disabled: boolean }): React.JSX.Element {
  const buildSel = useGraph((s) => s.buildSel)
  const toggleBuild = useGraph((s) => s.toggleBuild)
  const clearBuild = useGraph((s) => s.clearBuild)
  const nodeColor = useGraph((s) => s.nodes.find((n) => n.id === buildSel[0])?.color)

  return (
    <div className="g-build-bar">
      <span className="gbb-label">积木台</span>
      {buildSel.length === 0 ? (
        <span className="gbb-empty">点击表节点加入搭查集合（可多选），再生成 JOIN 骨架</span>
      ) : (
        buildSel.map((t) => (
          <span key={t} className="gbb-chip" style={{ borderColor: nodeColor }}>
            {t}
            <button className="gbb-x" title="移出" onClick={() => toggleBuild(t)}>✕</button>
          </span>
        ))
      )}
      <span className="spacer" />
      {buildSel.length > 0 && (
        <button className="ggb-btn" onClick={clearBuild}>清空</button>
      )}
      <button className="ggb-btn pri" disabled={disabled || buildSel.length < 2} onClick={onGenerate}>
        生成 JOIN 骨架
      </button>
    </div>
  )
}

export function GraphCanvas(): React.JSX.Element {
  const { nodes, edges, bounds, viewport, mode, selected, seq, build, setMode, select, setViewport,
    buildSel, toggleBuild, queryNodes, addQueryNode, removeQueryNode } = useGraph()
  const schema = useSchema((s) => s.data)
  const currentId = useConnections((s) => s.currentId)
  const selectTable = useSchema((s) => s.selectTable)
  const push = useResults((s) => s.push)
  const setAskDraft = useUi((s) => s.setAskDraft)
  const setMainView = useUi((s) => s.setMainView)
  const overview = useKnowledge((s) => s.overview)
  const loadOverview = useKnowledge((s) => s.load)

  const wrapRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ sx: number; sy: number; tx: number; ty: number } | null>(null)
  const [menu, setMenu] = useState<MenuState | null>(null)
  const [skeleton, setSkeleton] = useState<{ sql: string; missing: string[]; tables: string[] } | null>(null)
  const [skelDraft, setSkelDraft] = useState('')
  const [running, setRunning] = useState(false)
  // 图谱检索：选中节点的 k 跳探索（1/2/3 跳圈定子图）
  const [hop, setHop] = useState(2)

  // 从 FK 边 BFS 算选中节点的 k 跳邻居（"从图上找一步两步"）
  const hopSet = useMemo(() => {
    if (!selected || mode !== 'browse') return null
    const adj = new Map<string, string[]>()
    for (const e of edges) {
      adj.set(e.from, [...(adj.get(e.from) ?? []), e.to])
      adj.set(e.to, [...(adj.get(e.to) ?? []), e.from])
    }
    const reach = new Set<string>([selected])
    let frontier = [selected]
    for (let h = 0; h < hop; h++) {
      const next: string[] = []
      for (const t of frontier) {
        for (const nb of adj.get(t) ?? []) {
          if (!reach.has(nb)) {
            reach.add(nb)
            next.push(nb)
          }
        }
      }
      frontier = next
    }
    return reach
  }, [selected, edges, hop, mode])
  const hopCounts = useMemo(() => {
    if (!selected) return null
    const adj = new Map<string, string[]>()
    for (const e of edges) {
      adj.set(e.from, [...(adj.get(e.from) ?? []), e.to])
      adj.set(e.to, [...(adj.get(e.to) ?? []), e.from])
    }
    const countAt = (h: number): number => {
      const reach = new Set<string>([selected])
      let frontier = [selected]
      for (let i = 0; i < h; i++) {
        const next: string[] = []
        for (const t of frontier) {
          for (const nb of adj.get(t) ?? []) {
            if (!reach.has(nb)) { reach.add(nb); next.push(nb) }
          }
        }
        frontier = next
      }
      return reach.size - 1
    }
    return { h1: countAt(1), h2: countAt(2), h3: countAt(3) }
  }, [selected, edges])

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

  /** 搭查：基于已选表 + FK 生成 JOIN 骨架 SQL。 */
  function generateSkeleton(): void {
    if (!schema || buildSel.length < 2) return
    const { steps, missing } = buildJoinSkeleton(buildSel, schema.foreign_keys)
    const head = `SELECT\n  ${buildSel.map((t) => `${t}.*`).join(',\n  ')}\nFROM ${buildSel[0]}`
    const sql = steps.length > 0 ? `${head}\n${steps.map((s) => '  ' + s.clause).join('\n')}\nLIMIT 100;` : `${head}\nLIMIT 100;`
    setSkeleton({ sql, missing, tables: buildSel })
    setSkelDraft(sql)
  }

  /** 搭查：执行骨架 SQL（过闸门，manual 来源入审计）。 */
  async function runSkeletonSql(): Promise<void> {
    const sql = skelDraft.trim()
    if (!sql || !currentId || !skeleton) return
    setRunning(true)
    try {
      const r = await runQuery({ connectionId: currentId, sql, origin: 'manual' })
      if (r.verdict === 'allow') {
        const title = `${skeleton.tables.join(' ⋈ ')}`
        push({
          title: `搭查 · ${title}`,
          name: sql.slice(0, 20),
          headers: r.columns ?? [],
          types: r.types ?? [],
          rows: (r.rows ?? []) as unknown[][],
          meta: `${r.truncated ? '截断' : ''}${r.elapsed_ms}ms`.trim()
        })
        addQueryNode({ title, sql, tables: skeleton.tables })
        toastMsg('查询已执行并沉淀为查询节点')
        setSkeleton(null)
        setMainView('table')
      } else if (r.verdict === 'review') {
        toastMsg('这是写操作：请在 AI 区确认执行（搭查只做只读查询）')
      } else if (r.verdict === 'block') {
        toastMsg(`已拦截：${r.reason}`)
      }
    } catch (e) {
      toastMsg(`执行失败：${(e as Error).message}`)
    } finally {
      setRunning(false)
    }
  }

  /** 搭查：把骨架发给 AI 补条件。 */
  function askAiFill(): void {
    if (!skeleton) return
    setAskDraft(`基于这个 SQL 骨架帮我补全查询条件（保持表与 JOIN 不变）：\n${skelDraft}`)
    setSkeleton(null)
    setMainView('table')
  }

  /** 重跑查询节点。 */
  async function rerunQueryNode(qid: string): Promise<void> {
    const q = queryNodes.find((x) => x.id === qid)
    if (!q || !currentId) return
    setRunning(true)
    try {
      const r = await runQuery({ connectionId: currentId, sql: q.sql, origin: 'manual' })
      if (r.verdict === 'allow') {
        push({
          title: `搭查 · ${q.title}`,
          name: q.sql.slice(0, 20),
          headers: r.columns ?? [],
          types: r.types ?? [],
          rows: (r.rows ?? []) as unknown[][],
          meta: `${r.truncated ? '截断' : ''}${r.elapsed_ms}ms`.trim()
        })
        setMainView('table')
      } else if (r.verdict === 'review') {
        toastMsg('写操作需确认：请在 AI 区处理')
      } else if (r.verdict === 'block') {
        toastMsg(`已拦截：${r.reason}`)
      }
    } catch (e) {
      toastMsg(`执行失败：${(e as Error).message}`)
    } finally {
      setRunning(false)
    }
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
            // 图上检索：浏览模式选中节点 → k 跳圈定子图，圈外淡化
            const outHop = mode === 'browse' && hopSet !== null && !hopSet.has(n.id) && !isSel
            const inBuild = mode === 'build' && buildSel.includes(n.id)
            const buildIdx = buildSel.indexOf(n.id)
            return (
              <g
                key={n.id}
                transform={`translate(${n.x},${n.y})`}
                className={`g-node${isSel ? ' sel' : ''}${n.pending ? ' pend' : ''}${dim ? ' dim' : ''}${inBuild ? ' in-build' : ''}${outHop ? ' out-hop' : ''}`}
                style={{ cursor: 'pointer' }}
                onMouseDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation()
                  if (mode === 'build') {
                    toggleBuild(n.name) // 积木台：单击 = 加入/移出搭查集合
                    return
                  }
                  select(n.id)
                  selectTable(n.name) // 同步 schema 选中 → 上下文筹码联动
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
                {isSel && mode !== 'build' && <rect x={-4} y={-4} width={NODE_W + 8} height={NODE_H + 8} rx={10} fill="rgba(124,140,255,0.08)" />}
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={8}
                  fill={isSel ? '#161826' : inBuild ? 'rgba(110,231,183,0.08)' : '#14161d'}
                  stroke={inBuild ? '#6ee7b7' : isSel ? '#7c8cff' : n.pending ? 'rgba(246,173,85,0.5)' : n.annotated ? n.color : 'rgba(255,255,255,0.18)'}
                  strokeWidth={inBuild ? 1.6 : isSel ? 1.6 : 1}
                  strokeDasharray={n.annotated || isSel || inBuild ? undefined : '4 3'}
                  filter={isSel && mode !== 'build' ? 'url(#g-glow)' : undefined}
                />
                {inBuild && (
                  <g>
                    <circle cx={NODE_W - 12} cy={12} r={7} fill="#6ee7b7" />
                    <text x={NODE_W - 12} y={15} textAnchor="middle" fontSize={8} fontWeight={700} fill="#0a0b0e" style={{ fontFamily: 'var(--font-mono)' }}>
                      {buildIdx + 1}
                    </text>
                  </g>
                )}
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
          {queryNodes.map((q, i) => {
            const x = bounds.maxX + 70
            const y = bounds.minY + i * 66
            return (
              <g
                key={q.id}
                transform={`translate(${x},${y})`}
                className="g-qnode"
                style={{ cursor: 'pointer' }}
                onMouseDown={(e) => e.stopPropagation()}
                onClick={(e) => e.stopPropagation()}
                onDoubleClick={(e) => {
                  e.stopPropagation()
                  void rerunQueryNode(q.id)
                }}
                onContextMenu={(e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  const rect = wrapRef.current!.getBoundingClientRect()
                  setMenu({ x: e.clientX - rect.left, y: e.clientY - rect.top, table: q.title, qid: q.id })
                }}
              >
                <title>{q.sql}</title>
                <rect width={178} height={54} rx={8} fill="rgba(124,140,255,0.08)" stroke="rgba(124,140,255,0.6)" strokeWidth={1.2} strokeDasharray="5 4" />
                <text x={10} y={18} fontSize={10.5} fontWeight={600} fill="#aeb6ff" style={{ fontFamily: 'var(--font-sans)' }}>▤ {q.title}</text>
                <text x={10} y={34} fontSize={8.5} fill="#8b93a3" style={{ fontFamily: 'var(--font-mono)' }}>{(q.sql.split('\n')[0] ?? '').slice(0, 30)}{q.sql.split('\n').length > 1 ? '…' : ''}</text>
                <text x={10} y={46} fontSize={8} fill="#5b6472" style={{ fontFamily: 'var(--font-sans)' }}>双击重跑 · 右键管理</text>
              </g>
            )
          })}
        </g>
      </svg>

      {/* 模式切换 */}
      <div className="g-mode" onMouseDown={(e) => e.stopPropagation()}>
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

      {/* 图上检索：k 跳探索控件（浏览模式选中节点时） */}
      {mode === 'browse' && selected && hopSet && hopCounts && (
        <div className="g-hop-bar" onMouseDown={(e) => e.stopPropagation()}>
          <span className="ghb-label">图上检索</span>
          {[1, 2, 3].map((h) => (
            <button
              key={h}
              className={`ghb-btn${hop === h ? ' on' : ''}`}
              onClick={() => setHop(h)}
              title={`圈定 ${h} 跳内关联表`}
            >
              {h} 跳<span className="ghb-n">{h === 1 ? hopCounts.h1 : h === 2 ? hopCounts.h2 : hopCounts.h3}</span>
            </button>
          ))}
          <span className="ghb-hint">圈内 = 可达关联（路由同款 2 跳）· 圈外淡化</span>
        </div>
      )}

      {/* 搭查（积木台）工具条 */}
      {mode === 'build' && <BuildBar onGenerate={generateSkeleton} disabled={running} />}

      {/* 治理模式统计条 */}
      {mode === 'govern' && <div onMouseDown={(e) => e.stopPropagation()}><GovernanceBar /></div>}
      <div className="g-hint">
        {mode === 'build'
          ? '单击 = 加入搭查集合 · <b>双击 = 打开数据</b> · 右键 = 更多'
          : '单击 = 检查器 · <b>双击 = 打开数据</b> · 右键 = 更多'}
      </div>

      {/* JOIN 骨架面板 */}
      {skeleton && (
        <div className="g-skel-panel">
          <div className="gsk-h">
            JOIN 骨架
            {skeleton.missing.length > 0 && (
              <span className="gsk-warn"> · 无法连通：{skeleton.missing.join(', ')}（该表将缺失）</span>
            )}
            <span className="spacer" />
            <button className="gi-x" onClick={() => setSkeleton(null)}>✕</button>
          </div>
          <textarea
            className="gsk-ta mono"
            value={skelDraft}
            onChange={(e) => setSkelDraft(e.target.value)}
            rows={9}
            spellCheck={false}
          />
          <div className="gsk-acts">
            <span className="gsk-tip">改 SQL 后：直接运行（只读过闸门）或交给 AI 补条件</span>
            <button className="ggb-btn" onClick={askAiFill}>◎ 让 AI 补条件</button>
            <button className="ggb-btn pri" disabled={running || !skelDraft.trim()} onClick={() => void runSkeletonSql()}>
              ▶ 直接运行
            </button>
          </div>
        </div>
      )}

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
      <div className="g-zoom" onMouseDown={(e) => e.stopPropagation()}>
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
          {menu.qid ? (
            <>
              <button onClick={() => { setMenu(null); void rerunQueryNode(menu.qid!) }}>▶ 重跑查询</button>
              <button onClick={() => { setMenu(null); removeQueryNode(menu.qid!) }}>✕ 删除查询节点</button>
              <button onClick={() => setMenu(null)}>取消</button>
            </>
          ) : (
            <>
              <button onClick={() => { setMenu(null); void openData(menu.table) }}>▤ 打开数据</button>
              <button onClick={() => { setMenu(null); askAi(menu.table) }}>◎ 问 AI 这张表</button>
              {mode === 'build' && (
                <button onClick={() => { setMenu(null); toggleBuild(menu.table) }}>
                  {buildSel.includes(menu.table) ? '⊟ 移出搭查集合' : '⊞ 加入搭查集合'}
                </button>
              )}
              {mode === 'govern' && (
                <button disabled title="治理操作请用检查器">✓ 治理操作（检查器）</button>
              )}
              <button onClick={() => setMenu(null)}>取消</button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
