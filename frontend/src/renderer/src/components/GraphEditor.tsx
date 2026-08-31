import { useEffect, useRef, useState } from 'react'
import { MIN_R, colorFor, radiusFor, spherePositions, project } from '@renderer/lib/sphere'
import { useI18n } from '@renderer/store/i18n'

export interface EditorGraphNode {
  name: string
  row_count: number
  column_count: number
}

export interface EditorGraphEdge {
  from: string
  to: string
  kind: 'fk' | 'overlap' | 'user' | 'llm' | 'naming' | 'value_overlap' | 'query_log'
  from_col?: string | null
  to_col?: string | null
}

interface Props {
  tables: EditorGraphNode[]
  edges: EditorGraphEdge[]
  excluded?: string[]
  onAddEdge: (from: string, to: string) => void
  onDeleteEdge: (edge: EditorGraphEdge) => void
  onExcludeNode: (name: string) => void
  onEditNode: (name: string) => void
}

export function GraphEditor({ tables, edges, excluded, onAddEdge, onDeleteEdge, onExcludeNode, onEditNode }: Props): React.JSX.Element {
  const { t } = useI18n()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const toolbarRef = useRef<HTMLDivElement>(null)
  const pausedRef = useRef(false)
  const selRef = useRef<number | null>(null)
  const linkRef = useRef<string | null>(null)

  // 所有可变输入走 ref，effect 仅挂载一次，避免每次渲染重建几何导致 geom 短暂为空时点击崩溃
  const dataRef = useRef({ tables, edges, excluded, onAddEdge, onDeleteEdge, onExcludeNode, onEditNode })
  dataRef.current = { tables, edges, excluded, onAddEdge, onDeleteEdge, onExcludeNode, onEditNode }

  const excludedSet = new Set(excluded || [])
  const rtables = tables.filter((t) => !excludedSet.has(t.name))
  const nameToIdx = new Map(rtables.map((t, i) => [t.name, i]))
  const redges = edges.filter((e) => nameToIdx.has(e.from) && nameToIdx.has(e.to))
  const idxRef = useRef({ rtables, redges, nameToIdx })
  idxRef.current = { rtables, redges, nameToIdx }

  const [sel, setSel] = useState<string | null>(null)
  const [link, setLink] = useState<string | null>(null)

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext('2d')
    if (!ctx) return

    let W = 0, H = 0, D = 0, dpr = 1
    const resize = (): void => {
      dpr = Math.min(2, window.devicePixelRatio || 1)
      W = cv.clientWidth; H = cv.clientHeight
      cv.width = Math.floor(W * dpr); cv.height = Math.floor(H * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      D = Math.min(W, H)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(cv)

    let yaw = 0.55, pitch = 0.35, zoom = 1
    let targetYaw = yaw, targetPitch = pitch
    let dragging: { x: number; y: number } | null = null
    let downPt: { x: number; y: number } | null = null
    let moved = false
    let velY = 0, velP = 0, autoRot = true
    let hover: number | null = null

    const rotate = (p: [number, number, number] | undefined): { x: number; y: number; z: number; scale: number } =>
      project(p ?? [0, 0, 0], { yaw, pitch, zoom, D, W, H })

    const geom: { POS: [number, number, number][]; RADII: number[]; COLORS: string[] } = {
      POS: [], RADII: [], COLORS: []
    }
    // 同步构建几何（挂载即填充，避免 geom 为空时点击 rotate(undefined) 崩溃）
    const rebuildGeom = (): void => {
      const tbl = idxRef.current.rtables
      const counts = tbl.map((tt) => tt.row_count)
      const minC = counts.length ? Math.min(...counts) : 1
      const maxC = counts.length ? Math.max(...counts) : 1
      const n = tbl.length
      const POS = spherePositions(n)
      geom.POS = POS
      geom.RADII = tbl.map((tt) => radiusFor(tt.row_count, minC, maxC))
      geom.COLORS = tbl.map((tt) => colorFor(tt.name))
    }
    rebuildGeom()

    // 几何只随表集重建（rtables 引用变化才重算）；此前每帧重算布点/半径/取色纯属浪费
    let geomSrc: unknown = null
    const rebuildGeomIfNeeded = (): void => {
      const src = idxRef.current.rtables
      if (src !== geomSrc) {
        rebuildGeom()
        geomSrc = src
      }
    }

    // 每帧投影缓存（供绘制与 hitNode/hitEdge 复用，鼠标移动时不再全量重投影）
    let projLast: { i: number; p: ReturnType<typeof rotate> }[] = []

    // 邻居集合缓存：选中期间每帧重建 Set + 遍历全部边不必要
    let hlCache: { focus: number; eLen: number; set: Set<number> } | null = null
    const neighborsOf = (idx: number): Set<number> => {
      const es = idxRef.current.redges
      if (hlCache && hlCache.focus === idx && hlCache.eLen === es.length) return hlCache.set
      const set = new Set<number>([idx])
      es.forEach((e) => {
        const a = idxRef.current.nameToIdx.get(e.from)
        const b = idxRef.current.nameToIdx.get(e.to)
        if (a === idx && b != null) set.add(b)
        if (b === idx && a != null) set.add(a)
      })
      hlCache = { focus: idx, eLen: es.length, set }
      return set
    }

    let raf = 0
    const draw = (t: number): void => {
      ctx.clearRect(0, 0, W, H)
      rebuildGeomIfNeeded()
      const tbl = idxRef.current.rtables
      const es = idxRef.current.redges

      // projBy 按节点下标索引（边端点/工具栏定位取用）；绘制序按下标数组排序。
      // sort 后 projBy[a] 必须仍是节点 a（历史高亮错乱 bug 的教训），深度序只进 orderIdx。
      const projBy: { i: number; p: ReturnType<typeof rotate> }[] = []
      const orderIdx: number[] = []
      for (let i = 0; i < tbl.length; i++) {
        projBy.push({ i, p: rotate(geom.POS[i]) })
        orderIdx.push(i)
      }
      orderIdx.sort((a, b) => projBy[a].p.z - projBy[b].p.z)
      projLast = projBy

      const focus = selRef.current
      const hlSet = focus != null ? neighborsOf(focus) : null

      // 连线：默认保留花色但压暗；只要有一端是"点亮的点"就亮（任意类型边）
      es.forEach((e) => {
        const a = idxRef.current.nameToIdx.get(e.from)
        const b = idxRef.current.nameToIdx.get(e.to)
        if (a == null || b == null) return
        const A = projBy[a], B = projBy[b]
        const lit = focus != null && (a === focus || b === focus)
        const isUser = e.kind === 'user'
        // 端点收到圆的边缘外留 2px 间隙，避免连线穿透圆圈
        const ra = Math.max(MIN_R, geom.RADII[a] * Math.min(1.5, A.p.scale))
        const rb = Math.max(MIN_R, geom.RADII[b] * Math.min(1.5, B.p.scale))
        const dx = B.p.x - A.p.x, dy = B.p.y - A.p.y
        const len = Math.hypot(dx, dy)
        let ax = A.p.x, ay = A.p.y, bx = B.p.x, by = B.p.y
        if (len > ra + rb + 2) {
          const ux = dx / len, uy = dy / len, gap = 2
          ax = A.p.x + ux * (ra + gap); ay = A.p.y + uy * (ra + gap)
          bx = B.p.x - ux * (rb + gap); by = B.p.y - uy * (rb + gap)
        }
        ctx.strokeStyle = lit
          ? (isUser ? 'rgba(255,196,107,0.9)' : 'rgba(52,245,197,0.78)')
          : (isUser ? 'rgba(255,196,107,0.22)' : 'rgba(120,150,205,0.22)')
        ctx.lineWidth = lit ? 1.8 : 1
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke()
        const tm = (t / 1600 + (a * 0.13 + b * 0.07)) % 1
        const qx = ax + (bx - ax) * tm, qy = ay + (by - ay) * tm
        ctx.fillStyle = lit
          ? (isUser ? 'rgba(255,196,107,.95)' : 'rgba(52,245,197,.95)')
          : (isUser ? 'rgba(255,196,107,.3)' : 'rgba(120,150,205,.3)')
        ctx.beginPath(); ctx.arc(qx, qy, lit ? 2.1 : 1.2, 0, 7); ctx.fill()
      })

      // 节点：默认保留各自花色（低透明度）；仅焦点节点及其直接邻居点亮
      for (let oi = 0; oi < orderIdx.length; oi++) {
        const i = orderIdx[oi]
        const p = projBy[i].p
        const lit = focus != null && (i === focus || (hlSet ? hlSet.has(i) : false))
        const isFocus = focus === i
        const isLinkSrc = linkRef.current === tbl[i].name
        const r = Math.max(MIN_R, geom.RADII[i] * Math.min(1.5, p.scale))
        if (isFocus || isLinkSrc) {
          const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 2.4)
          g.addColorStop(0, 'rgba(255,255,255,.5)'); g.addColorStop(1, geom.COLORS[i] + '00')
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.4, 0, 7); ctx.fill()
        }
        ctx.globalAlpha = lit ? 1 : 0.5
        ctx.fillStyle = geom.COLORS[i]
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7); ctx.fill()
        ctx.lineWidth = (lit || isFocus || isLinkSrc) ? 2 : 1
        ctx.strokeStyle = isFocus ? 'rgba(255,255,255,.95)' : isLinkSrc ? 'rgba(255,196,107,.95)' : lit ? 'rgba(255,255,255,.6)' : 'rgba(255,255,255,.25)'
        ctx.stroke()
        ctx.globalAlpha = 1
        const fs = (isFocus || isLinkSrc) ? 12.5 : Math.min(12, 9.5 + p.scale * 2.2)
        ctx.font = `600 ${fs}px var(--sans, sans-serif)`
        ctx.textAlign = 'center'
        // 深色底衬替代 shadowBlur（阴影走软件渲染路径，逐帧文字绘制的性能悬崖）
        ctx.fillStyle = 'rgba(5,7,13,.9)'
        ctx.fillText(tbl[i].name, p.x + 1, p.y - r - 6)
        ctx.fillStyle = (isFocus || isLinkSrc) ? '#fff' : lit ? '#fff' : 'rgba(190,205,225,.55)'
        ctx.fillText(tbl[i].name, p.x, p.y - r - 7)
      }

      // 选中节点跟随定位工具栏
      const tb = toolbarRef.current
      if (tb && selRef.current != null && projBy[selRef.current]) {
        const q = projBy[selRef.current].p
        const r = Math.max(MIN_R, geom.RADII[selRef.current] * Math.min(1.5, q.scale))
        tb.style.left = `${q.x}px`
        tb.style.top = `${q.y - r - 14}px`
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)

    const hitNode = (px: number, py: number): number | null => {
      const tbl = idxRef.current.rtables
      let best: number | null = null, bd = 1e9
      // 复用绘制帧投影（最多滞后一帧），免去每次 pointermove 全量重投影
      tbl.forEach((_, i) => {
        const q = projLast[i]?.p
        if (!q) return
        const r = Math.max(MIN_R, geom.RADII[i] * q.scale) + 6
        const d = (px - q.x) ** 2 + (py - q.y) ** 2
        if (d < bd && d < r * r) { bd = d; best = i }
      })
      return best
    }

    const hitEdge = (px: number, py: number): EditorGraphEdge | null => {
      let best: EditorGraphEdge | null = null, bd = 36
      idxRef.current.redges.forEach((e) => {
        const a = idxRef.current.nameToIdx.get(e.from), b = idxRef.current.nameToIdx.get(e.to)
        if (a == null || b == null) return
        const A = projLast[a]?.p, B = projLast[b]?.p
        if (!A || !B) return
        const dx = B.x - A.x, dy = B.y - A.y
        const l2 = dx * dx + dy * dy || 1
        let tt = ((px - A.x) * dx + (py - A.y) * dy) / l2
        tt = Math.max(0, Math.min(1, tt))
        const cx = A.x + dx * tt, cy = A.y + dy * tt
        const d = (px - cx) ** 2 + (py - cy) ** 2
        if (d < bd) { bd = d; best = e }
      })
      return best
    }

    // offsetX/offsetY 由浏览器直接给出（相对目标元素），免去每次事件 getBoundingClientRect 的布局查询
    const pt = (e: PointerEvent | MouseEvent) => {
      return { x: e.offsetX, y: e.offsetY }
    }

    const clearSelection = (): void => {
      selRef.current = null; linkRef.current = null; pausedRef.current = false
      hover = null
      setSel(null); setLink(null)
    }

    const onDown = (e: PointerEvent): void => {
      dragging = pt(e); downPt = pt(e); moved = false
      velY = 0; velP = 0; autoRot = false
      cv.classList.add('drag'); cv.setPointerCapture(e.pointerId)
    }
    const onMove = (e: PointerEvent): void => {
      const p = pt(e)
      if (!dragging) {
        // 非拖拽：正常悬停预览（鼠标移上去即"选中"高亮）
        hover = hitNode(p.x, p.y)
        cv.style.cursor = hover != null ? 'pointer' : 'grab'
        return
      }
      // 拖拽旋转：冻结 hover，高亮集合保持稳定、跟着图一起转
      const dx = p.x - dragging.x, dy = p.y - dragging.y
      dragging = p
      if (downPt && (p.x - downPt.x) ** 2 + (p.y - downPt.y) ** 2 > 16) moved = true
      yaw += dx * 0.006; pitch = Math.max(-1.2, Math.min(1.2, pitch + dy * 0.005))
      velY = dx * 0.006; velP = dy * 0.005
      targetYaw = yaw; targetPitch = pitch
    }
    const onUp = (): void => { dragging = null; cv.classList.remove('drag') }
    const onLeave = (): void => { dragging = null; cv.classList.remove('drag'); hover = null }
    const onWheel = (e: WheelEvent): void => {
      e.preventDefault()
      zoom = Math.max(0.5, Math.min(2.4, zoom * (e.deltaY < 0 ? 1.09 : 1 / 1.09)))
    }
    const onClick = (e: MouseEvent): void => {
      if (moved) return // 拖拽旋转结束的 click 不触发选中/清除
      const p = pt(e)
      const i = hitNode(p.x, p.y)
      if (i != null) {
        hover = i // 仅用于光标样式；高亮由 selRef（单击选中）驱动
        const name = idxRef.current.rtables[i].name
        if (linkRef.current) {
          if (linkRef.current !== name) dataRef.current.onAddEdge(linkRef.current, name)
          clearSelection()
        } else {
          selRef.current = i; setSel(name); pausedRef.current = true
        }
        return
      }
      if (!linkRef.current) {
        const e2 = hitEdge(p.x, p.y)
        if (e2) { dataRef.current.onDeleteEdge(e2); return }
      }
      clearSelection()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') clearSelection()
    }

    const tick = setInterval(() => {
      if (!dragging) {
        if (autoRot) {
          targetYaw += 0.0015
        } else {
          // 松手后惯性滑行，速度逐渐衰减直至停下
          targetYaw = yaw + velY; targetPitch = pitch + velP; velY *= 0.93; velP *= 0.93
        }
      }
      yaw += (targetYaw - yaw) * 0.12; pitch += (targetPitch - pitch) * 0.12
    }, 16)

    cv.addEventListener('pointerdown', onDown)
    cv.addEventListener('pointermove', onMove)
    cv.addEventListener('pointerup', onUp)
    cv.addEventListener('pointerleave', onLeave)
    cv.addEventListener('wheel', onWheel, { passive: false })
    cv.addEventListener('click', onClick)
    window.addEventListener('keydown', onKey)

    return () => {
      ro.disconnect(); clearInterval(tick); cancelAnimationFrame(raf)
      cv.removeEventListener('pointerdown', onDown)
      cv.removeEventListener('pointermove', onMove)
      cv.removeEventListener('pointerup', onUp)
      cv.removeEventListener('pointerleave', onLeave)
      cv.removeEventListener('wheel', onWheel)
      cv.removeEventListener('click', onClick)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  const startLink = (): void => {
    if (!sel) return
    linkRef.current = sel; setLink(sel); pausedRef.current = true
  }
  const editNode = (): void => { if (sel) onEditNode(sel) }
  const excludeNode = (): void => { if (sel) { onExcludeNode(sel); selRef.current = null; linkRef.current = null; pausedRef.current = false; setSel(null); setLink(null) } }

  return (
    <div className="graph-editor">
      <canvas ref={canvasRef} className="graph3d" />
      {sel && !link && (
        <div className="ge-toolbar" ref={toolbarRef}>
          <button type="button" onClick={startLink}>{t('graph.link')}</button>
          <button type="button" onClick={editNode}>{t('graph.note')}</button>
          <button type="button" className="danger" onClick={excludeNode}>{t('graph.remove')}</button>
        </div>
      )}
      {link && (
        <div className="ge-banner">
          {t('graph.linkModePre')}<button type="button" className="link" onClick={() => { selRef.current = null; linkRef.current = null; pausedRef.current = false; setSel(null); setLink(null) }}>{t('common.cancel')}</button>{t('graph.linkModePost')}
        </div>
      )}
    </div>
  )
}
