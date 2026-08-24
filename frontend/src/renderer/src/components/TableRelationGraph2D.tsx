import { useEffect, useMemo, useReducer, useRef, useState } from 'react'
import type { GraphEdge } from '@renderer/api/types'
import { useI18n } from '@renderer/store/i18n'

/* ═══════════════════════════════════════════════════════════════
   TableRelationGraph2D — 手写 SVG 可编辑表关系图（spec §6 右栏）
   领域聚类初始布局 · 节点拖拽 · 字段级边标签 · 拖线弹面板连线 · 平移缩放
   无第三方图库；坐标受控（layout prop）+ 本地乐观覆盖，松手回传宿主持久化
   ═══════════════════════════════════════════════════════════════ */

export interface Trg2dTable {
  name: string
  tags?: string[]
  excluded?: boolean
}

export interface Trg2dAddEdge {
  from_table: string
  from_col: string
  to_table: string
  to_col: string
  cardinality: 'n:1' | '1:1'
}

export interface Trg2dDeleteEdge {
  from_table: string
  from_col?: string
  to_table: string
  to_col?: string
  kind: string
  /** draft 边宿主据此分流（llm+draft → 拒绝/确认草案，其余 → 删边） */
  status?: string
}

export type Trg2dPoint = { x: number; y: number }
export type Trg2dLayout = Record<string, Trg2dPoint>

interface Props {
  tables: Trg2dTable[]
  /** 含 fk/llm/user 边；llm 未确认边由宿主映射为 kind:'llm' + status:'draft' */
  edges: GraphEdge[]
  /** 连线面板两端字段下拉的数据源：表名 → 列名列表 */
  columnsByTable: Record<string, string[]>
  onAddEdge: (e: Trg2dAddEdge) => Promise<void>
  onDeleteEdge: (e: Trg2dDeleteEdge) => Promise<void>
  /** 选中 draft 边的确认操作（宿主缺省不显示 ✓ 按钮） */
  onConfirmEdge?: (e: Trg2dDeleteEdge) => Promise<void>
  /** 拖拽结束回调一次（宿主负责持久化）；值为全部表的最终坐标快照 */
  onLayoutChange: (layout: Trg2dLayout) => void
  /** 受控坐标（如 payload.layout 回读）；本地拖拽在其上做乐观覆盖 */
  layout?: Trg2dLayout
  /** 标签取色钩子（缺省用内置确定性 8 色板）；宿主可传审查页同源映射保持一致 */
  getTagColor?: (tag: string) => string | undefined
  className?: string
}

/* ── 几何常量 ── */
const NODE_W = 148
const NODE_H = 34
const ROW_GAP = 12            // 组内节点纵向间距
const GROUP_PAD = 46          // 组间留白（领域分区间的空白）
const EDGE_GAP = 4            // 线与节点边框的间隙
const LINK_BAND = 10          // 节点边缘"连线感应带"宽度
const HIT_W = 14              // 边命中区宽度
const ZOOM_MIN = 0.35
const ZOOM_MAX = 2.5
const UNTAGGED = '\u0000'     // 无标签桶排序键（保证排最后）
const TAG_PALETTE = ['#63c8ff', '#35d99a', '#ffb454', '#b18cff', '#ff6b81', '#2ee6a8', '#f472b6', '#fbbf24']

function hashStr(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return Math.abs(h)
}

/** 内置确定性标签色（宿主未提供 getTagColor 时兜底） */
function paletteColor(tag: string): string {
  return TAG_PALETTE[hashStr(tag) % TAG_PALETTE.length]
}

/**
 * 领域聚类初始布局：按首标签分组 → 组名排序（无标签恒最后）→ 组块按列网格摆放，
 * 组内竖排一列；同输入同输出（纯确定性，不依赖 DOM/时间）。
 */
function computeInitialLayout(tables: Trg2dTable[]): Trg2dLayout {
  const out: Trg2dLayout = {}
  const groups = new Map<string, string[]>()
  for (const t of [...tables].sort((a, b) => (a.name < b.name ? -1 : 1))) {
    const g = t.tags?.[0] ?? UNTAGGED
    const arr = groups.get(g)
    if (arr) arr.push(t.name)
    else groups.set(g, [t.name])
  }
  const keys = [...groups.keys()].sort((a, b) => {
    if (a === UNTAGGED) return 1
    if (b === UNTAGGED) return -1
    return a < b ? -1 : 1
  })
  if (!keys.length) return out
  const cols = Math.max(1, Math.ceil(Math.sqrt(keys.length)))
  const cellW = NODE_W + GROUP_PAD * 2
  // 每行高度 = 行内最高组块；组块在行内垂直居中
  const blocks = keys.map((k) => ({ key: k, names: groups.get(k)! }))
  for (let r = 0; r * cols < blocks.length; r++) {
    const row = blocks.slice(r * cols, (r + 1) * cols)
    const rowH = Math.max(...row.map((b) => b.names.length * (NODE_H + ROW_GAP) - ROW_GAP))
    let rowTop = 0
    for (let rr = 0; rr < r; rr++) {
      rowTop += Math.max(...blocks.slice(rr * cols, (rr + 1) * cols).map((b) => b.names.length * (NODE_H + ROW_GAP) - ROW_GAP)) + GROUP_PAD * 2
    }
    row.forEach((b, c) => {
      const blockH = b.names.length * (NODE_H + ROW_GAP) - ROW_GAP
      const top = rowTop + GROUP_PAD + (rowH - blockH) / 2
      b.names.forEach((name, i) => {
        out[name] = { x: c * cellW + GROUP_PAD + NODE_W / 2, y: top + i * (NODE_H + ROW_GAP) + NODE_H / 2 }
      })
    })
  }
  return out
}

/** 自中心出发的方向线与节点外接矩形的交点（边界裁剪），再外推 gap 像素留白 */
function clipToRect(cx: number, cy: number, dx: number, dy: number, gap: number): Trg2dPoint {
  const hw = NODE_W / 2 + gap
  const hh = NODE_H / 2 + gap
  const adx = Math.abs(dx), ady = Math.abs(dy)
  const t = Math.min(adx > 1e-6 ? hw / adx : Infinity, ady > 1e-6 ? hh / ady : Infinity)
  return { x: cx + dx * t, y: cy + dy * t }
}

/** 表名截断（超出节点宽度的字符数直接省略，避免测 DOM） */
function truncName(s: string): string {
  return s.length > 18 ? s.slice(0, 17) + '…' : s
}

interface EdgeGeom {
  key: string
  edge: GraphEdge
  draft: boolean
  d: string
  mid: Trg2dPoint
  angle: number
  /** 近垂直边标签保持水平、贴线右侧放置（避免竖排长条） */
  horizontal: boolean
  labelW: number
  /** 二次贝塞尔均匀采样折线（命中检测用，弦距近似在弓高下会漏检） */
  pts: Trg2dPoint[]
}

export function TableRelationGraph2D({
  tables, edges, columnsByTable, onAddEdge, onDeleteEdge, onConfirmEdge, onLayoutChange, layout, getTagColor, className,
}: Props): React.JSX.Element {
  const { t } = useI18n()
  const wrapRef = useRef<HTMLDivElement>(null)
  const viewGRef = useRef<SVGGElement | null>(null)
  const [, bump] = useReducer((x: number) => x + 1, 0)

  /* ── 视图变换（平移/缩放）：ref 为唯一事实来源，手势期间命令式写 DOM，结束时 bump 刷新覆盖层 ── */
  const viewRef = useRef({ tx: 30, ty: 30, k: 1 })
  const applyView = (): void => {
    const v = viewRef.current
    viewGRef.current?.setAttribute('transform', `translate(${v.tx} ${v.ty}) scale(${v.k})`)
  }

  /* ── 坐标解析：拖拽临时位 > 已提交覆盖 > 宿主 layout > 确定性初始布局 ── */
  const initialLayout = useMemo(() => computeInitialLayout(tables), [tables])
  const [overrides, setOverrides] = useState<Trg2dLayout>({})
  const [dragPos, setDragPos] = useState<Trg2dLayout>({})
  const posOf = (name: string): Trg2dPoint =>
    dragPos[name] ?? overrides[name] ?? layout?.[name] ?? initialLayout[name] ?? { x: 60, y: 60 }
  const posMap = useMemo(() => {
    const m: Trg2dLayout = {}
    for (const tb of tables) m[tb.name] = posOf(tb.name)
    return m
  }, [tables, initialLayout, layout, overrides, dragPos]) // eslint-disable-line react-hooks/exhaustive-deps

  const excludedSet = useMemo(() => new Set(tables.filter((tb) => tb.excluded).map((tb) => tb.name)), [tables])
  const tableSet = useMemo(() => new Set(tables.map((tb) => tb.name)), [tables])

  /* ── 可见边几何：端点缺失/涉及排除表的边不渲染 ── */
  const edgeGeoms = useMemo<EdgeGeom[]>(() => {
    const out: EdgeGeom[] = []
    for (const e of edges) {
      if (!tableSet.has(e.from) || !tableSet.has(e.to)) continue
      if (excludedSet.has(e.from) || excludedSet.has(e.to)) continue
      const a = posMap[e.from], b = posMap[e.to]
      if (!a || !b) continue
      const dx = b.x - a.x, dy = b.y - a.y
      const len = Math.hypot(dx, dy) || 1
      const pa = clipToRect(a.x, a.y, dx, dy, EDGE_GAP)
      const pb = clipToRect(b.x, b.y, -dx, -dy, EDGE_GAP)
      // 轻曲线：中点沿法线上抬 7% 长度，避免完全平行边重叠
      const nx = -dy / len, ny = dx / len
      const bow = len * 0.07
      const cx = (a.x + b.x) / 2 + nx * bow, cy = (a.y + b.y) / 2 + ny * bow
      const mid = { x: (pa.x + pb.x + 2 * cx) / 4, y: (pa.y + pb.y + 2 * cy) / 4 }
      let angle = (Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180) / Math.PI
      const horizontal = Math.abs(angle) > 55   // 近垂直边：标签水平放置
      if (!horizontal && (angle > 90 || angle < -90)) angle += 180   // 沿线标签保持正立
      const fc = e.from_col || '*', tc = e.to_col || '*'
      const label = `${e.from}.${fc} ─ ${e.cardinality === '1:1' ? '1:1' : 'n:1'} ─ ${e.to}.${tc}`
      // 命中折线采样：B(t)=(1-t)²P0+2(1-t)tC+t²P2，16 段足够 7px 阈值
      const pts: Trg2dPoint[] = []
      for (let i = 0; i <= 16; i++) {
        const tt = i / 16, u = 1 - tt
        pts.push({ x: u * u * pa.x + 2 * u * tt * cx + tt * tt * pb.x, y: u * u * pa.y + 2 * u * tt * cy + tt * tt * pb.y })
      }
      out.push({
        key: `${e.from}|${e.from_col ?? ''}|${e.to}|${e.to_col ?? ''}|${e.kind}|${e.status ?? 'confirmed'}`,
        edge: e, draft: e.status === 'draft',
        d: `M ${pa.x} ${pa.y} Q ${cx} ${cy} ${pb.x} ${pb.y}`,
        mid, angle, horizontal, labelW: label.length * 5.8 + 14, pts,
      })
    }
    return out
  }, [edges, posMap, tableSet, excludedSet])

  /* ── 标签配色 ── */
  const colorOf = (tb: Trg2dTable): string => {
    const tag = tb.tags?.[0]
    if (!tag) return '#5a6a7e'
    return getTagColor?.(tag) ?? paletteColor(tag)
  }

  /* ── 交互状态 ── */
  const [selKey, setSelKey] = useState<string | null>(null)
  const [hoverName, setHoverName] = useState<string | null>(null)
  const [linkZone, setLinkZone] = useState(false)
  const [link, setLink] = useState<{ from: string; cur: Trg2dPoint; target: string | null } | null>(null)
  const [panel, setPanel] = useState<{
    from_table: string; to_table: string; anchor: Trg2dPoint
    from_col: string; to_col: string; cardinality: 'n:1' | '1:1'; busy: boolean; error: string
  } | null>(null)

  type Gesture =
    | { type: 'pan'; sx: number; sy: number; tx: number; ty: number }
    | { type: 'move'; name: string; gx: number; gy: number; moved: boolean }
    | { type: 'link'; from: string }
    | null
  const gestureRef = useRef<Gesture>(null)
  const rectRef = useRef<DOMRect | null>(null)

  const screenToWorld = (clientX: number, clientY: number): Trg2dPoint => {
    const v = viewRef.current
    const r = rectRef.current ?? wrapRef.current?.getBoundingClientRect()
    if (!r) return { x: 0, y: 0 }
    return { x: (clientX - r.left - v.tx) / v.k, y: (clientY - r.top - v.ty) / v.k }
  }
  const worldToScreen = (p: Trg2dPoint): Trg2dPoint => {
    const v = viewRef.current
    return { x: p.x * v.k + v.tx, y: p.y * v.k + v.ty }
  }

  const hitNode = (p: Trg2dPoint): string | null => {
    let best: string | null = null
    for (const tb of tables) {
      if (excludedSet.has(tb.name)) continue
      const c = posMap[tb.name]
      if (!c) continue
      if (Math.abs(p.x - c.x) <= NODE_W / 2 + 2 && Math.abs(p.y - c.y) <= NODE_H / 2 + 2) best = tb.name
    }
    return best
  }

  /** 命中节点边缘感应带（外扩 LINK_BAND 但不含内核）→ 连线起点；中心区域 → 移动 */
  const inLinkBand = (name: string, p: Trg2dPoint): boolean => {
    const c = posMap[name]
    if (!c) return false
    const dx = Math.abs(p.x - c.x), dy = Math.abs(p.y - c.y)
    const outer = dx <= NODE_W / 2 + LINK_BAND && dy <= NODE_H / 2 + LINK_BAND
    const inner = dx < NODE_W / 2 - LINK_BAND && dy < NODE_H / 2 - LINK_BAND
    return outer && !inner
  }

  const hitEdge = (p: Trg2dPoint): string | null => {
    let best: string | null = null
    let bd = (HIT_W / 2) ** 2
    for (const g of edgeGeoms) {
      const pts = g.pts
      for (let i = 0; i < pts.length - 1; i++) {
        const ax = pts[i].x, ay = pts[i].y
        const mx = pts[i + 1].x - ax, my = pts[i + 1].y - ay
        const l2 = mx * mx + my * my || 1
        let tt = ((p.x - ax) * mx + (p.y - ay) * my) / l2
        tt = Math.max(0, Math.min(1, tt))
        const d = (p.x - ax - mx * tt) ** 2 + (p.y - ay - my * tt) ** 2
        if (d < bd) { bd = d; best = g.key }
      }
    }
    return best
  }

  /* ── 指针状态机：down 分诊（移动/连线/选边/平移）→ move 更新 → up 提交一次 ── */
  const onPointerDown = (ev: React.PointerEvent<SVGSVGElement>): void => {
    if (ev.button !== 0) return
    rectRef.current = wrapRef.current?.getBoundingClientRect() ?? null
    // 合成事件/无活动指针时 setPointerCapture 会抛 NotFoundError，静默降级为普通事件流
    try { ev.currentTarget.setPointerCapture(ev.pointerId) } catch { /* ignore */ }
    const p = screenToWorld(ev.clientX, ev.clientY)
    const name = hitNode(p)
    if (name) {
      if (inLinkBand(name, p)) {
        gestureRef.current = { type: 'link', from: name }
        setLink({ from: name, cur: p, target: null })
        setSelKey(null)
      } else {
        const c = posMap[name]
        gestureRef.current = { type: 'move', name, gx: c.x - p.x, gy: c.y - p.y, moved: false }
      }
      return
    }
    const ek = hitEdge(p)
    if (ek) { setSelKey(ek); return }
    setSelKey(null)
    const v = viewRef.current
    gestureRef.current = { type: 'pan', sx: ev.clientX, sy: ev.clientY, tx: v.tx, ty: v.ty }
    ev.currentTarget.classList.add('is-panning')
  }

  const onPointerMove = (ev: React.PointerEvent<SVGSVGElement>): void => {
    const g = gestureRef.current
    if (!g) {
      // 空闲态：更新悬停目标与连线感应带光标提示
      rectRef.current = null
      const p = screenToWorld(ev.clientX, ev.clientY)
      const name = hitNode(p)
      const zone = name != null && inLinkBand(name, p)
      if (name !== hoverName) setHoverName(name)
      if (zone !== linkZone) setLinkZone(zone)
      return
    }
    const p = screenToWorld(ev.clientX, ev.clientY)
    if (g.type === 'move') {
      const next = { ...dragPos, [g.name]: { x: p.x + g.gx, y: p.y + g.gy } }
      if (!g.moved) g.moved = true
      setDragPos(next)
    } else if (g.type === 'link') {
      const target = hitNode(p)
      setLink((cur) => cur && { ...cur, cur: p, target: target && target !== g.from ? target : null })
    } else if (g.type === 'pan') {
      const v = viewRef.current
      v.tx = g.tx + (ev.clientX - g.sx)
      v.ty = g.ty + (ev.clientY - g.sy)
      applyView()
    }
  }

  const endGesture = (ev: React.PointerEvent<SVGSVGElement>): void => {
    const g = gestureRef.current
    gestureRef.current = null
    ev.currentTarget.classList.remove('is-panning')
    if (!g) return
    if (g.type === 'move') {
      if (g.moved) {
        // 松手一次性提交：合并临时位 → 覆盖层，并把全量快照交给宿主持久化
        setOverrides((prev) => ({ ...prev, ...dragPos }))
        setDragPos({})
        const snapshot: Trg2dLayout = {}
        for (const tb of tables) snapshot[tb.name] = dragPos[tb.name] ?? overrides[tb.name] ?? layout?.[tb.name] ?? initialLayout[tb.name] ?? { x: 60, y: 60 }
        onLayoutChange(snapshot)
      }
    } else if (g.type === 'link') {
      const lk = link
      setLink(null)
      if (lk?.target) openPanel(lk.from, lk.target, lk.cur)
    } else if (g.type === 'pan') {
      bump() // 提交视图后刷新依赖视图的覆盖层位置
    }
  }

  /* ── 滚轮缩放（围绕光标点），命令式应用、防抖提交 ── */
  const wheelTimer = useRef<number | null>(null)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const onWheel = (e: WheelEvent): void => {
      e.preventDefault()
      const r = el.getBoundingClientRect()
      const v = viewRef.current
      const f = Math.exp(-e.deltaY * 0.0012)
      const nk = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, v.k * f))
      const mx = e.clientX - r.left, my = e.clientY - r.top
      const real = nk / v.k
      v.tx = mx - (mx - v.tx) * real
      v.ty = my - (my - v.ty) * real
      v.k = nk
      applyView()
      if (wheelTimer.current) window.clearTimeout(wheelTimer.current)
      wheelTimer.current = window.setTimeout(() => bump(), 140)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  /* Escape 清理：面板/连线/选中 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      setPanel(null); setLink(null); setSelKey(null); gestureRef.current = null
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /* ── 挂载后一次性适配视野：把内容包围盒居中放进容器 ── */
  const fittedRef = useRef(false)
  useEffect(() => {
    if (fittedRef.current || !tables.length || !wrapRef.current) return
    fittedRef.current = true
    requestAnimationFrame(() => {
      const el = wrapRef.current
      if (!el) return
      const xs = tables.map((tb) => posOf(tb.name).x)
      const ys = tables.map((tb) => posOf(tb.name).y)
      const minX = Math.min(...xs) - NODE_W / 2, maxX = Math.max(...xs) + NODE_W / 2
      const minY = Math.min(...ys) - NODE_H / 2, maxY = Math.max(...ys) + NODE_H / 2
      const bw = maxX - minX, bh = maxY - minY
      const k = Math.min(1.15, Math.max(ZOOM_MIN, Math.min((el.clientWidth - 70) / bw, (el.clientHeight - 70) / bh)))
      viewRef.current = { k, tx: (el.clientWidth - bw * k) / 2 - minX * k, ty: (el.clientHeight - bh * k) / 2 - minY * k }
      applyView()
      bump()
    })
  }, [tables.length]) // eslint-disable-line react-hooks/exhaustive-deps

  /* ── 连线面板 ── */
  const openPanel = (from: string, to: string, anchorWorld: Trg2dPoint): void => {
    const srcCols = columnsByTable[from] ?? []
    const dstCols = columnsByTable[to] ?? []
    // 字段智能预选：from 侧优先命中含目标表词干的列，to 侧优先主键 id
    const stem = to.replace(/s$/i, '').toLowerCase()
    const guessFrom = srcCols.find((c) => c.toLowerCase().includes(stem) && /id$/i.test(c))
      ?? srcCols.find((c) => c.toLowerCase().includes(stem))
      ?? srcCols.find((c) => /id$/i.test(c))
      ?? ''
    const guessTo = dstCols.find((c) => /^id$/i.test(c)) ?? ''
    setPanel({
      from_table: from, to_table: to, anchor: anchorWorld,
      from_col: guessFrom, to_col: guessTo, cardinality: 'n:1', busy: false, error: '',
    })
  }

  const confirmPanel = async (): Promise<void> => {
    if (!panel || panel.busy) return
    if (!panel.from_col || !panel.to_col) { setPanel({ ...panel, error: t('trg2d.noCols') }); return }
    setPanel({ ...panel, busy: true, error: '' })
    try {
      await onAddEdge({
        from_table: panel.from_table, from_col: panel.from_col,
        to_table: panel.to_table, to_col: panel.to_col, cardinality: panel.cardinality,
      })
      setPanel(null)
    } catch (err) {
      setPanel({ ...panel, busy: false, error: (err as Error).message || t('trg2d.failed') })
    }
  }

  const deleteSelected = (): void => {
    const g = edgeGeoms.find((x) => x.key === selKey)
    if (!g) return
    const e = g.edge
    setSelKey(null)
    void Promise.resolve(onDeleteEdge({
      from_table: e.from, from_col: e.from_col ?? undefined,
      to_table: e.to, to_col: e.to_col ?? undefined, kind: e.kind, status: e.status,
    })).catch(() => {})
  }

  /** draft 边确认（✓）：与删除按钮并排出现在选中边中点 */
  const confirmSelected = (): void => {
    const g = edgeGeoms.find((x) => x.key === selKey)
    if (!g || !onConfirmEdge) return
    const e = g.edge
    setSelKey(null)
    void Promise.resolve(onConfirmEdge({
      from_table: e.from, from_col: e.from_col ?? undefined,
      to_table: e.to, to_col: e.to_col ?? undefined, kind: e.kind, status: e.status,
    })).catch(() => {})
  }

  /* ── 渲染辅助 ── */
  const selGeom = edgeGeoms.find((x) => x.key === selKey) ?? null
  const v = viewRef.current
  const panelScreen = panel ? worldToScreen(panel.anchor) : null
  const delScreen = selGeom ? worldToScreen(selGeom.mid) : null
  const previewD = (() => {
    if (!link) return ''
    const a = posMap[link.from]
    if (!a) return ''
    const dx = link.cur.x - a.x, dy = link.cur.y - a.y
    const pa = clipToRect(a.x, a.y, dx, dy, EDGE_GAP + 2)
    return `M ${pa.x} ${pa.y} L ${link.cur.x} ${link.cur.y}`
  })()

  return (
    <div ref={wrapRef} className={`trg2d ${className ?? ''}`}>
      <svg
        className={`trg2d-svg${linkZone ? ' is-linkzone' : ''}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endGesture}
        onPointerCancel={endGesture}
        onPointerLeave={() => { if (!gestureRef.current && hoverName) setHoverName(null) }}
      >
        <g ref={(el) => { viewGRef.current = el; if (el) requestAnimationFrame(applyView) }}>
          {/* ── 边层 ── */}
          {edgeGeoms.map((g) => (
            <g key={g.key} className={`trg2d-edge${g.draft ? ' is-draft' : ''}${selKey === g.key ? ' is-sel' : ''}`}>
              <path className="trg2d-edge-hit" d={g.d} />
              <path className="trg2d-edge-line" d={g.d} />
              {/* 深度缩小时隐藏标签防糊（选中边常显） */}
              {(v.k >= 0.55 || selKey === g.key) && (
                <g transform={g.horizontal
                  ? `translate(${g.mid.x} ${g.mid.y})`
                  : `translate(${g.mid.x} ${g.mid.y}) rotate(${g.angle})`}>
                  <rect className="trg2d-edge-labelbg"
                    x={g.horizontal ? 9 : -g.labelW / 2} y={-8.5} width={g.labelW} height={17} rx={4} />
                  {!g.draft && g.edge.cardinality === '1:1' && (
                    <g className="trg2d-edge-ticks">
                      {/* 双短线徽标：垂直于线向、对称分布在标签两侧 */}
                      {(g.horizontal
                        ? [[-2, -13, 6, -13], [-2, -8, 6, -8], [-2, 8, 6, 8], [-2, 13, 6, 13]]
                        : [[-13, -4.5, -13, 4.5], [-8, -4.5, -8, 4.5], [8, -4.5, 8, 4.5], [13, -4.5, 13, 4.5]]
                      ).map(([x1, y1, x2, y2], i) => (
                        <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} />
                      ))}
                    </g>
                  )}
                  <text className="trg2d-edge-label"
                    x={g.horizontal ? 16 : 0}
                    textAnchor={g.horizontal ? 'start' : 'middle'}>
                    {`${g.edge.from}.${g.edge.from_col || '*'} ─ ${g.edge.cardinality === '1:1' ? '1:1' : 'n:1'} ─ ${g.edge.to}.${g.edge.to_col || '*'}`}
                  </text>
                </g>
              )}
              <title>{[g.edge.reason, g.draft ? t('kb.badgePending') : null].filter(Boolean).join(' · ')}</title>
            </g>
          ))}

          {/* ── 连线预览 ── */}
          {previewD && (
            <g className="trg2d-preview">
              <path d={previewD} />
              {link?.target && (() => {
                const c = posMap[link.target]
                return c ? <rect className="trg2d-preview-ring"
                  x={c.x - NODE_W / 2 - 5} y={c.y - NODE_H / 2 - 5} width={NODE_W + 10} height={NODE_H + 10} rx={(NODE_H + 10) / 2} />
                : null
              })()}
            </g>
          )}

          {/* ── 节点层 ── */}
          {tables.map((tb) => {
            const c = posMap[tb.name]
            if (!c) return null
            const exc = !!tb.excluded
            const isSrc = link?.from === tb.name
            return (
              <g key={tb.name}
                className={`trg2d-node${exc ? ' is-excluded' : ''}${hoverName === tb.name ? ' is-hover' : ''}${isSrc ? ' is-src' : ''}`}
                transform={`translate(${c.x} ${c.y})`}>
                <rect className="trg2d-node-body" x={-NODE_W / 2} y={-NODE_H / 2} width={NODE_W} height={NODE_H} rx={NODE_H / 2} />
                <circle className="trg2d-node-dot" cx={-NODE_W / 2 + 15} cy={0} r={4.5} fill={colorOf(tb)} />
                <text className="trg2d-node-name" x={-NODE_W / 2 + 26} y={4}>{truncName(tb.name)}</text>
                {exc && <title>{t('graph.remove')} · {tb.name}</title>}
              </g>
            )
          })}
        </g>
      </svg>

      {/* ── 操作提示 ── */}
      <div className="trg2d-hint">{t('trg2d.hint')}</div>

      {/* ── 缩放指示 ── */}
      <div className="trg2d-zoom">{Math.round(v.k * 100)}%</div>

      {/* ── 选中边的操作按钮（draft 边多一个 ✓ 确认） ── */}
      {selGeom && delScreen && (
        <>
          {selGeom.draft && onConfirmEdge && (
            <button type="button" className="trg2d-okbtn" style={{ left: delScreen.x - 15, top: delScreen.y }}
              title={t('trg2d.confirmEdge')} onClick={confirmSelected}>✓</button>
          )}
          <button type="button" className="trg2d-delbtn" style={{ left: delScreen.x + (selGeom.draft && onConfirmEdge ? 15 : 0), top: delScreen.y }}
            title={t('trg2d.del')} onClick={deleteSelected}>✕</button>
        </>
      )}

      {/* ── 连线面板（松手落在目标表上弹出）── */}
      {panel && panelScreen && (() => {
        const srcCols = columnsByTable[panel.from_table] ?? []
        const dstCols = columnsByTable[panel.to_table] ?? []
        return (
          <div className="trg2d-panel" style={{ left: panelScreen.x, top: panelScreen.y }} onPointerDown={(e) => e.stopPropagation()}>
            <div className="trg2d-panel-title">
              <b>{panel.from_table}</b><span className="trg2d-panel-arrow">→</span><b>{panel.to_table}</b>
            </div>
            <label className="trg2d-panel-field">
              <span>{t('trg2d.from')}</span>
              <select value={panel.from_col} onChange={(e) => setPanel({ ...panel, from_col: e.target.value })}>
                {!srcCols.length && <option value="">{t('trg2d.noCols')}</option>}
                {srcCols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <label className="trg2d-panel-field">
              <span>{t('trg2d.card')}</span>
              <select value={panel.cardinality} onChange={(e) => setPanel({ ...panel, cardinality: e.target.value as 'n:1' | '1:1' })}>
                <option value="n:1">n:1</option>
                <option value="1:1">1:1</option>
              </select>
            </label>
            <label className="trg2d-panel-field">
              <span>{t('trg2d.to')}</span>
              <select value={panel.to_col} onChange={(e) => setPanel({ ...panel, to_col: e.target.value })}>
                {!dstCols.length && <option value="">{t('trg2d.noCols')}</option>}
                {dstCols.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            {panel.error && <div className="trg2d-panel-error">{panel.error}</div>}
            <div className="trg2d-panel-acts">
              <button type="button" className="trg2d-btn ghost" onClick={() => setPanel(null)}>{t('trg2d.cancel')}</button>
              <button type="button" className="trg2d-btn primary" disabled={panel.busy} onClick={() => void confirmPanel()}>
                {panel.busy ? '…' : t('trg2d.confirm')}
              </button>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
