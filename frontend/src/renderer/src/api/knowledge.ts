import { request } from './client'
import type { KnowledgeOverview, RouteResult, TagInfo } from './types'

export interface TagLibrary {
  library: TagInfo[]
  tables: Record<string, string[]>
}

export function overview(connId: string): Promise<KnowledgeOverview> {
  return request(`/api/v1/knowledge/${connId}/overview`)
}

export function build(connId: string): Promise<{ tables: number; columns: number; edges: number }> {
  return request(`/api/v1/knowledge/${connId}/build`, { method: 'POST' })
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

export function rejectComment(connId: string, table: string, column?: string): Promise<{ rejected: number }> {
  return request(`/api/v1/knowledge/${connId}/reject-comment`, { method: 'POST', body: JSON.stringify({ table, column: column ?? null }) })
}

export function routeTables(connId: string, tagNames: string[]): Promise<RouteResult> {
  return request(`/api/v1/knowledge/${connId}/route`, { method: 'POST', body: JSON.stringify({ table: '', tags: tagNames }) })
}
