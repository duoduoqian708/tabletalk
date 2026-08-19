import { create } from 'zustand'
import type { KnowledgeOverview, SchemaResponse } from '@renderer/api/types'

export type GraphMode = 'browse' | 'build' | 'govern'

export interface GraphNode {
  id: string
  name: string
  domain: string | null
  color: string
  fields: number
  annotated: boolean
  pending: boolean
  x: number
  y: number
}

export interface GraphEdgeDef {
  from: string
  to: string
  label: string
}

/** 搭查模式沉淀的查询节点（可重跑、可复制） */
export interface QueryNode {
  id: string
  title: string
  sql: string
  tables: string[]
  ts: string
}

export interface Viewport {
  scale: number
  tx: number
  ty: number
}

interface GraphState {
  nodes: GraphNode[]
  edges: GraphEdgeDef[]
  /** 内容包围盒（构建时算好，供 fit 用） */
  bounds: { minX: number; minY: number; maxX: number; maxY: number }
  viewport: Viewport
  mode: GraphMode
  selected: string | null
  /** 构建序号：每次 build 递增，fit 依赖它重跑（内容或挂载变化时） */
  seq: number
  build: (schema: SchemaResponse, overview: KnowledgeOverview | null) => void
  setMode: (m: GraphMode) => void
  select: (id: string | null) => void
  setViewport: (v: Viewport) => void
  /** 搭查（积木台）：多选表集合 */
  buildSel: string[]
  toggleBuild: (t: string) => void
  clearBuild: () => void
  /** 沉淀的查询节点 */
  queryNodes: QueryNode[]
  addQueryNode: (qn: Omit<QueryNode, 'id' | 'ts'>) => void
  removeQueryNode: (id: string) => void
}

const DOMAIN_PALETTE = ['#7c8cff', '#6ee7b7', '#f6ad55', '#f472b6', '#38bdf8', '#a78bfa']
const NO_DOMAIN = '#8b93a3'

const NODE_W = 150
const NODE_H = 46
const GAP_X = 200
const GAP_Y = 96
const PAD = 40

function hash(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return h
}

function domainColor(domain: string | null): string {
  if (!domain) return NO_DOMAIN
  return DOMAIN_PALETTE[hash(domain) % DOMAIN_PALETTE.length]
}

/**
 * 布局：按领域分组纵向堆叠，领域列横向排列；无标签/孤立表收进末尾"无标签"列。
 * 全部居中到原点附近（组件 fit 时再整体适配视口）。
 */
function layout(nodes: GraphNode[]): void {
  const groups = new Map<string, GraphNode[]>()
  const iso: GraphNode[] = []
  for (const n of nodes) {
    if (n.domain) {
      const arr = groups.get(n.domain) ?? []
      arr.push(n)
      groups.set(n.domain, arr)
    } else {
      iso.push(n)
    }
  }
  // 领域列顺序：按表数降序，稳定
  const cols = [...groups.entries()].sort((a, b) => b[1].length - a[1].length)
  let x = PAD
  for (const [, group] of cols) {
    let y = PAD
    for (const n of group) {
      n.x = x
      n.y = y
      y += NODE_H + GAP_Y
    }
    x += GAP_X
  }
  // 无标签列（最右侧）
  if (iso.length > 0) {
    let y = PAD
    for (const n of iso) {
      n.x = x
      n.y = y
      y += NODE_H + GAP_Y
    }
  }
  // 居中：平移使内容包围盒中心 ≈ 原点
  const xs = nodes.map((n) => n.x)
  const ys = nodes.map((n) => n.y)
  const minX = Math.min(...xs)
  const maxX = Math.max(...xs) + NODE_W
  const minY = Math.min(...ys)
  const maxY = Math.max(...ys) + NODE_H
  const dx = (minX + maxX) / 2
  const dy = (minY + maxY) / 2
  for (const n of nodes) {
    n.x -= dx
    n.y -= dy
  }
}

export const useGraph = create<GraphState>((set, get) => ({
  nodes: [],
  edges: [],
  bounds: { minX: -200, minY: -120, maxX: 200, maxY: 120 },
  viewport: { scale: 1, tx: 0, ty: 0 },
  mode: 'browse',
  selected: null,
  seq: 0,
  buildSel: [],
  queryNodes: [],

  build(schema, overview) {
    const tagOf = new Map<string, string>()
    const draftTags = new Set<string>()
    const commentStatus = new Map<string, string>()
    if (overview) {
      for (const t of overview.tables) {
        commentStatus.set(t.name, t.comment_status)
        const confirmed = t.tags.filter((x) => x.status === 'confirmed').map((x) => x.name)
        if (confirmed.length > 0) tagOf.set(t.name, confirmed[0])
        if (t.tags.some((x) => x.status === 'draft')) draftTags.add(t.name)
      }
    }
    const tableSet = new Set(schema.tables.map((t) => t.name))
    const nodes: GraphNode[] = schema.tables.map((t) => {
      const domain = tagOf.get(t.name) ?? null
      const status = commentStatus.get(t.name)
      return {
        id: t.name,
        name: t.name,
        domain,
        color: domainColor(domain),
        fields: t.column_count,
        annotated: status ? status === 'confirmed' : !!t.comment,
        pending: draftTags.has(t.name) || status === 'draft',
        x: 0,
        y: 0
      }
    })
    const seen = new Set<string>()
    const edges: GraphEdgeDef[] = []
    for (const fk of schema.foreign_keys) {
      if (!tableSet.has(fk.table) || !tableSet.has(fk.ref_table)) continue
      const key = [fk.table, fk.ref_table].sort().join('::')
      if (seen.has(key)) continue
      seen.add(key)
      edges.push({ from: fk.table, to: fk.ref_table, label: fk.column })
    }
    layout(nodes)
    const xs = nodes.map((n) => n.x)
    const ys = nodes.map((n) => n.y)
    const bounds = {
      minX: Math.min(...xs),
      minY: Math.min(...ys),
      maxX: Math.max(...xs) + NODE_W,
      maxY: Math.max(...ys) + NODE_H
    }
    // 治理操作后重载会重建：保留选中（若节点仍存在），避免检查器闪关
    const keepSel = get().selected && nodes.some((n) => n.id === get().selected) ? get().selected : null
    set({ nodes, edges, bounds, selected: keepSel, viewport: { scale: 1, tx: 0, ty: 0 }, seq: get().seq + 1 })
  },

  setMode(m) {
    set({ mode: m })
  },

  select(id) {
    set({ selected: id })
  },

  setViewport(v) {
    set({ viewport: v })
  },

  toggleBuild(t) {
    set((s) => ({
      buildSel: s.buildSel.includes(t)
        ? s.buildSel.filter((x) => x !== t)
        : [...s.buildSel, t]
    }))
  },

  clearBuild() {
    set({ buildSel: [] })
  },

  addQueryNode(qn) {
    set((s) => ({
      queryNodes: [...s.queryNodes, { ...qn, id: `q_${Date.now().toString(36)}`, ts: new Date().toISOString() }]
    }))
  },

  removeQueryNode(id) {
    set((s) => ({ queryNodes: s.queryNodes.filter((q) => q.id !== id) }))
  }
}))

export { NODE_W, NODE_H }


/* ---------- 搭查：FK 路径 → JOIN 骨架 ---------- */

export interface JoinStep {
  from: string
  to: string
  from_col: string
  to_col: string
  clause: string
}

/**
 * 基于 FK 关系为选中表集合生成最小连接树（贪心 BFS）。
 * 返回 { steps, missing }：steps 为 JOIN 子句序列；missing 为无法连通的表。
 */
export function buildJoinSkeleton(
  tables: string[],
  fks: { table: string; column: string; ref_table: string; ref_column: string }[]
): { steps: JoinStep[]; missing: string[] } {
  const want = new Set(tables)
  if (tables.length === 0) return { steps: [], missing: [] }
  // 无向邻接：每边记两个方向（保留原 FK 方向）
  const adj = new Map<string, { to: string; from_col: string; to_col: string }[]>()
  const push = (a: string, b: string, from_col: string, to_col: string): void => {
    const arr = adj.get(a) ?? []
    arr.push({ to: b, from_col, to_col })
    adj.set(a, arr)
  }
  for (const fk of fks) {
    if (!want.has(fk.table) || !want.has(fk.ref_table)) continue
    push(fk.table, fk.ref_table, fk.column, fk.ref_column)
    push(fk.ref_table, fk.table, fk.ref_column, fk.column)
  }
  const visited = new Set<string>([tables[0]])
  const steps: JoinStep[] = []
  const edgesUsed = new Set<string>()
  while (visited.size < tables.length) {
    let best: { a: string; b: string; from_col: string; to_col: string } | null = null
    for (const a of visited) {
      for (const e of adj.get(a) ?? []) {
        if (visited.has(e.to)) continue
        const key = [a, e.to].sort().join('::')
        if (edgesUsed.has(key)) continue
        best = { a, b: e.to, from_col: e.from_col, to_col: e.to_col }
        edgesUsed.add(key)
        break
      }
      if (best) break
    }
    if (!best) break // 图不连通
    visited.add(best.b)
    steps.push({
      from: best.a,
      to: best.b,
      from_col: best.from_col,
      to_col: best.to_col,
      clause: `JOIN ${best.b} ON ${best.a}.${best.from_col} = ${best.b}.${best.to_col}`
    })
  }
  const missing = tables.filter((t) => !visited.has(t))
  return { steps, missing }
}
