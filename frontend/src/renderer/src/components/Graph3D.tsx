import { useEffect, useMemo, useRef, useState } from 'react'
import { MIN_R, colorFor, radiusFor, spherePositions, project } from '@renderer/lib/sphere'
import { useI18n } from '@renderer/store/i18n'

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
  selectedName?: string | null
  onClearSelection?: () => void
}

export function Graph3D({ tables, foreignKeys, onSelectNode, selectedName, onClearSelection }: Props): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const propsRef = useRef({ tables, foreignKeys, onSelectNode, onClearSelection })
  propsRef.current = { tables, foreignKeys, onSelectNode, onClearSelection }
  const selectedRef = useRef<number | null>(null)
  const { t } = useI18n()

  // D1: search
  const [q, setQ] = useState('')
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const [searchFlash, setSearchFlash] = useState<string | null>(null)

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
    return match.slice(0, 8)
  }, [tables, q])

  // D2: pause
  const [paused, setPaused] = useState(() => {
    try {
      return sessionStorage.getItem('tabletalk-graph-paused') === '1'
    } catch {
      return false
    }
  })
  // D4: onboarding
  const [onboardVisible, setOnboardVisible] = useState(() => {
    try {
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false
      return !localStorage.getItem('tabletalk-graph-onboarded')
    } catch {
      return false
    }
  })

  // canvas 内部可变状态的 refs（供 UI 与动画循环共享）
  const yawRef = useRef(0.55)
  const pitchRef = useRef(0.35)
  const zoomRef = useRef(1)
  const pausedRef = useRef(paused)
  const searchFlashRef = useRef<string | null>(null)
  const flashUntilRef = useRef(0)
  // 飞行状态
  const flyingRef = useRef<{
    active: boolean
    start: number
    dur: number
    from: { yaw: number; pitch: number; zoom: number }
    to: { yaw: number; pitch: number; zoom: number }
  } | null>(null)
  // 散开进度
  const scatterRef = useRef(0)
  const scatterTargetRef = useRef(0)
  // onboarding 飞行
  const onboardRef = useRef<{ active: boolean; start: number; fromYaw: number } | null>(null)
  const onboardSetterRef = useRef(setOnboardVisible)
  onboardSetterRef.current = setOnboardVisible

  useEffect(() => {
    pausedRef.current = paused
    try {
      sessionStorage.setItem('tabletalk-graph-paused', paused ? '1' : '0')
    } catch {}
  }, [paused])

  useEffect(() => {
    searchFlashRef.current = searchFlash
    if (searchFlash) {
      flashUntilRef.current = performance.now() + 1800
    }
  }, [searchFlash])

  useEffect(() => {
    selectedRef.current = selectedName
      ? propsRef.current.tables.findIndex((t) => t.name === selectedName)
      : null
    scatterTargetRef.current = selectedName ? 1 : 0
  }, [selectedName])

  // 触发搜索飞行的函数（暴露给事件处理器）
  const flyTo = (name: string) => {
    const idx = propsRef.current.tables.findIndex((tt) => tt.name === name)
    if (idx < 0) return
    const n = propsRef.current.tables.length
    if (n === 0) return
    const POS = spherePositions(n)
    const [x, y, z] = POS[idx]
    // 计算目标 yaw/pitch（把该点转到前方中心）
    const yawTo = Math.atan2(-x, z)
    const z1 = -x * Math.sin(yawTo) + z * Math.cos(yawTo)
    const pitchTo = Math.atan2(y, z1)
    // 最短 yaw 路径
    let curYaw = yawRef.current
    let delta = yawTo - curYaw
    while (delta > Math.PI) delta -= Math.PI * 2
    while (delta < -Math.PI) delta += Math.PI * 2
    const yawFinal = curYaw + delta
    // pitch 直接差值（范围 -1.2~1.2）
    let curPitch = pitchRef.current
    curPitch = Math.max(-1.2, Math.min(1.2, curPitch))
    const pitchFinal = Math.max(-1.2, Math.min(1.2, pitchTo))

    // 若开启 reduced-motion 则瞬切
    const reduce = (() => {
      try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return false }
    })()
    if (reduce) {
      yawRef.current = yawFinal
      pitchRef.current = pitchFinal
      zoomRef.current = 1.22
      setSearchFlash(name)
      propsRef.current.onSelectNode(name, 0, 0)
      selectedRef.current = idx
      scatterTargetRef.current = 1
      return
    }

    flyingRef.current = {
      active: true,
      start: performance.now(),
      dur: 980,
      from: { yaw: yawRef.current, pitch: pitchRef.current, zoom: zoomRef.current },
      to: { yaw: yawFinal, pitch: pitchFinal, zoom: 1.28 },
    }
    setSearchFlash(name)
    // 关闭下拉，保持输入
    setDropdownOpen(false)
  }

  // C5 可追溯：外部通过 tabletalk:do-locate 触发飞向定位（与搜索同链路）
  useEffect(() => {
    const h = (e: Event): void => {
      const tbl = (e as CustomEvent).detail?.table as string | undefined
      if (tbl) flyTo(tbl)
    }
    window.addEventListener('tabletalk:do-locate', h as unknown as EventListener)
    return () => window.removeEventListener('tabletalk:do-locate', h as unknown as EventListener)
  }, [])

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext('2d')
    if (!ctx) return

    let W = 0, H = 0, D = 0, dpr = 1
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

    let dragging: { x: number; y: number } | null = null
    let downPt: { x: number; y: number } | null = null
    let moved = false
    let velY = 0, velP = 0
    let hover: number | null = null
    let popPending = 0
    let downHit: number | null = null
    let downMoving = false
    let auto = 1
    let lastAct = performance.now()

    // onboarding: 首次进入 4s 飞行（仅首次，未标记已看过时）
    const shouldOnboard = (() => {
      try {
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false
        return !localStorage.getItem('tabletalk-graph-onboarded')
      } catch { return false }
    })()
    if (shouldOnboard) {
      onboardRef.current = { active: true, start: performance.now(), fromYaw: yawRef.current }
    }

    const isReducedMotion = (): boolean => {
      try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return false }
    }

    const rotate = (p: [number, number, number]) => project(p, { yaw: yawRef.current, pitch: pitchRef.current, zoom: zoomRef.current, D, W, H })

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

    const geom: { POS: [number, number, number][]; RADII: number[]; COLORS: string[] } = {
      POS: [],
      RADII: [],
      COLORS: []
    }

    let raf = 0
    const draw = (t: number): void => {
      // 飞行插值
      const now = performance.now()
      // onboarding 飞行（4s，自转一周 + 缓慢拉近）
      if (onboardRef.current?.active) {
        const el = now - onboardRef.current.start
        const p = Math.min(1, el / 4000)
        const ease = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2 // easeInOut
        yawRef.current = onboardRef.current.fromYaw + ease * Math.PI * 2
        zoomRef.current = 1 + ease * 0.28
        if (p >= 1) {
          onboardRef.current.active = false
          try { localStorage.setItem('tabletalk-graph-onboarded', '1') } catch {}
          onboardSetterRef.current(false)
        }
      }
      // 搜索飞行
      if (flyingRef.current?.active) {
        const f = flyingRef.current
        const el = now - f.start
        const p = Math.min(1, el / f.dur)
        // easeOutCubic
        const e = 1 - Math.pow(1 - p, 3)
        yawRef.current = f.from.yaw + (f.to.yaw - f.from.yaw) * e
        pitchRef.current = f.from.pitch + (f.to.pitch - f.from.pitch) * e
        zoomRef.current = f.from.zoom + (f.to.zoom - f.from.zoom) * e
        if (p >= 1) {
          flyingRef.current.active = false
          yawRef.current = f.to.yaw
          pitchRef.current = f.to.pitch
          zoomRef.current = f.to.zoom
          // 飞行结束：选中并点亮（但不弹卡，由上层 onSelectNode 控制；此处仅高亮）
          const name = searchFlashRef.current
          if (name) {
            const idx = propsRef.current.tables.findIndex((tt) => tt.name === name)
            if (idx >= 0) {
              selectedRef.current = idx
              const q = rotate(spherePositions(propsRef.current.tables.length)[idx])
              // 触发选中回调（定位）
              // 避免频繁弹窗防抖
              const nowMs = Date.now()
              if (nowMs - popPending > 300) {
                propsRef.current.onSelectNode(name, q.x, q.y)
                popPending = nowMs
              }
            }
          }
        }
      }

      // 散开进度插值（可打断，~1s 缓动）
      const red = isReducedMotion()
      if (red) {
        scatterRef.current = scatterTargetRef.current
      } else {
        scatterRef.current += (scatterTargetRef.current - scatterRef.current) * 0.07
        if (Math.abs(scatterRef.current - scatterTargetRef.current) < 0.001) scatterRef.current = scatterTargetRef.current
      }

      // 搜索闪烁过期清理
      if (searchFlashRef.current && now > flashUntilRef.current) {
        // 不自动清 selectedRef，仅清 flash（点亮颜色恢复普通）
      }

      if (bg) ctx.drawImage(bg, 0, 0, W, H)
      else ctx.clearRect(0, 0, W, H)

      const cy = Math.cos(yawRef.current * 0.35), sy = Math.sin(yawRef.current * 0.35)
      const cp = Math.cos(pitchRef.current * 0.35), sp = Math.sin(pitchRef.current * 0.35)
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

      const counts = tbl.map((tt) => tt.row_count)
      const minC = counts.length ? Math.min(...counts) : 1
      const maxC = counts.length ? Math.max(...counts) : 1
      const n = tbl.length
      const basePos = spherePositions(n)
      const RADII = tbl.map((tt) => radiusFor(tt.row_count, minC, maxC))
      const COLORS = tbl.map((tt) => colorFor(tt.name))

      // 散开：对选中节点的直接邻居径向外扩 20-30%
      const focus = selectedRef.current
      const hlSet = focus != null ? neighborsOf(focus) : null
      const scatter = scatterRef.current
      const POS: [number, number, number][] = basePos.map(([x, y, z], i) => {
        if (focus != null && hlSet && hlSet.has(i) && i !== focus && scatter > 0.001) {
          const s = 1 + 0.26 * scatter
          return [x * s, y * s, z * s]
        }
        return [x, y, z]
      })

      geom.POS = POS
      geom.RADII = RADII
      geom.COLORS = COLORS

      const S = D * 0.42
      ctx.strokeStyle = 'rgba(110,150,190,0.09)'
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.ellipse(W / 2, H / 2, S, S * Math.abs(Math.cos(pitchRef.current)), 0, 0, 7)
      ctx.stroke()

      const projBy = tbl.map((_, i) => ({ i, p: rotate(POS[i]) }))
      const proj = [...projBy].sort((a, b) => b.p.z - a.p.z)

      // searchFlash 临时高亮（琥珀色）
      const flashName = searchFlashRef.current && now < flashUntilRef.current ? searchFlashRef.current : null
      const flashIdx = flashName ? tbl.findIndex((tt) => tt.name === flashName) : -1

      fks.forEach((fk) => {
        const a = tbl.findIndex((t) => t.name === fk.table)
        const b = tbl.findIndex((t) => t.name === fk.ref_table)
        if (a < 0 || b < 0) return
        const A = projBy[a], B = projBy[b]
        const lit = focus != null && (a === focus || b === focus)
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

      proj.forEach(({ i, p }) => {
        const isFlash = i === flashIdx
        const lit = focus != null && (i === focus || (hlSet ? hlSet.has(i) : false))
        const isFocus = focus === i
        const r = Math.max(MIN_R, RADII[i] * Math.min(1.5, p.scale))
        const depth = Math.max(0, Math.min(1, (p.z + 1) / 2))
        if (isFlash) {
          const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 2.8)
          g.addColorStop(0, 'rgba(255,180,84,.55)'); g.addColorStop(1, '#ffb45400')
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.8, 0, 7); ctx.fill()
        } else if (isFocus) {
          const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 2.4)
          g.addColorStop(0, 'rgba(255,255,255,.5)'); g.addColorStop(1, COLORS[i] + '00')
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.4, 0, 7); ctx.fill()
        }
        const halo = isFlash ? 0.45 : lit ? 0.3 : 0.08 + depth * 0.12
        const hg = ctx.createRadialGradient(p.x, p.y, r * 0.6, p.x, p.y, r * 2.6)
        const col = isFlash ? '#ffb454' : COLORS[i]
        hg.addColorStop(0, col + Math.round(halo * 255).toString(16).padStart(2, '0'))
        hg.addColorStop(1, col + '00')
        ctx.fillStyle = hg
        ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.6, 0, 7); ctx.fill()
        ctx.globalAlpha = lit || isFlash ? 1 : 0.35 + depth * 0.35
        ctx.fillStyle = isFlash ? '#ffb454' : COLORS[i]
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7); ctx.fill()
        ctx.lineWidth = (lit || isFocus || isFlash) ? 2 : 1
        ctx.strokeStyle = isFlash ? 'rgba(255,180,84,.95)' : isFocus ? 'rgba(255,255,255,.95)' : lit ? 'rgba(255,255,255,.6)' : 'rgba(255,255,255,.25)'
        ctx.stroke()
        const fs = isFocus ? 12.5 : Math.min(12, 9.5 + p.scale * 2.2)
        ctx.font = `600 ${fs}px var(--sans, sans-serif)`
        ctx.textAlign = 'center'
        ctx.globalAlpha = lit || isFlash ? 1 : 0.45 + depth * 0.4
        ctx.fillStyle = lit || isFlash ? '#fff' : 'rgba(190,205,225,.55)'
        ctx.shadowColor = 'rgba(5,7,13,.9)'; ctx.shadowBlur = 5
        ctx.fillText(tbl[i].name, p.x, p.y - r - 7)
        ctx.shadowBlur = 0
        ctx.globalAlpha = 1
      })
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)

    const hit = (px: number, py: number): number | null => {
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

    const cancelFlight = (): void => {
      if (flyingRef.current?.active) flyingRef.current.active = false
      if (onboardRef.current?.active) {
        onboardRef.current.active = false
        try { localStorage.setItem('tabletalk-graph-onboarded', '1') } catch {}
        onboardSetterRef.current(false)
      }
    }

    const onDown = (e: PointerEvent): void => {
      downMoving = Math.abs(velY) + Math.abs(velP) > 0.0006
      dragging = pt(e); downPt = pt(e); moved = false
      velY = 0; velP = 0
      auto = 0; lastAct = performance.now()
      downHit = hit(downPt.x, downPt.y)
      cv.classList.add('drag'); cv.setPointerCapture(e.pointerId)
      cancelFlight()
      // onboarding 任意交互立即打断且永不重放
      if (onboardRef.current?.active) {
        try { localStorage.setItem('tabletalk-graph-onboarded', '1') } catch {}
      }
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
      yawRef.current += dx * 0.006; pitchRef.current = Math.max(-1.2, Math.min(1.2, pitchRef.current + dy * 0.005))
      velY = dx * 0.006; velP = dy * 0.005
    }
    const onUp = (): void => {
      dragging = null; cv.classList.remove('drag')
      if (moved) hover = null
    }
    const onLeave = (): void => { dragging = null; cv.classList.remove('drag'); hover = null }
    const onWheel = (e: WheelEvent): void => {
      e.preventDefault()
      auto = 0; lastAct = performance.now()
      zoomRef.current = Math.max(0.5, Math.min(2.4, zoomRef.current * (e.deltaY < 0 ? 1.09 : 1 / 1.09)))
      cancelFlight()
    }
    const onClick = (e: MouseEvent): void => {
      if (moved || downMoving) return
      const i = downHit
      hover = i
      if (i != null) {
        selectedRef.current = i
        scatterTargetRef.current = 1
        const now = Date.now()
        if (now - popPending > 300) {
          const q = rotate(geom.POS[i])
          propsRef.current.onSelectNode(propsRef.current.tables[i].name, q.x, q.y)
          popPending = now
        }
      } else if (propsRef.current.onClearSelection) {
        propsRef.current.onClearSelection()
        selectedRef.current = null
        scatterTargetRef.current = 0
      }
    }
    const tick = setInterval(() => {
      if (!dragging) {
        const idle = performance.now() - lastAct > 1200
        const still = Math.abs(velY) + Math.abs(velP) < 0.0005
        const shouldSpin = !pausedRef.current && !flyingRef.current?.active && !onboardRef.current?.active
        const targetAuto = shouldSpin && idle && still ? 1 : 0
        auto += (targetAuto - auto) * 0.02
        if (!isReducedMotion()) {
          yawRef.current += 0.0016 * auto + velY
          pitchRef.current += velP
          pitchRef.current = Math.max(-1.2, Math.min(1.2, pitchRef.current))
        }
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
      ro.disconnect(); clearInterval(tick); cancelAnimationFrame(raf)
      cv.removeEventListener('pointerdown', onDown)
      cv.removeEventListener('pointermove', onMove)
      cv.removeEventListener('pointerup', onUp)
      cv.removeEventListener('pointerleave', onLeave)
      cv.removeEventListener('wheel', onWheel)
      cv.removeEventListener('click', onClick)
    }
  }, [])

  const handleSearchKey = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Enter') {
      const name = filtered[0]?.name ?? q.trim()
      if (name && tables.some((tb) => tb.name === name)) {
        flyTo(name)
      } else if (filtered.length > 0) {
        flyTo(filtered[0].name)
      }
    } else if (e.key === 'Escape') {
      setDropdownOpen(false)
      ;(e.target as HTMLInputElement).blur()
    }
  }

  return (
    <>
      <canvas ref={canvasRef} className="graph3d" />
      {/* 搜索 */}
      <div className="g3d-search">
        <input
          className="g3d-search-input"
          placeholder={t('graph.searchPlaceholder')}
          value={q}
          onChange={(e) => { setQ(e.target.value); setDropdownOpen(true) }}
          onFocus={() => setDropdownOpen(true)}
          onBlur={() => setTimeout(() => setDropdownOpen(false), 150)}
          onKeyDown={handleSearchKey}
        />
        {dropdownOpen && q.trim() && (
          <div className="g3d-search-drop">
            {filtered.length === 0 ? (
              <div className="g3d-search-empty">{t('graph.searchNoResult')}</div>
            ) : (
              filtered.map((tb) => (
                <button
                  key={tb.name}
                  className="g3d-search-item"
                  onMouseDown={(ev) => { ev.preventDefault(); flyTo(tb.name) }}
                >
                  <span className="mono">{tb.name}</span>
                  <span className="g3d-search-meta">{tb.row_count} 行 · {tb.column_count} 列</span>
                </button>
              ))
            )}
          </div>
        )}
      </div>
      {/* 暂停/恢复 */}
      <button
        className={`g3d-pause ${paused ? 'is-paused' : ''}`}
        title={paused ? t('graph.resume') : t('graph.pause')}
        onClick={() => setPaused((v) => !v)}
        aria-label={paused ? t('graph.resume') : t('graph.pause')}
      >
        {paused ? '▶' : '⏸'}
      </button>
      {/* Onboarding */}
      {onboardVisible && (
        <div
          className="g3d-onboard"
          onClick={() => {
            try { localStorage.setItem('tabletalk-graph-onboarded', '1') } catch {}
            setOnboardVisible(false)
            if (onboardRef.current) onboardRef.current.active = false
          }}
        >
          <span className="g3d-onboard-dot" />
          <span>{t('graph.onboarding')}</span>
          <span className="g3d-onboard-dismiss">✕</span>
        </div>
      )}
    </>
  )
}
