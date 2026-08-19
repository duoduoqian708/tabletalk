import { request } from './client'
import type { BuildProgress, KbStatus, KnowledgeOverview, RouteResult, TagInfo } from './types'

export interface TagLibrary {
  library: TagInfo[]
  tables: Record<string, string[]>
}

export function overview(connId: string): Promise<KnowledgeOverview> {
  return request(`/api/v1/knowledge/${connId}/overview`)
}

/** 启动后台构建任务（任务化：立即返回，轮询 buildProgress）。 */
export function build(connId: string): Promise<{ job_id: string; kb_status: string; stage: string }> {
  return request(`/api/v1/knowledge/${connId}/build`, { method: 'POST' })
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

export interface SyncResult {
  changed: boolean
  tables_added: number
  tables_removed: number
  tables_changed: number
  message?: string
}

/** 手动增量同步：指纹对比 → 变化则增量构建（返回 diff 摘要）。 */
export function syncKb(connId: string): Promise<SyncResult> {
  return request(`/api/v1/knowledge/${connId}/sync`, { method: 'POST' })
}

/** 确认闸：一键确认全部草案文档 + draft 标签 → kb_status=ready（解锁数据源）。 */
export function confirmAllEnabled(connId: string): Promise<{ docs: number; tags: number; kb_status: string }> {
  return request(`/api/v1/knowledge/${connId}/confirm-all`, { method: 'POST' })
}

export function tags(connId: string): Promise<TagLibrary> {
  return request(`/api/v1/knowledge/${connId}/tags`)
}

export function annotateTags(connId: string): Promise<{ tables: number; descriptions: number; new_tags: number; library_size: number }> {
  return request(`/api/v1/knowledge/${connId}/annotate-tags`, { method: 'POST' })
}

export function confirmTag(connId: string, name: string): Promise<{ confirmed: boolean }> {
  return request(`/api/v1/knowledge/${connId}/tags/confirm`, { method: 'POST', body: JSON.stringify({ name }) })
}

export function rejectTag(connId: string, name: string): Promise<{ rejected: boolean }> {
  return request(`/api/v1/knowledge/${connId}/tags/reject`, { method: 'POST', body: JSON.stringify({ name }) })
}

export function assignTags(connId: string, table: string, tagNames: string[]): Promise<{ assigned: number }> {
  return request(`/api/v1/knowledge/${connId}/tags/assign`, { method: 'POST', body: JSON.stringify({ table, tags: tagNames }) })
}

export function confirmComment(connId: string, table: string, column?: string): Promise<{ confirmed: number }> {
  return request(`/api/v1/knowledge/${connId}/confirm`, { method: 'POST', body: JSON.stringify({ table, column: column ?? null }) })
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

export function routeTables(connId: string, tagNames: string[]): Promise<RouteResult> {
  return request(`/api/v1/knowledge/${connId}/route`, { method: 'POST', body: JSON.stringify({ table: '', tags: tagNames }) })
}

/** 知识文档 top-N 检索（关键词 + 向量 + 图谱邻居扩散的混合打分）。 */
export interface KbDoc {
  id: string
  kind: string
  title: string
  body: string
  table: string | null
  column: string | null
  status: string
  source: string
  tags: string[]
}

export function retrieve(connId: string, q: string, k = 10): Promise<{ count: number; docs: KbDoc[] }> {
  return request(`/api/v1/knowledge/${connId}/retrieve?q=${encodeURIComponent(q)}&k=${k}`)
}
