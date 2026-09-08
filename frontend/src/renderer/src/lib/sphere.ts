/**
 * 星图 / 知识图谱共享的 3D 球面投影工具。
 * Graph3D（首页星图）使用：
 * 调色板、按表名稳定取色、按行数算半径、fibonacci 球面布点、透视投影。
 * 两者各自的事件/绘制差异（编辑连线、工具栏跟随等）保留在各组件内。
 */

export const SPHERE_PALETTE = ['#4cc9f0', '#34f5c5', '#ffc46b', '#8b7cf8', '#ff8fb3']

export const MIN_R = 5
const MAX_R = MIN_R * 5 // 直径比封顶 5 倍

export function colorFor(name: string): string {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return SPHERE_PALETTE[h % SPHERE_PALETTE.length]
}

/** 按行数对数缩放节点半径（minC/maxC 为当前表集行数范围） */
export function radiusFor(rowCount: number, minC: number, maxC: number): number {
  const c = Math.max(1, rowCount)
  const lo = Math.log(Math.max(1, minC))
  const hi = Math.log(Math.max(2, maxC))
  const t = hi > lo ? (Math.log(c) - lo) / (hi - lo) : 0.5
  return MIN_R + (MAX_R - MIN_R) * Math.max(0, Math.min(1, t))
}

/** fibonacci 球面布点：返回 n 个均匀分布的 3D 点（单位球） */
export function spherePositions(n: number): [number, number, number][] {
  const GA = Math.PI * (3 - Math.sqrt(5))
  const out: [number, number, number][] = []
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / Math.max(1, n - 1)) * 2
    const rad = Math.sqrt(Math.max(0, 1 - y * y))
    const th = GA * i
    out.push([rad * Math.cos(th), y, rad * Math.sin(th)])
  }
  return out
}

export interface ProjParams {
  yaw: number
  pitch: number
  zoom: number
  /** 画布短边（用于透视缩放基准） */
  D: number
  W: number
  H: number
}

export interface Projected {
  x: number
  y: number
  z: number
  scale: number
}

/** 透视投影：单位球点 → 屏幕坐标（CSS 像素，原点画布中心） */
export function project(p: [number, number, number], pr: ProjParams): Projected {
  const { yaw, pitch, zoom, D, W, H } = pr
  const [x, y, z] = p
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch)
  const x1 = x * cy + z * sy, z1 = -x * sy + z * cy
  const y1 = y * cp - z1 * sp, z2 = y * sp + z1 * cp
  const f = 2.2 * zoom
  const k = f / (f - z2)
  const s = D * 0.42
  return { x: W / 2 + x1 * k * s, y: H / 2 - y1 * k * s, z: z2, scale: k }
}
