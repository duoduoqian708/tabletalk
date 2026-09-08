import { request } from './client'
import type { BuildProgress, FieldHistoryItem, GraphEdge, KbStatus, KnowledgeOverview, RoundBaseline, RouteResult, TagInfo } from './types'

export interface TagLibrary {
  library: TagInfo[]
  tables: Record<string, string[]>
}

export function overview(connId: string): Promise<KnowledgeOverview> {
  return request(`/api/v1/knowledge/${connId}/overview`)
}

/** 启动后台构建任务（任务化：立即返回，轮询 buildProgress）。
 */
export function build(
  connId: string,
  includeSamples = false,
  trigger: 'init' | 'rebuild' = 'init',
  annotateMode: 'diff' | 'full' = 'diff',
  tagMode: 'keep' | 'anchor' | 'fresh' = 'keep'
): Promise<{ job_id: string; kb_status: string; stage: string }> {
  return request(`/api/v1/knowledge/${connId}/build`, {
    method: 'POST',
    body: JSON.stringify({ include_samples: includeSamples, trigger, annotate_mode: annotateMode, tag_mode: tagMode }),
  })
}

export function buildProgress(connId: string): Promise<BuildProgress> {
  return request(`/api/v1/knowledge/${connId}/build/progress`)
}

export function buildCancel(connId: string): Promise<{ cancelled: boolean }> {
  return request(`/api/v1/knowledge/${connId}/build/cancel`, { method: 'POST' })
}

export function kbStatus(connId: string): Promise<KbStatus> {
  return request(`/api/v1/knowledge/${connId}/status`)
}

export interface DiscardResult {
  discarded: { columns: number; tables: number; tags: number; edges: number }
  /** 有历史 confirmed → ready（旧知识继续可用）；全库无 confirmed → none（未构建态） */
  kb_status: string
}

/** 放弃本轮全部草案（撤草案保历史）：draft 注释/标签/LLM 边全撤，confirmed 不动。 */
export function discardKb(connId: string): Promise<DiscardResult> {
  return request(`/api/v1/knowledge/${connId}/discard`, { method: 'POST' })
}

export function tags(connId: string): Promise<TagLibrary> {
  return request(`/api/v1/knowledge/${connId}/tags`)
}

export function confirmTag(connId: string, name: string): Promise<{ confirmed: boolean }> {
  return request(`/api/v1/knowledge/${connId}/tags/confirm`, { method: 'POST', body: JSON.stringify({ name }) })
}

export function rejectTag(connId: string, name: string): Promise<{ rejected: boolean }> {
  return request(`/api/v1/knowledge/${connId}/tags/reject`, { method: 'POST', body: JSON.stringify({ name }) })
}

/** 人工新建标签（直接 confirmed，立即可路由）。color 空 → 哈希色板兜底。 */
export function createTag(connId: string, name: string, description = '', color = ''): Promise<{ created: boolean }> {
  return request(`/api/v1/knowledge/${connId}/tags/create`, { method: 'POST', body: JSON.stringify({ name, description, color }) })
}

/** 人工编辑标签：改名（同步表绑定）/改描述/改颜色（后端持久化，全端引用同步）。 */
export function updateTag(connId: string, name: string, patch: { newName?: string; description?: string; color?: string }): Promise<{ updated: boolean }> {
  return request(`/api/v1/knowledge/${connId}/tags/update`, {
    method: 'POST',
    body: JSON.stringify({
      name,
      new_name: patch.newName ?? null,
      description: patch.description ?? null,
      color: patch.color ?? null,
    }),
  })
}

/** 删除一条用户手写笔记。 */
export function deleteDoc(connId: string, docId: string): Promise<{ deleted: boolean }> {
  return request(`/api/v1/knowledge/${connId}/docs/${encodeURIComponent(docId)}`, { method: 'DELETE' })
}

/** 知识文档列表（可按表过滤）。 */
export function listDocs(connId: string, table?: string): Promise<{ count: number; docs: Array<{ id: string; kind: string; title: string; body: string; table: string | null; column: string | null; status: string; source: string; updated_at?: string }> }> {
  const qs = table ? `?table=${encodeURIComponent(table)}` : ''
  return request(`/api/v1/knowledge/${connId}/docs${qs}`)
}

export function assignTags(connId: string, table: string, tagNames: string[]): Promise<{ assigned: number }> {
  return request(`/api/v1/knowledge/${connId}/tags/assign`, { method: 'POST', body: JSON.stringify({ table, tags: tagNames }) })
}

export function confirmComment(connId: string, table: string, column?: string, tableOnly?: boolean): Promise<{ confirmed: number }> {
  return request(`/api/v1/knowledge/${connId}/confirm`, { method: 'POST', body: JSON.stringify({ table, column: column ?? null, table_only: tableOnly ?? false }) })
}

/** 整库一键确认全部待确认注释（table/column 均为 null）。 */
export function confirmAll(connId: string): Promise<{ confirmed: number }> {
  return request(`/api/v1/knowledge/${connId}/confirm`, { method: 'POST', body: JSON.stringify({ table: null, column: null }) })
}

export function rejectComment(connId: string, table: string, column?: string): Promise<{ rejected: number }> {
  return request(`/api/v1/knowledge/${connId}/reject-comment`, { method: 'POST', body: JSON.stringify({ table, column: column ?? null }) })
}

/** 手动写入/覆盖表级注释（PUT docs，kind=note）。返回新 doc。 */
export function saveDoc(connId: string, table: string, note: string): Promise<unknown> {
  return request(`/api/v1/knowledge/${connId}/docs`, {
    method: 'PUT',
    body: JSON.stringify({ table, note, kind: 'note' })
  })
}

/** 按表编辑知识（详情面板两块）：表/列注释 + 向量化片段覆盖。只写知识字段。 */
export interface TableEditColumnInput { name: string; comment?: string | null; values?: string | null; example?: string | null }
export interface TableEditInput {
  table: string
  table_comment?: string | null
  column_comments?: TableEditColumnInput[]
  vector_text?: string | null
}
export interface TableEditResult {
  changed: boolean
  table: string
  vector_text: string
  vector_override: string | null
  vector_profile?: string | null
  proposed_profile?: string | null
}
export function patchTable(connId: string, input: TableEditInput): Promise<TableEditResult> {
  return request(`/api/v1/knowledge/${connId}/table`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  })
}

export function routeTables(connId: string, tagNames: string[]): Promise<RouteResult> {
  return request(`/api/v1/knowledge/${connId}/route`, { method: 'POST', body: JSON.stringify({ table: '', tags: tagNames }) })
}

/** 检索命中的表知识卡（v2 一表一卡：text=可读表描述，payload=结构化信息）。 */
export interface KbCard {
  table: string
  text: string
  payload: { ddl?: string; tags?: string[]; layout?: Record<string, unknown>; draft_count?: number; updated_at?: string }
  score: number
}

/** 知识检索（关键词 + 向量 + 图谱邻居扩散的混合打分）→ 表知识卡。 */
export function retrieve(connId: string, q: string, k = 10): Promise<{ count: number; cards: KbCard[] }> {
  return request(`/api/v1/knowledge/${connId}/retrieve?q=${encodeURIComponent(q)}&k=${k}`)
}

export interface GraphEdgeInput {
  from_table: string
  to_table: string
  source?: 'user'
  from_col?: string | null
  to_col?: string | null
  weight?: number | null
  /** 基数 n:1 | 1:1 | 1:N | N:M（默认 n:1） */
  cardinality?: 'n:1' | '1:1' | '1:N' | 'N:M'
  /** 守卫谓词（多态关联条件，如 "X.type = 1"），普通关联省略 */
  guard?: string | null
  /** 复合边完整列对 [[from,to],...]；缺省用 from_col/to_col 单列 */
  cols?: [string, string][] | null
}

export function addEdge(connId: string, edge: GraphEdgeInput): Promise<{ edge: GraphEdge; graph: { edges: GraphEdge[] } }> {
  return request(`/api/v1/knowledge/${connId}/graph/edges`, {
    method: 'POST',
    body: JSON.stringify({ source: 'user', ...edge }),
  })
}

export function removeEdge(connId: string, edge: { from_table: string; to_table: string; source: string }): Promise<{ removed: number; graph: { edges: GraphEdge[] } }> {
  return request(`/api/v1/knowledge/${connId}/graph/edges`, {
    method: 'DELETE',
    body: JSON.stringify(edge),
  })
}

export function setExcluded(connId: string, table: string, excluded: boolean): Promise<{ excluded: string[] }> {
  return request(`/api/v1/knowledge/${connId}/graph/exclude`, {
    method: 'POST',
    body: JSON.stringify({ table, excluded }),
  })
}

/** 持久化 2D 图布局坐标（拖拽松手回传全量快照 → 各表 payload.layout）。 */
export function putGraphLayout(connId: string, layout: Record<string, { x: number; y: number }>): Promise<{ saved: number; layout: Record<string, { x: number; y: number }> }> {
  return request(`/api/v1/knowledge/${connId}/graph/layout`, {
    method: 'PUT',
    body: JSON.stringify({ layout }),
  })
}

export interface GraphDraftEdge {
  from_table: string
  from_col: string | null
  to_table: string
  to_col: string | null
  reason: string
  source: string
}

/** 确认 LLM draft 边 → 写入正式图谱（fromTable=null 确认全部）。返回新正式边供图即时刷新。 */
export function confirmGraphDrafts(connId: string, fromTable?: string | null): Promise<{ confirmed: number; llm_draft_edges: GraphDraftEdge[]; edges: GraphEdge[] }> {
  return request(`/api/v1/knowledge/${connId}/graph/confirm`, {
    method: 'POST',
    body: JSON.stringify({ from_table: fromTable ?? null }),
  })
}

/** 拒绝 LLM draft 边（从 draft 列表移除）。 */
export function rejectGraphDrafts(connId: string, fromTable?: string | null): Promise<{ rejected: number; llm_draft_edges: GraphDraftEdge[]; edges: GraphEdge[] }> {
  return request(`/api/v1/knowledge/${connId}/graph/reject`, {
    method: 'POST',
    body: JSON.stringify({ from_table: fromTable ?? null }),
  })
}

/** 字段历史版本（版本制「版本回溯」：确认时归档，复用旧值）。 */
export function fieldHistory(connId: string, table: string, column: string): Promise<{ items: FieldHistoryItem[] }> {
  const p = new URLSearchParams({ table, column })
  return request(`/api/v1/knowledge/${connId}/field-history?${p.toString()}`)
}

/** 用历史版本覆盖当前字段（comment/values/example，状态不变）。 */
export function applyFieldHistory(connId: string, table: string, column: string, historyId: number): Promise<{ applied: boolean }> {
  return request(`/api/v1/knowledge/${connId}/field-history/apply`, {
    method: 'POST',
    body: JSON.stringify({ table, column, history_id: historyId }),
  })
}

/* ═══ 审核重构（2026-09）：本轮对比区 API ═══ */

/** 手动增量构建：结构对比 → 变化表 AI 增量注释（产生提案则进待审） */
export function syncIncremental(connId: string): Promise<{ changed: boolean; tables_added?: number; tables_removed?: number; tables_changed?: number; docs_added?: number }> {
  return request(`/api/v1/knowledge/${connId}/sync`, { method: 'POST' })
}

/** 单表旧版知识（对比层"旧"侧） */
export function roundTableBaseline(connId: string, table: string): Promise<{ table: string; has_baseline: boolean; baseline: RoundBaseline }> {
  return request(`/api/v1/knowledge/${connId}/round/table/${encodeURIComponent(table)}`)
}

/** 单表重新注释（审核镜头「重试」/手动刷新单表）：与构建共用注释缓存 */
export function annotateTable(connId: string, table: string): Promise<{ table: string; source: string; items: number; added: number }> {
  return request(`/api/v1/knowledge/${connId}/annotate-table`, {
    method: 'POST',
    body: JSON.stringify({ table }),
  })
}

/** 批量裁决：一次请求提升/撤销多张表（几百表规模） */
export function batchReview(connId: string, tables: string[], action: 'confirm' | 'reject'): Promise<{ applied: number; tables: string[]; pending: Record<string, number>; kb_status: string; version: number }> {
  return request(`/api/v1/knowledge/${connId}/tables/batch-review`, {
    method: 'POST',
    body: JSON.stringify({ tables, action }),
  })
}

/** 标签版本制应用：采纳新版 + 保留旧版，其余淘汰 */
export function applyRoundTags(connId: string, keepOld: string[], adoptNew: string[]): Promise<{ adopted: number; kept_old: number; removed: number; kb_status: string; version: number; tags: { library: TagInfo[]; tables: Record<string, string[]> } }> {
  return request(`/api/v1/knowledge/${connId}/tags/apply-round`, {
    method: 'POST',
    body: JSON.stringify({ keep_old: keepOld, adopt_new: adoptNew }),
  })
}

/** 审核收尾：待审全清 → 版本启用（ready）；未清 → 保持 pending */
export function reviewFinalize(connId: string): Promise<{ kb_status: string; version: number; pending: Record<string, number> }> {
  return request(`/api/v1/knowledge/${connId}/review/finalize`, { method: 'POST' })
}

/** 红边"保留"（pin 豁免自动移除） */
export function pinEdge(connId: string, e: { from_table: string; to_table: string; from_col?: string | null; to_col?: string | null }): Promise<{ pinned: number }> {
  return request(`/api/v1/knowledge/${connId}/graph/edges/pin`, {
    method: 'POST',
    body: JSON.stringify(e),
  })
}
