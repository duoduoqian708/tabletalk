import { useEffect, useId, useMemo, useReducer, useRef, useState } from 'react'
import type { GraphEdge } from '@renderer/api/types'
import { colorFor } from '@renderer/lib/sphere'
import { useI18n } from '@renderer/store/i18n'

/* ═══════════════════════════════════════════════════════════════
   TableRelationGraph2D — 手写 SVG 可编辑表关系图（spec §6 右栏）
   领域聚类初始布局 · 圆形节点（尺寸/配色对齐 3D 星图）· 字段级边标签 ·
   边缘拖线弹面板连线（箭头）· 中心拖动节点 · 平移缩放
   展示/编辑双态：展示态连线带 from→to 定向流动，编辑态箭头+可拖
   无第三方图库；坐标受控（layout prop）+ 本地乐观覆盖，松手回传宿主持久化
   ═══════════════════════════════════════════════════════════════ */

export interface Trg2dTable {
  name: string
  tags?: string[]
  excluded?: boolean
  /** 节点规模（列数/行数），驱动圆形半径 1:1 对齐 3D 星图 */
  size?: number
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
  className?: string
  /** 展示态（只读，连线定向流动）| 编辑态（箭头 + 边缘拖线 + 移动 + 选边删确认）。缺省=edit */
  mode?: 'edit' | 'display'
  /** 表名 → 颜色覆盖（知识库标签色）；无覆盖时走 colorFor hash */
  colorMap?: Record<string, string>
  /** 外部高亮边 key（关系预览列表联动）：高亮渲染 + 视图自动平移到边中点 */
  highlightKey?: string | null
}

/** 边唯一 key（与内部 edgeGeoms 同源，供外部列表联动定位） */
export function graphEdgeKey(e: GraphEdge): string {
  return `${e.from}|${e.from_col ?? ''}|${e.to}|${e.to_col ?? ''}|${e.kind}|${e.status ?? 'confirmed'}`
}

/* ── 几何常量 ── */
const NODE_R_MIN = 18            // 圆形节点最小半径
const NODE_R_MAX = 34            // 圆形节点最大半径（按规模对数缩放）
const NODE_D = NODE_R_MAX * 2    // 圆直径（布局占用上界）
const NODE_STEP_X = NODE_D + 92  // 横向格宽（圆 + 组间留白）
const NODE_STEP_Y = NODE_D + 24  // 纵向格步（圆 + 下方标签 + 间距）
const GROUP_PAD = 46             // 组间留白（领域分区间的空白）
const EDGE_GAP = 4               // 线与节点边框的间隙
const LINK_BAND = 16             // 圆环"连线感应带"宽度（边缘拖线的可抓区）
const HIT_W = 14                 // 边命中区宽度
const ZOOM_MIN = 0.35
const ZOOM_MAX = 2.5
const UNTAGGED = '\u0000'     // 无标签桶排序键（保证排最后）
/**
 * 领域聚类初始布局：按首标签分组 → 组名排序（无标签恒最后）→ 组块按列网格摆放，
 * 组内竖排一列；同输入同输出（纯确定性，不依赖 DOM/时间）。
 */
export function computeInitialLayout(tables: Trg2dTable[]): Trg2dLayout {
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
  // 组内小网格（每行最多 2 个节点）：避免组内节点垂直堆成"一条线"
  const INNER_COLS = 2
  const cellW = NODE_STEP_X * INNER_COLS
  // 每行高度 = 行内最高组块；组块在行内垂直居中
  const blocks = keys.map((k) => ({ key: k, names: groups.get(k)! }))
  for (let r = 0; r * cols < blocks.length; r++) {
    const row = blocks.slice(r * cols, (r + 1) * cols)
    const rowH = Math.max(...row.map((b) => Math.ceil(b.names.length / INNER_COLS) * NODE_STEP_Y))
    let rowTop = 0
    for (let rr = 0; rr < r; rr++) {
      rowTop += Math.max(...blocks.slice(rr * cols, (rr + 1) * cols).map((b) => Math.ceil(b.names.length / INNER_COLS) * NODE_STEP_Y)) + GROUP_PAD * 2
    }
    row.forEach((b, c) => {
      const blockH = Math.ceil(b.names.length / INNER_COLS) * NODE_STEP_Y
      const top = rowTop + GROUP_PAD + (rowH - blockH) / 2
      b.names.forEach((name, i) => {
        out[name] = {
          x: c * cellW + GROUP_PAD + (i % INNER_COLS) * NODE_STEP_X + NODE_D / 2,
          y: top + Math.floor(i / INNER_COLS) * NODE_STEP_Y + NODE_D / 2,
        }
      })
    })
  }
  // 简易 repulsion：检测重叠节点并推开（80 次迭代上限）
  const minGap = NODE_D + 16
  const allNames = Object.keys(out)
  for (let iter = 0; iter < 80; iter++) {
    let moved = false
    for (let i = 0; i < allNames.length; i++) {
      for (let j = i + 1; j < allNames.length; j++) {
        const a = out[allNames[i]], b = out[allNames[j]]
        const dx = b.x - a.x, dy = b.y - a.y
        const dist = Math.hypot(dx, dy) || 1
        if (dist < minGap) {
          const push = (minGap - dist) / 2 + 0.5
          const nx = dx / dist, ny = dy / dist
          a.x -= nx * push; a.y -= ny * push
          b.x += nx * push; b.y += ny * push
          moved = true
        }
      }
    }
    if (!moved) break
  }
  return out
}

/** 自圆心出发的方向线与节点圆的交点（半径裁剪），再外推 gap 像素留白 */
function clipToCircle(cx: number, cy: number, dx: number, dy: number, gap: number, radius: number): Trg2dPoint {
  const len = Math.hypot(dx, dy) || 1
  const t = (radius + gap) / len
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
  labelFull: string   // "from.table.from_col → to.table.to_col (n:1)"
  /** 二次贝塞尔均匀采样折线（命中检测用，弦距近似在弓高下会漏检） */
  pts: Trg2dPoint[]
}

export function TableRelationGraph2D({
  tables, edges, columnsByTable, onAddEdge, onDeleteEdge, onConfirmEdge, onLayoutChange, layout, className, mode = 'edit', colorMap, highlightKey,
}: Props): React.JSX.Element {
  const { t } = useI18n()
  const editable = mode === 'edit'
  const wrapRef = useRef<HTMLDivElement>(null)
  const viewGRef = useRef<SVGGElement | null>(null)
  const [, bump] = useReducer((x: number) => x + 1, 0)
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '').slice(0, 8)
  const arrowId = `trg2d-arrow-${uid}`

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

  /* ── 节点圆半径：按规模对数缩放（尺寸对齐 3D 星图 radiusFor 的语义） ── */
  const radii = useMemo(() => {
    const m: Record<string, number> = {}
    const s = tables.map((tb) => Math.max(1, tb.size ?? 1))
    const mn = Math.min(...s), mx = Math.max(...s)
    const lo = Math.log(mn), hi = Math.log(Math.max(2, mx))
    for (const tb of tables) {
      const c = Math.max(1, tb.size ?? 1)
      const tt = hi > lo ? (Math.log(c) - lo) / (hi - lo) : 0.5
      m[tb.name] = NODE_R_MIN + (NODE_R_MAX - NODE_R_MIN) * Math.max(0, Math.min(1, tt))
    }
    return m
  }, [tables])

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
      const ra = radii[e.from] ?? NODE_R_MIN, rb = radii[e.to] ?? NODE_R_MIN
      const pa = clipToCircle(a.x, a.y, dx, dy, EDGE_GAP, ra)
      const pb = clipToCircle(b.x, b.y, -dx, -dy, EDGE_GAP, rb)
      // 轻曲线：中点沿法线上抬 7% 长度，避免完全平行边重叠
      const nx = -dy / len, ny = dx / len
      const bow = len * 0.07
      const cx = (a.x + b.x) / 2 + nx * bow, cy = (a.y + b.y) / 2 + ny * bow
      const mid = { x: (pa.x + pb.x + 2 * cx) / 4, y: (pa.y + pb.y + 2 * cy) / 4 }
      let angle = (Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180) / Math.PI
      const horizontal = Math.abs(angle) > 55   // 近垂直边：标签水平放置
      if (!horizontal && (angle > 90 || angle < -90)) angle += 180   // 沿线标签保持正立
      const fc = e.from_col || '*', tc = e.to_col || '*'
      const cardStr = e.cardinality === '1:1' ? '1:1' : 'n:1'
      const labelFull = `${e.from}.${fc} → ${e.to}.${tc} (${cardStr})`
      // 命中折线采样：B(t)=(1-t)²P0+2(1-t)tC+t²P2，16 段足够 7px 阈值
      const pts: Trg2dPoint[] = []
      for (let i = 0; i <= 16; i++) {
        const tt = i / 16, u = 1 - tt
        pts.push({ x: u * u * pa.x + 2 * u * tt * cx + tt * tt * pb.x, y: u * u * pa.y + 2 * u * tt * cy + tt * tt * pb.y })
      }
      out.push({
        key: graphEdgeKey(e),
        edge: e, draft: e.status === 'draft',
        d: `M ${pa.x} ${pa.y} Q ${cx} ${cy} ${pb.x} ${pb.y}`,
        mid, angle, horizontal, labelW: (fc.length + tc.length) * 5.2 + 30, labelFull, pts,
      })
    }
    return out
  }, [edges, posMap, tableSet, excludedSet, radii])

  /* ── 节点配色（与 3D 星图同源 SPHERE_PALETTE，按表名稳定取色） ── */
  const nodeColor = (tb: Trg2dTable): string => (tb.excluded ? '#5a6a7e' : (colorMap?.[tb.name] ?? colorFor(tb.name)))

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
      const r = radii[tb.name] ?? NODE_R_MIN
      if (Math.hypot(p.x - c.x, p.y - c.y) <= r + 3) best = tb.name
    }
    return best
  }

  /** 命中圆环（外圈内 r..r-LINK_BAND 的环形带）→ 连线起点；圆内部 → 移动 */
  const inLinkBand = (name: string, p: Trg2dPoint): boolean => {
    const c = posMap[name]
    if (!c) return false
    const r = radii[name] ?? NODE_R_MIN
    const dist = Math.hypot(p.x - c.x, p.y - c.y)
    const inner = Math.max(3, r - LINK_BAND)
    return dist <= r + 3 && dist >= inner
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
    if (!editable) {
      // 展示态：只允许平移/缩放，不接链接/移动/选边手势
      const v = viewRef.current
      gestureRef.current = { type: 'pan', sx: ev.clientX, sy: ev.clientY, tx: v.tx, ty: v.ty }
      ev.currentTarget.classList.add('is-panning')
      return
    }
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
      // 空闲态：更新悬停目标与连线感应带光标提示（展示态不追踪）
      rectRef.current = null
      if (!editable) return
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
      const minX = Math.min(...xs) - NODE_R_MAX, maxX = Math.max(...xs) + NODE_R_MAX
      const minY = Math.min(...ys) - (NODE_R_MAX + 14), maxY = Math.max(...ys) + (NODE_R_MAX + 14)
      const bw = maxX - minX, bh = maxY - minY
      const k = Math.min(1.15, Math.max(ZOOM_MIN, Math.min((el.clientWidth - 70) / bw, (el.clientHeight - 70) / bh)))
      viewRef.current = { k, tx: (el.clientWidth - bw * k) / 2 - minX * k, ty: (el.clientHeight - bh * k) / 2 - minY * k }
      applyView()
      bump()
    })
  }, [tables.length]) // eslint-disable-line react-hooks/exhaustive-deps

  /* ── 外部高亮联动：列表点边 → 视图平移到边中点（保持缩放） ── */
  useEffect(() => {
    if (!highlightKey || !wrapRef.current) return
    const g = edgeGeoms.find((x) => x.key === highlightKey)
    if (!g) return
    const el = wrapRef.current
    const v = viewRef.current
    v.tx = el.clientWidth / 2 - g.mid.x * v.k
    v.ty = el.clientHeight / 2 - g.mid.y * v.k
    applyView()
    bump()
  }, [highlightKey]) // eslint-disable-line react-hooks/exhaustive-deps

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
    const r = radii[link.from] ?? NODE_R_MIN
    const dx = link.cur.x - a.x, dy = link.cur.y - a.y
    const pa = clipToCircle(a.x, a.y, dx, dy, EDGE_GAP + 2, r)
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
        <defs>
          <marker id={arrowId} viewBox="0 0 10 10" refX="8" refY="5"
            markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L10,5 L0,10 z" className="trg2d-arrow" />
          </marker>
        </defs>
        <g ref={(el) => { viewGRef.current = el; if (el) requestAnimationFrame(applyView) }}>
          {/* ── 边层 ── */}
          {edgeGeoms.map((g) => (
            <g key={g.key} className={`trg2d-edge${g.draft ? ' is-draft' : ''}${selKey === g.key ? ' is-sel' : ''}${highlightKey === g.key ? ' is-highlight' : ''}${editable ? ' is-edit' : (g.draft ? '' : ' is-flow')}`}>
              <path className="trg2d-edge-hit" d={g.d} />
              <path className="trg2d-edge-line" d={g.d}
                markerEnd={editable ? `url(#${arrowId})` : undefined} />
              {/* 深度缩小时隐藏标签防糊（选中/高亮边常显） */}
              {(v.k >= 0.55 || selKey === g.key || highlightKey === g.key) && (
                <g transform={g.horizontal
                  ? `translate(${g.mid.x} ${g.mid.y})`
                  : `translate(${g.mid.x} ${g.mid.y}) rotate(${g.angle})`}>
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
                    x={g.horizontal ? 9 : 0}
                    textAnchor={g.horizontal ? 'start' : 'middle'}>
                    {`${g.edge.from_col || '*'} → ${g.edge.to_col || '*'}`}
                  </text>
                </g>
              )}
              <title>{[g.labelFull, g.edge.reason, g.draft ? t('kb.badgePending') : null].filter(Boolean).join(' · ')}</title>
            </g>
          ))}

          {/* ── 连线预览 ── */}
          {previewD && (
            <g className="trg2d-preview">
              <path d={previewD} />
              {link?.target && (() => {
                const c = posMap[link.target]
                return c ? <circle className="trg2d-preview-ring"
                  cx={c.x} cy={c.y} r={(radii[link.target] ?? NODE_R_MIN) + 5} />
                : null
              })()}
            </g>
          )}

          {/* ── 节点层（圆形，配色/半径对齐 3D 星图）── */}
          {tables.map((tb) => {
            const c = posMap[tb.name]
            if (!c) return null
            const exc = !!tb.excluded
            const isSrc = link?.from === tb.name
            const r = radii[tb.name] ?? NODE_R_MIN
            return (
              <g key={tb.name}
                className={`trg2d-node${exc ? ' is-excluded' : ''}${hoverName === tb.name ? ' is-hover' : ''}${isSrc ? ' is-src' : ''}`}
                transform={`translate(${c.x} ${c.y})`}>
                <circle className="trg2d-node-body" r={r} fill={nodeColor(tb)} />
                {hoverName === tb.name && linkZone && (
                  <circle className="trg2d-link-ring" r={r + 6} />
                )}
                <text className="trg2d-node-name-bg" y={r + 12} textAnchor="middle">{truncName(tb.name)}</text>
                <text className="trg2d-node-name" y={r + 12} textAnchor="middle">{truncName(tb.name)}</text>
                {exc && <title>{t('graph.remove')} · {tb.name}</title>}
              </g>
            )
          })}
        </g>
      </svg>

      {/* ── 操作提示 ── */}
      <div className="trg2d-hint">{editable ? t('trg2d.hint') : '展示态 · 只读方向流动 · 切「编辑」可拖线连表'}</div>

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
