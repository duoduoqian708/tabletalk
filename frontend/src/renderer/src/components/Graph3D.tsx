import { useEffect, useRef } from 'react'
import { MIN_R, colorFor, radiusFor, spherePositions, project } from '@renderer/lib/sphere'

export interface GraphNode {
  name: string
  row_count: number
  column_count: number
}

export interface GraphEdge {
  table: string
  ref_table: string
}

interface Props {
  tables: GraphNode[]
  foreignKeys: GraphEdge[]
  onSelectNode: (name: string, x: number, y: number) => void
  /** 受控选中（表名）；父级关弹窗时传 null 即可清除高亮 */
  selectedName?: string | null
  /** 点击空白处 / 取消选中时回调，用于父级关闭弹窗 */
  onClearSelection?: () => void
}

export function Graph3D({ tables, foreignKeys, onSelectNode, selectedName, onClearSelection }: Props): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const propsRef = useRef({ tables, foreignKeys, onSelectNode, onClearSelection })
  propsRef.current = { tables, foreignKeys, onSelectNode, onClearSelection }
  // 选中态提到 ref，供父级通过 selectedName 反向清除（修复弹窗关闭后高亮泄漏）
  const selectedRef = useRef<number | null>(null)

  // 父级 selectedName 变化时同步内部选中（null = 清除高亮）
  useEffect(() => {
    selectedRef.current = selectedName
      ? propsRef.current.tables.findIndex((t) => t.name === selectedName)
      : null
  }, [selectedName])

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext('2d')
    if (!ctx) return

    let W = 0, H = 0, D = 0, dpr = 1
    // 深空背景：离屏缓存（径向渐变 + 星云），只在 resize 时重建
    let bg: HTMLCanvasElement | null = null
    const buildBg = (): void => {
      const b = document.createElement('canvas')
      b.width = cv.width; b.height = cv.height
      const c = b.getContext('2d')
      if (!c) return
      c.setTransform(dpr, 0, 0, dpr, 0, 0)
      const g = c.createRadialGradient(W * 0.5, H * 0.44, 0, W * 0.5, H * 0.5, Math.hypot(W, H) * 0.62)
      g.addColorStop(0, '#0c1322')
      g.addColorStop(0.55, '#070b13')
      g.addColorStop(1, '#04060b')
      c.fillStyle = g
      c.fillRect(0, 0, W, H)
      const nebula = (x: number, y: number, r: number, color: string): void => {
        const ng = c.createRadialGradient(x, y, 0, x, y, r)
        ng.addColorStop(0, color)
        ng.addColorStop(1, 'rgba(0,0,0,0)')
        c.fillStyle = ng
        c.fillRect(x - r, y - r, r * 2, r * 2)
      }
      const M = Math.max(W, H)
      nebula(W * 0.24, H * 0.3, M * 0.4, 'rgba(76,201,240,0.05)')
      nebula(W * 0.78, H * 0.66, M * 0.44, 'rgba(139,124,248,0.05)')
      nebula(W * 0.58, H * 0.24, M * 0.3, 'rgba(46,230,168,0.035)')
      nebula(W * 0.5, H * 0.5, M * 0.46, 'rgba(46,230,168,0.025)')
      bg = b
    }
    const resize = (): void => {
      dpr = Math.min(2, window.devicePixelRatio || 1)
      W = cv.clientWidth; H = cv.clientHeight
      cv.width = Math.floor(W * dpr); cv.height = Math.floor(H * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      D = Math.min(W, H)
      buildBg()
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(cv)

    // 星空：球壳布点一次，帧内随视图做 0.35 倍慢速视差旋转 + 闪烁，形成远近层次
    const STARS = Array.from({ length: 130 }, () => {
      const u = Math.random() * 2 - 1
      const th = Math.random() * Math.PI * 2
      const s = Math.sqrt(1 - u * u)
      return {
        x: s * Math.cos(th), y: u, z: s * Math.sin(th),
        size: 0.6 + Math.random() * 1.3,
        phase: Math.random() * 7,
        sp: 0.5 + Math.random() * 1.3,
        base: 0.2 + Math.random() * 0.55
      }
    })

    let yaw = 0.55, pitch = 0.35, zoom = 1
    let dragging: { x: number; y: number } | null = null
    let downPt: { x: number; y: number } | null = null
    let moved = false
    let velY = 0, velP = 0
    let hover: number | null = null
    let popPending = 0
    // 按下瞬间锁定目标节点；若按下时图谱还在惯性漂移，本次"点击"仅用于止停，不选中节点
    let downHit: number | null = null
    let downMoving = false
    // 自转恢复：任何交互即停转（保证命中判定准确），静止约 1.2s 后速度平滑回升，保持星图常转的灵动感
    let auto = 1
    let lastAct = performance.now()

    const rotate = (p: [number, number, number]) => project(p, { yaw, pitch, zoom, D, W, H })

    const neighborsOf = (idx: number): Set<number> => {
      const set = new Set<number>([idx])
      propsRef.current.foreignKeys.forEach((fk) => {
        const a = propsRef.current.tables.findIndex((t) => t.name === fk.table)
        const b = propsRef.current.tables.findIndex((t) => t.name === fk.ref_table)
        if (a === idx && b >= 0) set.add(b)
        if (b === idx && a >= 0) set.add(a)
      })
      return set
    }

    // 每帧刷新的几何缓存，供 draw / hit 共享（schema 加载后 tables 会变）
    const geom: { POS: [number, number, number][]; RADII: number[]; COLORS: string[] } = {
      POS: [],
      RADII: [],
      COLORS: []
    }

    const draw = (t: number): void => {
      if (bg) ctx.drawImage(bg, 0, 0, W, H)
      else ctx.clearRect(0, 0, W, H)

      // 星空（视差旋转比数据球慢，制造景深）
      const cy = Math.cos(yaw * 0.35), sy = Math.sin(yaw * 0.35)
      const cp = Math.cos(pitch * 0.35), sp = Math.sin(pitch * 0.35)
      const SS = D * 0.62
      for (const st of STARS) {
        const x1 = st.x * cy + st.z * sy, z1 = -st.x * sy + st.z * cy
        const y1 = st.y * cp - z1 * sp
        const a = st.base * (0.55 + 0.45 * Math.sin(t * 0.001 * st.sp + st.phase))
        ctx.fillStyle = `rgba(200,220,255,${a.toFixed(3)})`
        ctx.beginPath()
        ctx.arc(W / 2 + x1 * SS, H / 2 - y1 * SS, st.size, 0, 7)
        ctx.fill()
      }

      const tbl = propsRef.current.tables
      const fks = propsRef.current.foreignKeys

      // 每帧按当前 tables 重建几何（schema 异步加载后 tables 会变化）
      const counts = tbl.map((tt) => tt.row_count)
      const minC = counts.length ? Math.min(...counts) : 1
      const maxC = counts.length ? Math.max(...counts) : 1
      const n = tbl.length
      const POS = spherePositions(n)
      const RADII = tbl.map((tt) => radiusFor(tt.row_count, minC, maxC))
      const COLORS = tbl.map((tt) => colorFor(tt.name))
      geom.POS = POS
      geom.RADII = RADII
      geom.COLORS = COLORS

      // 天球赤道环：极淡参照线，给出"天球"轮廓
      const S = D * 0.42
      ctx.strokeStyle = 'rgba(110,150,190,0.09)'
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.ellipse(W / 2, H / 2, S, S * Math.abs(Math.cos(pitch)), 0, 0, 7)
      ctx.stroke()

      // projBy 按节点下标索引（供边端点/弹窗定位取用）；proj 仅按深度排序决定绘制次序。
      // 两者必须分开：sort 后 proj[a] 是"深度第 a 位"的节点而非节点 a，混用会导致
      // 边随旋转每帧接到不同节点对上（点亮错乱的历史 bug 根因）。
      const projBy = tbl.map((_, i) => ({ i, p: rotate(POS[i]) }))
      const proj = [...projBy].sort((a, b) => b.p.z - a.p.z)

      // 高亮锁定在"单击选中"的节点（维持态），不跟随 hover；未选中则全部压暗
      const focus = selectedRef.current
      const hlSet = focus != null ? neighborsOf(focus) : null

      // 连线：默认保留花色但压暗；仅当某端点为焦点（悬停/选中）节点时点亮
      fks.forEach((fk) => {
        const a = tbl.findIndex((t) => t.name === fk.table)
        const b = tbl.findIndex((t) => t.name === fk.ref_table)
        if (a < 0 || b < 0) return
        const A = projBy[a], B = projBy[b]
        const lit = focus != null && (a === focus || b === focus)
        // 端点收到圆的边缘外留 2px 间隙，避免连线穿透圆圈
        const ra = Math.max(MIN_R, RADII[a] * Math.min(1.5, A.p.scale))
        const rb = Math.max(MIN_R, RADII[b] * Math.min(1.5, B.p.scale))
        const dx = B.p.x - A.p.x, dy = B.p.y - A.p.y
        const len = Math.hypot(dx, dy)
        let ax = A.p.x, ay = A.p.y, bx = B.p.x, by = B.p.y
        if (len > ra + rb + 2) {
          const ux = dx / len, uy = dy / len, gap = 2
          ax = A.p.x + ux * (ra + gap); ay = A.p.y + uy * (ra + gap)
          bx = B.p.x - ux * (rb + gap); by = B.p.y - uy * (rb + gap)
        }
        ctx.strokeStyle = lit ? 'rgba(52,245,197,0.75)' : 'rgba(120,150,205,0.22)'
        ctx.lineWidth = lit ? 1.8 : 1
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke()
        const tm = (t / 1600 + (a * 0.13 + b * 0.07)) % 1
        const qx = ax + (bx - ax) * tm, qy = ay + (by - ay) * tm
        ctx.fillStyle = lit ? 'rgba(52,245,197,.95)' : 'rgba(120,150,205,.3)'
        ctx.beginPath(); ctx.arc(qx, qy, lit ? 2.1 : 1.2, 0, 7); ctx.fill()
      })

      // 节点：彩色柔光光环 + 景深明暗；焦点及其直接邻居点亮
      proj.forEach(({ i, p }) => {
        const lit = focus != null && (i === focus || (hlSet ? hlSet.has(i) : false))
        const isFocus = focus === i
        const r = Math.max(MIN_R, RADII[i] * Math.min(1.5, p.scale))
        const depth = Math.max(0, Math.min(1, (p.z + 1) / 2)) // 近=1 远=0
        if (isFocus) {
          const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 2.4)
          g.addColorStop(0, 'rgba(255,255,255,.5)'); g.addColorStop(1, COLORS[i] + '00')
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.4, 0, 7); ctx.fill()
        }
        const halo = lit ? 0.3 : 0.08 + depth * 0.12
        const hg = ctx.createRadialGradient(p.x, p.y, r * 0.6, p.x, p.y, r * 2.6)
        hg.addColorStop(0, COLORS[i] + Math.round(halo * 255).toString(16).padStart(2, '0'))
        hg.addColorStop(1, COLORS[i] + '00')
        ctx.fillStyle = hg
        ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.6, 0, 7); ctx.fill()
        ctx.globalAlpha = lit ? 1 : 0.35 + depth * 0.35
        ctx.fillStyle = COLORS[i]
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7); ctx.fill()
        ctx.lineWidth = (lit || isFocus) ? 2 : 1
        ctx.strokeStyle = isFocus ? 'rgba(255,255,255,.95)' : lit ? 'rgba(255,255,255,.6)' : 'rgba(255,255,255,.25)'
        ctx.stroke()
        const fs = isFocus ? 12.5 : Math.min(12, 9.5 + p.scale * 2.2)
        ctx.font = `600 ${fs}px var(--sans, sans-serif)`
        ctx.textAlign = 'center'
        ctx.globalAlpha = lit ? 1 : 0.45 + depth * 0.4
        ctx.fillStyle = lit ? '#fff' : 'rgba(190,205,225,.55)'
        ctx.shadowColor = 'rgba(5,7,13,.9)'; ctx.shadowBlur = 5
        ctx.fillText(tbl[i].name, p.x, p.y - r - 7)
        ctx.shadowBlur = 0
        ctx.globalAlpha = 1
      })
      requestAnimationFrame(draw)
    }
    requestAnimationFrame(draw)

    const hit = (px: number, py: number): number | null => {
      // 与绘制顺序一致（画家算法：近节点后画，盖在远节点上）。
      // 按深度 近->远 检测，命中即返回最上层可见节点，避免点到被遮挡的背侧节点。
      const order = propsRef.current.tables
        .map((_, i) => ({ i, p: rotate(geom.POS[i]) }))
        .sort((a, b) => a.p.z - b.p.z)
      for (const { i, p } of order) {
        const r = Math.max(MIN_R, geom.RADII[i] * p.scale) + 6
        if ((px - p.x) ** 2 + (py - p.y) ** 2 < r * r) return i
      }
      return null
    }

    const pt = (e: PointerEvent | MouseEvent) => {
      const rect = cv.getBoundingClientRect()
      return { x: e.clientX - rect.left, y: e.clientY - rect.top }
    }

    const onDown = (e: PointerEvent): void => {
      // 记录按下时是否仍在惯性漂移（先取速度再归零）
      downMoving = Math.abs(velY) + Math.abs(velP) > 0.0006
      dragging = pt(e); downPt = pt(e); moved = false
      velY = 0; velP = 0
      auto = 0; lastAct = performance.now() // 交互即停转（命中判定需要静止画面）
      downHit = hit(downPt.x, downPt.y) // 按下瞬间锁定目标：旋转已冻结，起按点即目标点
      cv.classList.add('drag'); cv.setPointerCapture(e.pointerId)
    }
    const onMove = (e: PointerEvent): void => {
      const p = pt(e)
      if (!dragging) {
        hover = hit(p.x, p.y)
        cv.style.cursor = hover != null ? 'pointer' : 'grab'
        return
      }
      lastAct = performance.now()
      const dx = p.x - dragging.x, dy = p.y - dragging.y
      dragging = p
      if (downPt && (p.x - downPt.x) ** 2 + (p.y - downPt.y) ** 2 > 16) moved = true
      yaw += dx * 0.006; pitch = Math.max(-1.2, Math.min(1.2, pitch + dy * 0.005))
      velY = dx * 0.006; velP = dy * 0.005
    }
    const onUp = (): void => {
      dragging = null; cv.classList.remove('drag')
      if (moved) hover = null // 旋转结束：清掉残存 hover，高亮回到选中态，避免乱亮别的节点
    }
    const onLeave = (): void => { dragging = null; cv.classList.remove('drag'); hover = null }
    const onWheel = (e: WheelEvent): void => {
      e.preventDefault()
      auto = 0; lastAct = performance.now()
      zoom = Math.max(0.5, Math.min(2.4, zoom * (e.deltaY < 0 ? 1.09 : 1 / 1.09)))
    }
    const onClick = (e: MouseEvent): void => {
      if (moved || downMoving) return // 拖拽旋转结束 / 止停漂移的轻点不触发选中
      const i = downHit
      hover = i // 点击后同步 hover 到实际点中的节点，避免残存 hover 覆盖 selected 导致高亮错位
      if (i != null) {
        selectedRef.current = i
        const now = Date.now()
        if (now - popPending > 300) {
          const q = rotate(geom.POS[i])
          propsRef.current.onSelectNode(propsRef.current.tables[i].name, q.x, q.y)
          popPending = now
        }
      } else if (propsRef.current.onClearSelection) {
        // 点击空白处：关闭弹窗并清除高亮
        propsRef.current.onClearSelection()
      }
    }
    const tick = setInterval(() => {
      if (!dragging) {
        // 惯性衰减 + 静止 1.2s 后自转速度缓升（0.02/tick 的淡入，避免猛地重启）
        const idle = performance.now() - lastAct > 1200
        const still = Math.abs(velY) + Math.abs(velP) < 0.0005
        auto += ((idle && still ? 1 : 0) - auto) * 0.02
        yaw += 0.0016 * auto + velY
        pitch += velP
        velY *= 0.93; velP *= 0.93
      }
    }, 16)

    cv.addEventListener('pointerdown', onDown)
    cv.addEventListener('pointermove', onMove)
    cv.addEventListener('pointerup', onUp)
    cv.addEventListener('pointerleave', onLeave)
    cv.addEventListener('wheel', onWheel, { passive: false })
    cv.addEventListener('click', onClick)

    return () => {
      ro.disconnect(); clearInterval(tick)
      cv.removeEventListener('pointerdown', onDown)
      cv.removeEventListener('pointermove', onMove)
      cv.removeEventListener('pointerup', onUp)
      cv.removeEventListener('pointerleave', onLeave)
      cv.removeEventListener('wheel', onWheel)
      cv.removeEventListener('click', onClick)
    }
  }, [])

  return <canvas ref={canvasRef} className="graph3d" />
}
