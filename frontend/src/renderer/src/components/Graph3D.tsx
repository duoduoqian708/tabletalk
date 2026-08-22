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
  onOpenData?: (name: string) => void
  /** 缩略模式（表数据视图打开时）：降帧渲染，让出主线程 */
  mini?: boolean
}

export function Graph3D({ tables, foreignKeys, onSelectNode, selectedName, onClearSelection, onOpenData, mini, paused: controlledPaused, onTogglePause }: Props & { paused?: boolean; onTogglePause?: () => void }): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const propsRef = useRef({ tables, foreignKeys, onSelectNode, onClearSelection, onOpenData })
  propsRef.current = { tables, foreignKeys, onSelectNode, onClearSelection, onOpenData }
  const miniRef = useRef(mini)
  miniRef.current = mini
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

  // D2: pause - 受控时由外层驱动（按钮外置避免随 g3d-wrap 位移），否则内部自治
  const [internalPaused, setInternalPaused] = useState(() => {
    try {
      return sessionStorage.getItem('tabletalk-graph-paused') === '1'
    } catch {
      return false
    }
  })
  const isControlled = typeof controlledPaused !== 'undefined'
  const paused = isControlled ? (controlledPaused as boolean) : internalPaused
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
  // 性能：缓存球面布点与半径/颜色，避免每帧重算（仅表集变更时重算）
  const baseCacheRef = useRef<{ n: number; basePos: [number, number, number][]; radii: number[]; colors: string[]; minC: number; maxC: number } | null>(null)
  const idxMapRef = useRef<Map<string, number>>(new Map())

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
    // 已去掉拉伸：保持原位，不再外扩
    scatterTargetRef.current = 0
  }, [selectedName])

  // 缓存几何：表集变更时一次性计算，避免每帧分配
  useEffect(() => {
    const tbl = tables
    const n = tbl.length
    if (n === 0) {
      baseCacheRef.current = null
      idxMapRef.current = new Map()
      return
    }
    const counts = tbl.map((tt) => tt.row_count)
    const minC = Math.min(...counts)
    const maxC = Math.max(...counts)
    baseCacheRef.current = {
      n,
      basePos: spherePositions(n),
      radii: tbl.map((tt) => radiusFor(tt.row_count, minC, maxC)),
      colors: tbl.map((tt) => colorFor(tt.name)),
      minC,
      maxC
    }
    const m = new Map<string, number>()
    tbl.forEach((t, i) => m.set(t.name, i))
    idxMapRef.current = m
  }, [tables])

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

    // 立即点亮关联（不等飞行结束，用户反馈“要缓一会才亮”）
    selectedRef.current = idx
    // 触发外层高亮，弹窗初始位置先给 0,0，下一帧跟随会校正到投影
    propsRef.current.onSelectNode(name, 0, 0)
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

    // ---- 性能：一次性烘焙贴图（sprite），替代每帧每节点的 createRadialGradient / shadowBlur ----
    // createRadialGradient 每帧每节点分配渐变对象、shadowBlur 走软件渲染路径，是 2D 画布最重的两类操作；
    // 预烘焙成小画布后每帧只做 drawImage（GPU 合成），大表集下帧耗从数十毫秒降到个位数。
    const SPR = 96
    const mkSprite = (paint: (c: CanvasRenderingContext2D, s: number) => void): HTMLCanvasElement => {
      const el = document.createElement('canvas')
      el.width = SPR; el.height = SPR
      const c = el.getContext('2d')
      if (c) paint(c, SPR)
      return el
    }
    // 星空微光贴图（替代每帧 130 次路径填充与 rgba 字符串拼接）
    const starSprite = mkSprite((c, sz) => {
      const cx = sz / 2
      const g = c.createRadialGradient(cx, cx, 0, cx, cx, cx)
      g.addColorStop(0, '#c8dcff'); g.addColorStop(0.35, '#c8dcff'); g.addColorStop(1, 'rgba(200,220,255,0)')
      c.fillStyle = g; c.fillRect(0, 0, sz, sz)
    })

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

    // 邻居集合缓存：选中期间每帧都要点亮相邻节点，重建 Set + 遍历全部 FK 的代价不必要
    let hlCache: { focus: number; fkLen: number; n: number; set: Set<number> } | null = null
    const neighborsOf = (idx: number): Set<number> => {
      const fks = propsRef.current.foreignKeys
      const n = propsRef.current.tables.length
      if (hlCache && hlCache.focus === idx && hlCache.fkLen === fks.length && hlCache.n === n) {
        return hlCache.set
      }
      const set = new Set<number>([idx])
      const mp = idxMapRef.current
      // 优先走索引映射，O(1) 查找，回退 findIndex 兼容旧路径
      fks.forEach((fk) => {
        const a = mp.get(fk.table) ?? propsRef.current.tables.findIndex((t) => t.name === fk.table)
        const b = mp.get(fk.ref_table) ?? propsRef.current.tables.findIndex((t) => t.name === fk.ref_table)
        if (a === idx && b >= 0) set.add(b)
        if (b === idx && a >= 0) set.add(a)
      })
      hlCache = { focus: idx, fkLen: fks.length, n, set }
      return set
    }

    const geom: { POS: [number, number, number][]; RADII: number[]; COLORS: string[] } = {
      POS: [],
      RADII: [],
      COLORS: []
    }

    let raf = 0
    let frame = 0
    let lastFs = -1
    // 复用投影缓冲：projBy 按节点下标索引（边端点/弹窗取用）；orderIdx 是深度序下标数组。
    // sort 后按下标取 projBy[a] 仍是节点 a（历史高亮错乱 bug 的教训），深度序只存在 orderIdx 里。
    let projBy: { i: number; p: ReturnType<typeof rotate> }[] = []
    const orderIdx: number[] = []
    const draw = (t: number): void => {
      // 非可见时降频：页面隐藏时跳过重绘，节省 CPU/电量
      if (typeof document !== 'undefined' && document.hidden) {
        raf = requestAnimationFrame(draw)
        return
      }
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

      // 已去掉拉伸：保持原位
      scatterRef.current = 0

      // 搜索闪烁过期清理
      if (searchFlashRef.current && now > flashUntilRef.current) {
        // 不自动清 selectedRef，仅清 flash（点亮颜色恢复普通）
      }

      // 缩略模式（表数据视图打开时）隔帧渲染：后台角标 ~30fps，把主线程让给前景表格交互
      frame += 1
      if (miniRef.current && frame % 2 === 1) {
        raf = requestAnimationFrame(draw)
        return
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
        const s = st.size * 3.2
        ctx.globalAlpha = a
        ctx.drawImage(starSprite, W / 2 + x1 * SS - s / 2, H / 2 - y1 * SS - s / 2, s, s)
      }
      ctx.globalAlpha = 1

      const tbl = propsRef.current.tables
      const fks = propsRef.current.foreignKeys
      const n = tbl.length
      // 使用缓存的几何，避免每帧重算与分配
      const cached = baseCacheRef.current
      const idxMap = idxMapRef.current
      const basePos = cached && cached.n === n ? cached.basePos : spherePositions(n)
      const RADII = cached && cached.n === tbl.length ? cached.radii : tbl.map((tt) => {
        const counts = tbl.map((x) => x.row_count)
        const minC0 = Math.min(...counts)
        const maxC0 = Math.max(...counts)
        return radiusFor(tt.row_count, minC0, maxC0)
      })
      const COLORS = cached && cached.n === tbl.length ? cached.colors : tbl.map((tt) => colorFor(tt.name))

      // 已去掉拉伸：保持原位，仅高亮
      const focus = selectedRef.current
      const hlSet = focus != null ? neighborsOf(focus) : null
      const POS: [number, number, number][] = basePos

      geom.POS = POS
      geom.RADII = RADII
      geom.COLORS = COLORS

      const S = D * 0.42
      ctx.strokeStyle = 'rgba(110,150,190,0.09)'
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.ellipse(W / 2, H / 2, S, S * Math.abs(Math.cos(pitchRef.current)), 0, 0, 7)
      ctx.stroke()

      // 投影复用缓冲，避免每帧分配对象数组；orderIdx 升序 z（远者先画，近者后画在上层=正确遮挡序）
      if (projBy.length !== n) {
        projBy = tbl.map((_, i) => ({ i, p: rotate(POS[i]) }))
      } else {
        for (let i = 0; i < n; i++) {
          projBy[i].i = i
          projBy[i].p = rotate(POS[i])
        }
      }
      if (orderIdx.length !== n) {
        orderIdx.length = 0
        for (let i = 0; i < n; i++) orderIdx.push(i)
      }
      orderIdx.sort((a, b) => projBy[a].p.z - projBy[b].p.z)

      // 弹窗跟随：选中点的投影随图转动，命令式更新 .node-pop 位置（免 React 60fps 重渲染）
      if (selectedRef.current != null && !miniRef.current) {
        const selIdx = selectedRef.current
        if (selIdx >= 0 && selIdx < n) {
          const qp = projBy[selIdx].p
          const popEl = document.querySelector('.node-pop') as HTMLElement | null
          if (popEl) {
            const wrap = document.querySelector('.g3d-wrap') as HTMLElement | null
            const w = wrap ? wrap.clientWidth : W
            const h = wrap ? wrap.clientHeight : H
            const POP_W = 296, POP_H = 196
            let px = qp.x + 16, py = qp.y - 24
            px = Math.min(Math.max(8, px), Math.max(8, w - POP_W - 8))
            py = Math.min(Math.max(8, py), Math.max(8, h - POP_H - 8))
            popEl.style.left = `${px}px`
            popEl.style.top = `${py}px`
            popEl.style.opacity = '1'
            popEl.style.pointerEvents = 'auto'
          }
        }
      }

      // searchFlash 临时高亮（琥珀色）
      const flashName = searchFlashRef.current && now < flashUntilRef.current ? searchFlashRef.current : null
      const flashIdx = flashName ? tbl.findIndex((tt) => tt.name === flashName) : -1

      fks.forEach((fk) => {
        const a = idxMap.get(fk.table) ?? tbl.findIndex((t) => t.name === fk.table)
        const b = idxMap.get(fk.ref_table) ?? tbl.findIndex((t) => t.name === fk.ref_table)
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

      for (let oi = 0; oi < orderIdx.length; oi++) {
        const i = orderIdx[oi]
        const p = projBy[i].p
        const isFlash = i === flashIdx
        const lit = focus != null && (i === focus || (hlSet ? hlSet.has(i) : false))
        const isFocus = focus === i
        const r = Math.max(MIN_R, RADII[i] * Math.min(1.5, p.scale))
        const depth = Math.max(0, Math.min(1, (p.z + 1) / 2))
        const col = COLORS[i]
        if (isFlash && !isFocus) {
          // 搜索闪烁干净圈（保持本色，仅外圈琥珀）- 选中时不黄，保持白圈
          ctx.globalAlpha = 1
          ctx.strokeStyle = 'rgba(255,180,84,0.95)'
          ctx.lineWidth = 1.8
          ctx.beginPath()
          ctx.arc(p.x, p.y, r + 3.8, 0, Math.PI * 2)
          ctx.stroke()
        }
        ctx.globalAlpha = lit || isFlash ? 1 : 0.35 + depth * 0.35
        ctx.fillStyle = col
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7); ctx.fill()
        if (isFocus) {
          // 干净白圈，无光晕，保持本色（选中优先，搜索黄不覆盖白）
          ctx.globalAlpha = 1
          ctx.strokeStyle = 'rgba(255,255,255,0.92)'
          ctx.lineWidth = 1.8
          ctx.beginPath()
          ctx.arc(p.x, p.y, r + 3.4, 0, Math.PI * 2)
          ctx.stroke()
        } else if (!isFlash) {
          ctx.lineWidth = lit ? 2 : 1
          ctx.strokeStyle = lit ? 'rgba(255,255,255,.6)' : 'rgba(255,255,255,.25)'
          ctx.stroke()
        } else {
          // isFlash 已在上方画过琥珀圈，节点本身描边保持与 lit 一致
          ctx.lineWidth = 2
          ctx.strokeStyle = 'rgba(255,180,84,.95)'
          ctx.stroke()
        }
        if (!miniRef.current) {
          const fs = isFocus ? 12.8 : Math.min(12, 9.5 + p.scale * 2.2)
          if (fs !== lastFs) {
            ctx.font = `600 ${fs}px var(--sans, sans-serif)`
            lastFs = fs
          }
          ctx.textAlign = 'center'
          ctx.globalAlpha = lit || isFlash ? 1 : 0.45 + depth * 0.4
          // 深色底衬两次 fillText 替代 shadowBlur（阴影走软件渲染路径，是每帧文字绘制的性能悬崖）
          ctx.fillStyle = 'rgba(5,7,13,.9)'
          ctx.fillText(tbl[i].name, p.x + 1, p.y - r - 6)
          ctx.fillStyle = lit || isFlash ? '#fff' : 'rgba(190,205,225,.55)'
          ctx.fillText(tbl[i].name, p.x, p.y - r - 7)
          ctx.globalAlpha = 1
        }
      }
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)

    const hit = (px: number, py: number): number | null => {
      // 复用绘制帧已算好的深度序投影（从近到远测试），不再在每次 pointermove 上
      // 全量重投影 + 排序——高刷指针下该事件每秒可触发 200+ 次，曾是悬停卡顿的主要来源。
      // 上一帧投影最多滞后 16ms，自转速度下肉眼不可辨。
      for (let k = orderIdx.length - 1; k >= 0; k--) {
        const i = orderIdx[k]
        const p = projBy[i]?.p
        if (!p) continue
        const r = Math.max(MIN_R, geom.RADII[i] * p.scale) + 6
        if ((px - p.x) ** 2 + (py - p.y) ** 2 < r * r) return i
      }
      return null
    }

    // offsetX/offsetY 由浏览器直接给出（相对目标元素），免去每次事件 getBoundingClientRect 的布局查询
    const pt = (e: PointerEvent | MouseEvent): { x: number; y: number } => {
      return { x: e.offsetX, y: e.offsetY }
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
      // 双击的第二次 click（detail===2）交由 dblclick 处理，避免“选中→立即取消”的闪烁
      if ((e as MouseEvent).detail === 2) return
      const i = downHit
      hover = i
      if (i != null) {
        if (selectedRef.current === i) {
          // 再点已点亮的点 → 取消点亮
          selectedRef.current = null
          scatterTargetRef.current = 0
          propsRef.current.onClearSelection?.()
          return
        }
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
    const onDblClick = (e: MouseEvent): void => {
      const p = pt(e as unknown as PointerEvent)
      const i = hit(p.x, p.y)
      if (i != null && propsRef.current.onOpenData) {
        // 双击直接开表（无需先经弹窗）
        const name = propsRef.current.tables[i].name
        // 先保证选中态
        selectedRef.current = i
        scatterTargetRef.current = 1
        propsRef.current.onSelectNode(name, 0, 0)
        propsRef.current.onOpenData(name)
      } else if (i != null) {
        // 兼容未传 onOpenData 时退化为单击选中
        const q = rotate(geom.POS[i])
        propsRef.current.onSelectNode(propsRef.current.tables[i].name, q.x, q.y)
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
    cv.addEventListener('dblclick', onDblClick)

    return () => {
      ro.disconnect(); clearInterval(tick); cancelAnimationFrame(raf)
      cv.removeEventListener('pointerdown', onDown)
      cv.removeEventListener('pointermove', onMove)
      cv.removeEventListener('pointerup', onUp)
      cv.removeEventListener('pointerleave', onLeave)
      cv.removeEventListener('wheel', onWheel)
      cv.removeEventListener('click', onClick)
      cv.removeEventListener('dblclick', onDblClick)
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
      {/* 暂停/恢复 - 受控时由外层按钮接管，避免随 g3d-wrap 位移 */}
      {!isControlled && (
        <button
          className={`g3d-pause ${paused ? 'is-paused' : ''}`}
          title={paused ? t('graph.resume') : t('graph.pause')}
          onClick={() => setInternalPaused((v) => !v)}
          aria-label={paused ? t('graph.resume') : t('graph.pause')}
        >
          {paused ? '▶' : '⏸'}
        </button>
      )}
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
